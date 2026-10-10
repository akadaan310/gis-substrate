"""Export / restore: versioned manifest + content, integrity verification.

Export layout (<export_dir>/):
  manifest.json          versioned, stable ids, all metadata + relationships
  blobs/ab/cd/<sha256>   content for every referenced blob (incl. legacy bytes)

Restore refuses to run into a non-empty database. Hashes are verified on restore.
"""
import hashlib
import json
import os
import shutil
import time

from . import db as _db
from .store import blob_path, sha256_file, new_id

EXPORT_VERSION = 1

_TABLES = ["workspace", "session", "event", "blob", "artifact", "artifact_revision",
           "edge", "decision", "task", "claim", "evidence", "collection",
           "collection_member", "share_link", "checkpoint", "handoff",
           "extract_job", "extracted_chunk", "audit_log", "legacy_file", "kv"]


def _dump_table(con, table, where="", args=()):
    q = "SELECT * FROM %s" % table
    if where:
        q += " WHERE " + where
    return [dict(r) for r in con.execute(q, args).fetchall()]


def build_export(con, data_dir, workspace_id=None, include_blobs=True):
    """Return (manifest_dict, blob_shas:set). Manifest references blobs by sha256."""
    now = time.time()
    ws_filter = ""
    ws_args = ()
    if workspace_id:
        ws_filter = "workspace_id=?"
        ws_args = (workspace_id,)
    manifest = {
        "format": "substrate-export",
        "version": EXPORT_VERSION,
        "exported_at": now,
        "schema_version": _db.SCHEMA_VERSION,
        "workspace_filter": workspace_id,
        "tables": {},
        "omitted": [],
    }
    blob_shas = set()
    # workspaces
    if workspace_id:
        manifest["tables"]["workspace"] = _dump_table(
            con, "workspace", "id=?", (workspace_id,))
        ws_ids = [workspace_id]
    else:
        manifest["tables"]["workspace"] = _dump_table(con, "workspace")
        ws_ids = [w["id"] for w in manifest["tables"]["workspace"]]
    scoped = "workspace_id IN (%s)" % ",".join("?" * len(ws_ids)) if ws_ids else "1=0"
    sargs = tuple(ws_ids)
    manifest["tables"]["session"] = _dump_table(con, "session", scoped if ws_ids else "1=0", sargs)
    ses_ids = [s["id"] for s in manifest["tables"]["session"]]
    if ses_ids:
        ev = _dump_table(con, "event",
                         "session_id IN (%s)" % ",".join("?" * len(ses_ids)), tuple(ses_ids))
    else:
        ev = _dump_table(con, "event", "1=0")
    manifest["tables"]["event"] = ev
    for t in ("decision", "task", "claim", "checkpoint", "handoff"):
        manifest["tables"][t] = _dump_table(con, t, scoped if ws_ids else "1=0", sargs)
    claim_ids = [c["id"] for c in manifest["tables"]["claim"]]
    manifest["tables"]["evidence"] = _dump_table(
        con, "evidence",
        "claim_id IN (%s)" % ",".join("?" * len(claim_ids)), tuple(claim_ids)) if claim_ids else []
    # artifacts: unscoped exports include EVERYTHING (complete archive);
    # scoped exports include artifacts referenced by in-scope records.
    art_ids = set()
    if workspace_id:
        for e in ev:
            for m in _iter_artifact_refs(e.get("meta_json") or "{}"):
                art_ids.add(m)
        for e in manifest["tables"]["evidence"]:
            if e.get("artifact_id"):
                art_ids.add(e["artifact_id"])
        for h in manifest["tables"]["handoff"]:
            if h.get("payload_artifact_id"):
                art_ids.add(h["payload_artifact_id"])
        # collections are global; include membership only for referenced artifacts
        manifest["tables"]["collection"] = []
        manifest["tables"]["collection_member"] = []
        # scoped inclusion rule, documented: an artifact is in a workspace-scoped
        # export iff it is referenced by an in-scope event/evidence/handoff.
        # Edges are then restricted to those whose BOTH endpoints are exported.
    else:
        manifest["tables"]["collection"] = _dump_table(con, "collection")
        manifest["tables"]["collection_member"] = _dump_table(con, "collection_member")
        for m in manifest["tables"]["collection_member"]:
            if m.get("artifact_id"):
                art_ids.add(m["artifact_id"])
    if workspace_id:
        if art_ids:
            arts = _dump_table(con, "artifact",
                               "id IN (%s)" % ",".join("?" * len(art_ids)), tuple(art_ids))
        else:
            arts = []
    else:
        # unscoped: the complete archive. Never a subset.
        arts = _dump_table(con, "artifact")
    manifest["tables"]["artifact"] = arts
    for a in arts:
        blob_shas.add(a["blob_sha256"])
    rev_aids = [a["id"] for a in arts]
    if rev_aids:
        revs = _dump_table(con, "artifact_revision",
                           "artifact_id IN (%s)" % ",".join("?" * len(rev_aids)), tuple(rev_aids))
    else:
        revs = []
    manifest["tables"]["artifact_revision"] = revs
    for r in revs:
        blob_shas.add(r["blob_sha256"])
    # edges: every exported edge must have BOTH endpoints in the exported set.
    # (Unscoped: everything is exported, so all edges qualify. Scoped: only
    # edges between in-scope records. Dropped scoped edges are listed in
    # manifest["omitted"] so the loss is visible, never silent.)
    def _exported_ids():
        ids = set()
        for rows in manifest["tables"].values():
            for r in rows:
                if isinstance(r, dict) and r.get("id"):
                    ids.add(r["id"])
        return ids
    if workspace_id:
        exported = _exported_ids()
        all_edges = _dump_table(con, "edge")
        kept, dropped = [], []
        for e in all_edges:
            if e.get("src_id") in exported and e.get("dst_id") in exported:
                kept.append(e)
            else:
                dropped.append(e["id"])
        manifest["tables"]["edge"] = kept
        if dropped:
            manifest["omitted"].append(
                "edges with endpoints outside the workspace scope: %d dropped (%s)" %
                (len(dropped), ", ".join(dropped[:10])))
    else:
        manifest["tables"]["edge"] = _dump_table(con, "edge")
    if workspace_id and rev_aids:
        manifest["tables"]["legacy_file"] = _dump_table(
            con, "legacy_file",
            "artifact_id IN (%s)" % ",".join("?" * len(rev_aids)), tuple(rev_aids))
    elif workspace_id:
        manifest["tables"]["legacy_file"] = []
    else:
        manifest["tables"]["legacy_file"] = _dump_table(con, "legacy_file")
    # extracted chunks + jobs: full fidelity for unscoped; scoped exports keep
    # only rows belonging to exported artifacts.
    _art_id_set = {a["id"] for a in arts}
    if workspace_id and _art_id_set:
        _aids = tuple(_art_id_set)
        _ph = ",".join("?" * len(_aids))
        manifest["tables"]["extract_job"] = _dump_table(
            con, "extract_job", "artifact_id IN (%s)" % _ph, _aids)
        manifest["tables"]["extracted_chunk"] = _dump_table(
            con, "extracted_chunk", "artifact_id IN (%s)" % _ph, _aids)
    elif workspace_id:
        manifest["tables"]["extract_job"] = []
        manifest["tables"]["extracted_chunk"] = []
    else:
        manifest["tables"]["extract_job"] = _dump_table(con, "extract_job")
        manifest["tables"]["extracted_chunk"] = _dump_table(con, "extracted_chunk")
    manifest["tables"]["audit_log"] = []
    manifest["tables"]["share_link"] = []  # tokens never exported
    manifest["tables"]["blob"] = _dump_table(
        con, "blob", "sha256 IN (%s)" % ",".join("?" * len(blob_shas)),
        tuple(blob_shas)) if blob_shas else []
    if workspace_id:
        manifest["omitted"].append("share_links, audit_log: excluded from scoped export")
    return manifest, blob_shas


def _iter_artifact_refs(meta_json):
    try:
        m = json.loads(meta_json)
    except Exception:
        return
    for k in ("artifact_id", "artifact_ids"):
        v = m.get(k)
        if isinstance(v, str):
            yield v
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    yield x


def write_bundle(con, data_dir, dest_dir, workspace_id=None):
    """Write export bundle to dest_dir. Returns manifest path."""
    os.makedirs(dest_dir, exist_ok=True)
    manifest, blob_shas = build_export(con, data_dir, workspace_id)
    # copy blob bytes (legacy bytes read from source_ref when not in store)
    art_by_sha = {}
    for a in manifest["tables"]["artifact"]:
        art_by_sha.setdefault(a["blob_sha256"], a)
    for r in manifest["tables"]["artifact_revision"]:
        art_by_sha.setdefault(r["blob_sha256"], {"source": "upload", "source_ref": None})
    missing = []
    for sha in blob_shas:
        src = blob_path(data_dir, sha)
        if not os.path.isfile(src):
            a = art_by_sha.get(sha, {})
            ref = a.get("source_ref")
            if ref and os.path.isfile(ref):
                src = ref  # legacy in-place bytes
            else:
                missing.append(sha)
                continue
        dst = os.path.join(dest_dir, "blobs", sha[0:2], sha[2:4], sha)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    manifest["missing_blobs"] = missing
    mpath = os.path.join(dest_dir, "manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=1)
    return mpath


def _check_structure(manifest):
    """Verify relational integrity of the manifest: every reference must resolve
    to an exported record. Returns a list of problem strings (empty = clean)."""
    problems = []
    tables = manifest.get("tables", {})
    ids = set()
    for rows in tables.values():
        for r in rows:
            if isinstance(r, dict) and r.get("id"):
                ids.add(r["id"])
    artifact_ids = {a["id"] for a in tables.get("artifact", []) if a.get("id")}
    # edges: both endpoints must exist
    for e in tables.get("edge", []):
        for end in ("src_id", "dst_id"):
            if e.get(end) not in ids:
                problems.append("edge %s: %s %s not in export" %
                                (e.get("id"), end, e.get(end)))
    # revisions must belong to exported artifacts
    for r in tables.get("artifact_revision", []):
        if r.get("artifact_id") not in artifact_ids:
            problems.append("artifact_revision %s: artifact %s not in export" %
                            (r.get("id"), r.get("artifact_id")))
    # collection members
    for m in tables.get("collection_member", []):
        if m.get("artifact_id") and m["artifact_id"] not in artifact_ids:
            problems.append("collection_member %s: artifact %s not in export" %
                            (m.get("id"), m["artifact_id"]))
    # evidence artifact references
    for e in tables.get("evidence", []):
        if e.get("artifact_id") and e["artifact_id"] not in artifact_ids:
            problems.append("evidence %s: artifact %s not in export" %
                            (e.get("id"), e["artifact_id"]))
    # handoff payload artifacts
    for h in tables.get("handoff", []):
        if h.get("payload_artifact_id") and h["payload_artifact_id"] not in artifact_ids:
            problems.append("handoff %s: payload artifact %s not in export" %
                            (h.get("id"), h["payload_artifact_id"]))
    # legacy mappings
    for lf in tables.get("legacy_file", []):
        if lf.get("artifact_id") not in artifact_ids:
            problems.append("legacy_file %s: artifact %s not in export" %
                            (lf.get("legacy_id"), lf.get("artifact_id")))
    # extracted chunks reference exported artifacts
    for c in tables.get("extracted_chunk", []):
        if c.get("artifact_id") not in artifact_ids:
            problems.append("extracted_chunk: artifact %s not in export" %
                            c.get("artifact_id"))
    return problems


def verify_bundle(export_dir):
    """Recompute hashes of bundled blobs and check relational structure.
    Returns report dict."""
    mpath = os.path.join(export_dir, "manifest.json")
    with open(mpath) as f:
        manifest = json.load(f)
    ok, bad, missing = 0, [], []
    want = set()
    for a in manifest["tables"].get("artifact", []):
        want.add(a["blob_sha256"])
    for r in manifest["tables"].get("artifact_revision", []):
        want.add(r["blob_sha256"])
    for sha in want:
        p = os.path.join(export_dir, "blobs", sha[0:2], sha[2:4], sha)
        if not os.path.isfile(p):
            missing.append(sha)
            continue
        if sha256_file(p) == sha:
            ok += 1
        else:
            bad.append(sha)
    structure_problems = _check_structure(manifest)
    return {"format_ok": manifest.get("format") == "substrate-export",
            "version": manifest.get("version"),
            "blobs_ok": ok, "blobs_bad": bad, "blobs_missing": missing,
            "manifest_missing_blobs": manifest.get("missing_blobs", []),
            "structure_ok": not structure_problems,
            "structure_problems": structure_problems}


def restore_bundle(export_dir, data_dir):
    """Restore into an EMPTY data_dir. Refuses non-empty targets."""
    os.makedirs(data_dir, exist_ok=True)
    target_db = os.path.join(data_dir, "substrate.db")
    if os.path.exists(target_db) and os.path.getsize(target_db) > 0:
        # allow only a freshly-initialized empty db
        con0 = _db.connect(data_dir)
        n = con0.execute("SELECT COUNT(*) FROM workspace").fetchone()[0]
        con0.close()
        if n > 0:
            raise RuntimeError("refusing to restore into a non-empty database")
    report = verify_bundle(export_dir)
    if report["blobs_bad"] or report["blobs_missing"]:
        raise RuntimeError("bundle failed integrity check: %s" % report)
    if not report["structure_ok"]:
        raise RuntimeError(
            "bundle failed structural check (%d problems, e.g. %s)" %
            (len(report["structure_problems"]),
             "; ".join(report["structure_problems"][:3])))
    with open(os.path.join(export_dir, "manifest.json")) as f:
        manifest = json.load(f)
    con = _db.connect(data_dir)
    _db.init_db(con)
    with con:
        for table in _TABLES:
            rows = manifest["tables"].get(table, [])
            for row in rows:
                if table == "kv" and row["key"] == "data_dir":
                    continue  # target keeps its own data_dir
                cols = ", ".join(row.keys())
                ph = ", ".join("?" * len(row))
                con.execute("INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (table, cols, ph),
                            tuple(row.values()))
        # copy blobs into the store
        for sha in {a["blob_sha256"] for a in manifest["tables"].get("artifact", [])} | \
                   {r["blob_sha256"] for r in manifest["tables"].get("artifact_revision", [])}:
            src = os.path.join(export_dir, "blobs", sha[0:2], sha[2:4], sha)
            dst = blob_path(data_dir, sha)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.isfile(dst):
                shutil.copyfile(src, dst)
        # rebuild FTS index from restored extracted chunks (full fidelity),
        # falling back to metadata for artifacts without chunks.
        con.execute("DELETE FROM search_fts")
        chunks_by_art = {}
        for c in manifest["tables"].get("extracted_chunk", []):
            chunks_by_art.setdefault(c["artifact_id"], []).append(c)
        for a in manifest["tables"].get("artifact", []):
            chunks = sorted(chunks_by_art.get(a["id"], []),
                            key=lambda c: c["chunk_no"])
            body = "\n\n".join(c["text"] for c in chunks)[:50000]
            con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body)"
                        " VALUES('artifact',?,?,?)", (a["id"], a["filename"], body))
        for e in manifest["tables"].get("event", []):
            con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body)"
                        " VALUES('event',?,?,?)",
                        (e["id"], "%s / %s" % (e.get("actor", ""), e.get("kind", "")),
                         e.get("text", "")))
        for t, dtype in (("decision", "decision"), ("task", "task"), ("claim", "claim")):
            for r in manifest["tables"].get(t, []):
                con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body)"
                            " VALUES(?,?,?,?)", (dtype, r["id"], r.get("title", ""),
                                                 r.get("body", "")))
        # record restore provenance: a restored system must distinguish a
        # complete search index from one that is still rebuilding.
        _n_chunks = sum(len(v) for v in chunks_by_art.values())
        _pending = sum(1 for j in manifest["tables"].get("extract_job", [])
                       if j.get("status") in ("pending", "processing"))
        _failed = sum(1 for j in manifest["tables"].get("extract_job", [])
                      if j.get("status") == "failed")
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('fts_status', ?)",
                    (json.dumps({"restored_at": time.time(),
                                 "chunks_restored": _n_chunks,
                                 "jobs_pending": _pending,
                                 "jobs_failed": _failed,
                                 "index": "complete" if not _pending else "rebuilding"}),))
    counts = {t: len(manifest["tables"].get(t, [])) for t in _TABLES}
    return {"restored_tables": counts, "integrity": report}
