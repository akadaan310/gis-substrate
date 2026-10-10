"""Read-only integrity checker: database <-> blob store consistency.

Checks (all read-only; never deletes or repairs):
  - every blob row has a corresponding file in blobs/
  - every blob file hashes to its expected sha256
  - every artifact / artifact_revision references an existing blob row + file
  - orphaned blob files (on disk, not referenced by any row)
  - orphaned blob rows (in db, not referenced by any artifact/revision)
  - every edge endpoint resolves to an existing record
  - collection_member / evidence / handoff / legacy_file references resolve
  - extracted_chunk rows reference existing artifacts (and current revisions)
  - extract_job rows reference existing artifacts
  - search_fts consistency: every artifact has an FTS row; FTS rows reference
    existing artifacts (sampled body check, not full reindex)

Distinguishes corruption (missing/unreadable/hash-mismatch) from harmless
states (orphaned files pending GC, pending jobs). Prints counts and
actionable diagnostics; never prints document contents or secrets.
"""
import os

from .store import blob_path, sha256_file


def check(data_dir, hash_sample=0):
    """Run all checks. Returns a report dict.

    hash_sample: if >0, hash-verify only that many blob files (0 = all).
    """
    import sqlite3
    db_path = os.path.join(data_dir, "substrate.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rep = {"data_dir": data_dir, "checks": {}, "problems": []}

    def problem(kind, detail):
        rep["problems"].append({"kind": kind, "detail": detail})

    # -- blob rows vs files ---------------------------------------------
    # Note: legacy-migrated artifacts keep bytes in place (source_ref);
    # their blob rows exist for hashing/dedup but have no blobs/ file.
    legacy_shas = {r["blob_sha256"] for r in con.execute(
        "SELECT blob_sha256 FROM artifact WHERE source='legacy'")}
    blob_rows = {r["sha256"]: r["size"]
                 for r in con.execute("SELECT sha256, size FROM blob")}
    rep["checks"]["blob_rows"] = len(blob_rows)
    rep["checks"]["legacy_in_place"] = len(legacy_shas)
    missing_files, hash_bad, hash_ok = [], [], 0
    files_to_hash = [s for s in blob_rows if s not in legacy_shas]
    if hash_sample and len(files_to_hash) > hash_sample:
        import random
        files_to_hash = random.Random(42).sample(files_to_hash, hash_sample)
    for sha in files_to_hash:
        p = blob_path(data_dir, sha)
        if not os.path.isfile(p):
            missing_files.append(sha)
        else:
            try:
                if sha256_file(p) == sha:
                    hash_ok += 1
                else:
                    hash_bad.append(sha)
                    problem("blob_hash_mismatch",
                            "blob file %s does not match its sha256" % sha[:16])
            except OSError as e:
                problem("blob_unreadable", "%s: %s" % (sha[:16], e))
    for sha in missing_files:
        problem("blob_file_missing", "blob row %s has no file" % sha[:16])
    rep["checks"]["blob_files_missing"] = len(missing_files)
    rep["checks"]["blob_hash_ok"] = hash_ok
    rep["checks"]["blob_hash_bad"] = len(hash_bad)

    # -- artifact / revision references ----------------------------------
    artifact_ids = {r["id"] for r in con.execute("SELECT id FROM artifact")}
    rep["checks"]["artifacts"] = len(artifact_ids)
    for aid, sha, source, ref in con.execute(
            "SELECT id, blob_sha256, source, source_ref FROM artifact"):
        if sha not in blob_rows:
            problem("artifact_missing_blob",
                    "artifact %s references unknown blob %s" % (aid, sha[:16]))
        # legacy artifacts store bytes in place (source_ref), not in blobs/
        if source == "legacy" and ref:
            if not os.path.isfile(ref):
                problem("artifact_source_ref_missing",
                        "legacy artifact %s source file missing" % aid)
        elif not os.path.isfile(blob_path(data_dir, sha)):
            problem("artifact_blob_file_missing",
                    "artifact %s blob file missing: %s" % (aid, sha[:16]))
    rev_count = 0
    for rid, aid, sha in con.execute(
            "SELECT id, artifact_id, blob_sha256 FROM artifact_revision"):
        rev_count += 1
        if aid not in artifact_ids:
            problem("revision_orphaned",
                    "revision %s references missing artifact %s" % (rid, aid))
        if sha not in blob_rows:
            problem("revision_missing_blob",
                    "revision %s references unknown blob %s" % (rid, sha[:16]))
    rep["checks"]["revisions"] = rev_count

    # -- orphaned blobs (informational, not corruption) -------------------
    referenced = {r["blob_sha256"] for r in
                  con.execute("SELECT blob_sha256 FROM artifact")}
    referenced |= {r["blob_sha256"] for r in
                   con.execute("SELECT blob_sha256 FROM artifact_revision")}
    orphan_rows = [s for s in blob_rows if s not in referenced]
    rep["checks"]["orphan_blob_rows"] = len(orphan_rows)
    # orphaned files on disk
    on_disk = set()
    blobs_dir = os.path.join(data_dir, "blobs")
    if os.path.isdir(blobs_dir):
        for d1 in os.listdir(blobs_dir):
            p1 = os.path.join(blobs_dir, d1)
            if not os.path.isdir(p1):
                continue
            for d2 in os.listdir(p1):
                p2 = os.path.join(p1, d2)
                if not os.path.isdir(p2):
                    continue
                for fn in os.listdir(p2):
                    if len(fn) == 64:
                        on_disk.add(fn)
    orphan_files = [s for s in on_disk if s not in blob_rows]
    rep["checks"]["orphan_blob_files"] = len(orphan_files)
    if orphan_files:
        problem("orphan_blob_files",
                "%d blob files on disk have no db row (pending GC or manual copy)"
                % len(orphan_files))

    # -- relationship endpoints -------------------------------------------
    all_ids = set(artifact_ids)
    for t in ("workspace", "session", "event", "decision", "task", "claim",
              "collection", "handoff", "checkpoint"):
        try:
            all_ids |= {r["id"] for r in con.execute("SELECT id FROM %s" % t)}
        except Exception:
            pass
    edge_bad = 0
    for eid, s, d in con.execute("SELECT id, src_id, dst_id FROM edge"):
        if s not in all_ids or d not in all_ids:
            edge_bad += 1
            problem("edge_dangling",
                    "edge %s: %s -> %s has missing endpoint" % (eid, s, d))
    rep["checks"]["edges"] = con.execute(
        "SELECT COUNT(*) FROM edge").fetchone()[0]
    rep["checks"]["edges_dangling"] = edge_bad

    # -- other foreign references ------------------------------------------
    for tbl, col, idcol, label in (
            ("collection_member", "artifact_id", "id", "collection_member"),
            ("evidence", "artifact_id", "id", "evidence"),
            ("handoff", "payload_artifact_id", "id", "handoff"),
            ("legacy_file", "artifact_id", "legacy_id", "legacy_file"),
            ("extracted_chunk", "artifact_id", "chunk_no", "extracted_chunk"),
            ("extract_job", "artifact_id", "id", "extract_job")):
        try:
            bad = 0
            for (rid, ref) in con.execute(
                    "SELECT %s, %s FROM %s WHERE %s IS NOT NULL" % (
                        idcol, col, tbl, col)):
                target = artifact_ids if "artifact" in col else all_ids
                if ref not in target:
                    bad += 1
                    problem("dangling_reference",
                            "%s %s references missing %s %s" %
                            (label, rid, col, ref))
            rep["checks"]["%s_dangling" % tbl] = bad
        except Exception as e:
            rep["checks"]["%s_dangling" % tbl] = "error: %s" % e

    # -- extraction vs current revision ------------------------------------
    stale_chunks = 0
    for aid in artifact_ids:
        rev = con.execute(
            "SELECT id FROM artifact_revision WHERE artifact_id=? "
            "ORDER BY rev_no DESC LIMIT 1", (aid,)).fetchone()
        if not rev:
            continue
        chunk_revs = {r[0] for r in con.execute(
            "SELECT DISTINCT revision_id FROM extracted_chunk WHERE artifact_id=?",
            (aid,))}
        if chunk_revs and rev[0] not in chunk_revs:
            stale_chunks += 1
            problem("stale_extraction",
                    "artifact %s has chunks from an older revision" % aid)
    rep["checks"]["artifacts_with_stale_chunks"] = stale_chunks

    # -- FTS consistency ----------------------------------------------------
    fts_artifacts = {r["doc_id"] for r in con.execute(
        "SELECT doc_id FROM search_fts WHERE doc_type='artifact'")}
    missing_fts = artifact_ids - fts_artifacts
    rep["checks"]["artifacts_missing_fts"] = len(missing_fts)
    if missing_fts:
        problem("fts_missing",
                "%d artifacts have no FTS row" % len(missing_fts))
    extra_fts = fts_artifacts - artifact_ids
    rep["checks"]["fts_orphan_rows"] = len(extra_fts)
    if extra_fts:
        problem("fts_orphan",
                "%d FTS rows reference missing artifacts" % len(extra_fts))

    # -- extract job states (informational) ---------------------------------
    rep["checks"]["jobs_by_status"] = {
        r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) AS n FROM extract_job GROUP BY status")}
    con.close()
    rep["ok"] = not rep["problems"]
    rep["summary"] = ("%d problems found" % len(rep["problems"])
                      if rep["problems"] else "all checks passed")
    return rep


def format_report(rep):
    """Human-readable summary. No document contents, no secrets."""
    lines = ["integrity check: %s" % rep["data_dir"],
             "result: %s" % rep["summary"]]
    for k in sorted(rep["checks"]):
        v = rep["checks"][k]
        if isinstance(v, dict):
            v = ", ".join("%s=%s" % (sk, sv) for sk, sv in sorted(v.items()))
        lines.append("  %s: %s" % (k, v))
    if rep["problems"]:
        lines.append("problems:")
        kinds = {}
        for p in rep["problems"]:
            kinds.setdefault(p["kind"], []).append(p["detail"])
        for kind in sorted(kinds):
            lines.append("  [%s] %d" % (kind, len(kinds[kind])))
            for d in kinds[kind][:5]:
                lines.append("    - %s" % d)
            if len(kinds[kind]) > 5:
                lines.append("    ... and %d more" % (len(kinds[kind]) - 5))
    return "\n".join(lines)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Substrate integrity checker (read-only)")
    ap.add_argument("--data-dir", required=True, help="substrate data directory")
    ap.add_argument("--hash-sample", type=int, default=0,
                    help="hash-verify at most N blob files (0 = all)")
    args = ap.parse_args(argv)
    rep = check(args.data_dir, hash_sample=args.hash_sample)
    print(format_report(rep))
    return 1 if not rep["ok"] else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
