"""Retrieval layers A (exact), B (structured), C (full-text), D (semantic, deferred).

Layer A: stable identifiers, exact filenames, sha256 prefixes, legacy ids.
Layer B: structured SQL filters with deterministic ordering and bounded limits.
Layer C: FTS5 BM25 over search_fts; results always resolve to a source id.
Layer D: insertion point only -- returns empty with an explicit reason until an
    embedding backend is configured.
Layer E (graph expansion) lives in model.traverse; context.build_package
    combines layers into a budgeted context package.
"""
import re

_HEX = re.compile(r"^[0-9a-fA-F]+$")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _clamp_limit(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = DEFAULT_LIMIT
    return max(1, min(v, MAX_LIMIT))


# ---------------------------------------------------------------- Layer A

def search_exact(con, data_dir, q):
    """Exact lookups by id / filename / sha256 prefix / legacy id.

    One indexed query per table; results capped at 50.
    """
    q = (q or "").strip()
    if not q:
        return []
    out = []

    def add(doc_type, doc_id, title, snippet, locator, why):
        out.append({"doc_type": doc_type, "doc_id": doc_id, "title": title,
                    "snippet": snippet, "locator": locator,
                    "layer": "exact", "why": why})

    # artifact id (PK)
    r = con.execute("SELECT id, filename, blob_sha256, mime, size FROM artifact"
                    " WHERE id=?", (q,)).fetchone()
    if r:
        add("artifact", r["id"], r["filename"],
            "artifact %s (%s, %d bytes)" % (r["id"], r["mime"], r["size"]),
            "sha256:%s" % r["blob_sha256"][:16], "exact artifact id match")
    # exact filename (indexed)
    for r in con.execute(
            "SELECT id, filename, mime, size FROM artifact WHERE filename=?"
            " ORDER BY created_at DESC LIMIT 25", (q,)):
        add("artifact", r["id"], r["filename"],
            "artifact %s (%s, %d bytes)" % (r["id"], r["mime"], r["size"]),
            "filename exact match", "exact filename match")
    # sha256 prefix (>= 8 hex chars, prefix LIKE is index-friendly)
    if len(q) >= 8 and _HEX.match(q):
        for r in con.execute(
                "SELECT id, filename, blob_sha256 FROM artifact"
                " WHERE blob_sha256 LIKE ? ORDER BY created_at DESC LIMIT 25",
                (q.lower() + "%",)):
            add("artifact", r["id"], r["filename"],
                "sha256 %s..." % r["blob_sha256"][:16],
                "sha256 prefix", "sha256 prefix match (%d chars)" % len(q))
    # legacy id -> artifact (PK)
    r = con.execute("SELECT legacy_id, artifact_id FROM legacy_file WHERE legacy_id=?",
                    (q,)).fetchone()
    if r:
        art = con.execute("SELECT filename FROM artifact WHERE id=?",
                          (r["artifact_id"],)).fetchone()
        add("artifact", r["artifact_id"],
            art["filename"] if art else r["artifact_id"],
            "legacy id %s" % r["legacy_id"],
            "legacy_file map", "legacy id maps to artifact")
    # session id (PK)
    r = con.execute("SELECT id, title FROM session WHERE id=?", (q,)).fetchone()
    if r:
        add("session", r["id"], r["title"] or "(untitled session)",
            "session %s" % r["id"], "session id", "exact session id match")
    # event id (PK)
    r = con.execute("SELECT id, session_id, seq, kind, actor FROM event WHERE id=?",
                    (q,)).fetchone()
    if r:
        add("event", r["id"], "%s: %s #%d" % (r["kind"], r["actor"] or "?", r["seq"]),
            "event %s in session %s" % (r["id"], r["session_id"]),
            "session %s seq %d" % (r["session_id"], r["seq"]),
            "exact event id match")
    # handoff id (PK)
    r = con.execute("SELECT id, objective, status FROM handoff WHERE id=?",
                    (q,)).fetchone()
    if r:
        add("handoff", r["id"], (r["objective"] or "(no objective)")[:80],
            "handoff %s, status %s" % (r["id"], r["status"]),
            "handoff id", "exact handoff id match")

    # dedupe by (doc_type, doc_id), keep first
    seen, deduped = set(), []
    for item in out:
        key = (item["doc_type"], item["doc_id"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:50]


# ---------------------------------------------------------------- Layer B

_WS_LINKED = ("session", "decision", "task", "claim")


def _workspace_artifact_ids(con, workspace_id, cap=5000):
    """Artifact ids directly edge-linked to workspace-owned sessions/decisions/
    tasks/claims (bounded). Events resolve via their session; claim evidence is
    covered by claim links.
    """
    sess_ids = [r[0] for r in con.execute(
        "SELECT id FROM session WHERE workspace_id=? ORDER BY created_at DESC LIMIT ?",
        (workspace_id, cap))]
    dec_ids = [r[0] for r in con.execute(
        "SELECT id FROM decision WHERE workspace_id=? LIMIT ?", (workspace_id, cap))]
    tsk_ids = [r[0] for r in con.execute(
        "SELECT id FROM task WHERE workspace_id=? LIMIT ?", (workspace_id, cap))]
    clm_ids = [r[0] for r in con.execute(
        "SELECT id FROM claim WHERE workspace_id=? LIMIT ?", (workspace_id, cap))]
    buckets = {"session": sess_ids, "decision": dec_ids,
               "task": tsk_ids, "claim": clm_ids}
    aids = set()
    for otype, oids in buckets.items():
        if not oids:
            continue
        ph = ",".join("?" * len(oids))
        q = ("SELECT DISTINCT CASE WHEN src_type='artifact' THEN src_id ELSE dst_id END"
             " FROM edge WHERE"
             " (src_type='artifact' AND dst_type=? AND dst_id IN (%s))"
             " OR (dst_type='artifact' AND src_type=? AND src_id IN (%s))") % (ph, ph)
        for (aid,) in con.execute(q, [otype] + oids + [otype] + oids):
            aids.add(aid)
            if len(aids) >= cap:
                return aids
    return aids


def search_structured(con, filters):
    """Structured filtering. filters keys: workspace_id, session_id, actor,
    kind (event kind), mime_prefix, visibility, since, until, limit
    (default 50, max 200). All SQL with indexes; deterministic ORDER BY.
    """
    f = dict(filters or {})
    limit = _clamp_limit(f.get("limit", DEFAULT_LIMIT))
    since, until = f.get("since"), f.get("until")
    wid = f.get("workspace_id")

    def time_clause(col, clauses, args):
        if since is not None:
            clauses.append("%s >= ?" % col); args.append(since)
        if until is not None:
            clauses.append("%s <= ?" % col); args.append(until)

    out = {"artifacts": [], "events": [], "sessions": [],
           "decisions": [], "tasks": [], "claims": []}

    # -- sessions
    c, a = [], []
    if wid:
        c.append("workspace_id=?"); a.append(wid)
    time_clause("created_at", c, a)
    q = "SELECT * FROM session" + (" WHERE " + " AND ".join(c) if c else "") \
        + " ORDER BY created_at DESC, id DESC LIMIT ?"
    a.append(limit)
    out["sessions"] = [dict(r) for r in con.execute(q, a)]

    # -- events
    c, a = [], []
    sid = f.get("session_id")
    if sid:
        c.append("session_id=?"); a.append(sid)
    elif wid:
        c.append("session_id IN (SELECT id FROM session WHERE workspace_id=?)")
        a.append(wid)
    if f.get("actor"):
        c.append("actor=?"); a.append(f["actor"])
    if f.get("kind"):
        c.append("kind=?"); a.append(f["kind"])
    time_clause("COALESCE(ts, created_at)", c, a)
    q = "SELECT * FROM event" + (" WHERE " + " AND ".join(c) if c else "") \
        + " ORDER BY session_id, seq LIMIT ?"
    a.append(limit)
    out["events"] = [dict(r) for r in con.execute(q, a)]

    # -- artifacts
    c, a = [], []
    if f.get("mime_prefix"):
        c.append("mime LIKE ?"); a.append(f["mime_prefix"] + "%")
    if f.get("visibility"):
        c.append("visibility=?"); a.append(f["visibility"])
    time_clause("created_at", c, a)
    aids = None
    if wid:
        aids = _workspace_artifact_ids(con, wid)
        if not aids:
            out["artifacts"] = []
        else:
            c.append("id IN (%s)" % ",".join("?" * len(aids)))
            a.extend(sorted(aids))
    if wid and not aids:
        pass
    else:
        q = "SELECT * FROM artifact" + (" WHERE " + " AND ".join(c) if c else "") \
            + " ORDER BY created_at DESC, id DESC LIMIT ?"
        a.append(limit)
        out["artifacts"] = [dict(r) for r in con.execute(q, a)]

    # -- decisions / tasks / claims
    for table, key in (("decision", "decisions"), ("task", "tasks"),
                       ("claim", "claims")):
        c, a = [], []
        if wid:
            c.append("workspace_id=?"); a.append(wid)
        order = "updated_at DESC" if table == "task" else "created_at DESC"
        time_clause("created_at", c, a)
        q = "SELECT * FROM %s" % table \
            + (" WHERE " + " AND ".join(c) if c else "") \
            + " ORDER BY %s, id DESC LIMIT ?" % order
        a.append(limit)
        out[key] = [dict(r) for r in con.execute(q, a)]

    return out


# ---------------------------------------------------------------- Layer C

def _fts_match_expr(q):
    """Build a safe FTS5 MATCH expression from raw user text.

    SQLite < 3.38 cannot bind MATCH parameters, so the expression is
    interpolated — every term is double-quoted with embedded quotes doubled,
    which makes injection impossible. Terms are OR-ed; capped at 20.
    """
    terms = re.findall(r"[^\s]+", (q or "").strip())
    safe = []
    for t in terms[:20]:
        t = t.replace('"', '""').strip("*:^()")
        if t:
            safe.append('"%s"' % t)
    return " OR ".join(safe)


def search_fulltext(con, q, limit=50, doc_types=None):
    """BM25 full-text over search_fts. Results carry snippet + rank and always
    resolve to (doc_type, doc_id).

    Artifact results are enriched with chunk-level provenance: which
    extraction backend produced the text (embedded-text vs ocr), the page
    location, and whether extraction is complete or pending for that
    artifact. This keeps retrieved passages traceable to their source.
    """
    limit = _clamp_limit(limit)
    match = _fts_match_expr(q)
    if not match:
        return []
    args = []
    qsql = ("SELECT doc_type, doc_id, title,"
            " snippet(search_fts, 3, '<b>', '</b>', '...', 40) AS snip,"
            " bm25(search_fts) AS rank"
            " FROM search_fts WHERE search_fts MATCH '%s'" % match.replace("'", "''"))
    if doc_types:
        qsql += " AND doc_type IN (%s)" % ",".join("?" * len(doc_types))
        args.extend(doc_types)
    qsql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    out = []
    art_ids = [r["doc_id"] for r in con.execute(qsql, args)]
    # batch provenance for artifact hits (avoids N+1 queries)
    prov_map = _batch_provenance(con, [i for i in art_ids
                                       if True]) if art_ids else {}
    for r in con.execute(qsql, args):
        item = {"doc_type": r["doc_type"], "doc_id": r["doc_id"],
                "title": r["title"] or "(untitled)",
                "snippet": r["snip"] or "",
                "rank": r["rank"], "layer": "fulltext",
                "why": "FTS5 BM25 match on '%s'" % (q or "")[:60]}
        if r["doc_type"] == "artifact":
            item["provenance"] = prov_map.get(r["doc_id"], {"extraction": "unknown"})
        out.append(item)
    return out


def _batch_provenance(con, artifact_ids):
    """Fetch extraction provenance for many artifacts in batched queries."""
    if not artifact_ids:
        return {}
    ph = ",".join("?" * len(artifact_ids))
    # chunk backends/methods
    chunk_info = {}
    for aid, backend, method in con.execute(
            "SELECT artifact_id, backend, method FROM extracted_chunk"
            " WHERE artifact_id IN (%s)" % ph, tuple(artifact_ids)):
        chunk_info.setdefault(aid, set()).add(
            ((backend or "unknown"), (method or "unknown")))
    chunk_counts = {r[0]: r[1] for r in con.execute(
        "SELECT artifact_id, COUNT(*) FROM extracted_chunk"
        " WHERE artifact_id IN (%s) GROUP BY artifact_id" % ph,
        tuple(artifact_ids))}
    # latest job per artifact
    jobs = {}
    for aid, status, err in con.execute(
            "SELECT artifact_id, status, last_error FROM extract_job"
            " WHERE artifact_id IN (%s)" % ph, tuple(artifact_ids)):
        # keep the most recent (relies on row order; refine if needed)
        jobs[aid] = (status, err)
    # current revisions
    cur_revs = {}
    for aid, rid in con.execute(
            "SELECT artifact_id, id FROM artifact_revision WHERE artifact_id IN (%s)"
            " ORDER BY rev_no DESC" % ph, tuple(artifact_ids)):
        cur_revs.setdefault(aid, rid)
    chunk_revs = {}
    for aid, rid in con.execute(
            "SELECT artifact_id, revision_id FROM extracted_chunk"
            " WHERE artifact_id IN (%s) AND revision_id IS NOT NULL"
            " GROUP BY artifact_id, revision_id" % ph,
            tuple(artifact_ids)):
        chunk_revs.setdefault(aid, set()).add(rid)
    out = {}
    for aid in artifact_ids:
        prov = {"extraction": "unknown", "complete": True}
        if aid in chunk_info:
            methods = sorted(chunk_info[aid])
            prov["extraction"] = ", ".join("%s/%s" % m for m in methods)
            prov["chunk_count"] = chunk_counts.get(aid, 0)
        if aid in jobs:
            status, err = jobs[aid]
            prov["job_status"] = status
            if status in ("pending", "processing"):
                prov["complete"] = False
                prov["note"] = "extraction %s; index may be incomplete" % status
            elif status in ("failed", "unavailable"):
                prov["complete"] = False
                prov["note"] = "extraction %s: %s" % (status, (err or "")[:120])
        if aid in cur_revs and aid in chunk_revs:
            if cur_revs[aid] not in chunk_revs[aid]:
                prov["complete"] = False
                prov["note"] = "extracted text is from an older revision"
        out[aid] = prov
    return out


def semantic_candidates(con, q, limit=20):
    """Deferred: no embedding backend is configured.

    Returns ([], reason). FTS5 + structured + graph retrieval cover the
    product need; see DESIGN.md decision 5.
    """
    return [], "deferred: no embedding backend configured"


# ---------------------------------------------------------------- combined

def _in_workspace(con, doc_type, doc_id, workspace_id):
    """True when the object belongs to the workspace (bounded, per-type check)."""
    if doc_type in ("session", "decision", "task", "claim", "checkpoint", "handoff"):
        r = con.execute("SELECT workspace_id FROM %s WHERE id=?" % doc_type,
                        (doc_id,)).fetchone()
        return bool(r) and r[0] == workspace_id
    if doc_type == "event":
        r = con.execute("SELECT s.workspace_id FROM event e JOIN session s"
                        " ON s.id=e.session_id WHERE e.id=?", (doc_id,)).fetchone()
        return bool(r) and r[0] == workspace_id
    if doc_type == "artifact":
        return doc_id in _workspace_artifact_ids(con, workspace_id)
    return True  # collections etc.: no workspace binding; do not over-filter


def search_all(con, q, filters=None, limit=50, doc_types=None, workspace_id=None,
               data_dir=None):
    """Exact first, then full-text; dedupe by (doc_type, doc_id).

    Never serializes the whole archive. Each result carries layer + why.
    `filters` may carry doc_type/doc_types (str or list). `workspace_id`
    post-filters the bounded result set. `data_dir` accepted for
    compatibility; exact lookups do not need it.
    """
    limit = _clamp_limit(limit)
    f = dict(filters or {})
    dts = doc_types or f.get("doc_types") or f.get("doc_type")
    if isinstance(dts, str):
        dts = [dts]
    results, seen = [], set()
    considered = 0
    layers_used = []

    exact = search_exact(con, data_dir, q)
    considered += len(exact)
    if exact:
        layers_used.append("exact")
    for item in exact:
        key = (item["doc_type"], item["doc_id"])
        if key not in seen:
            seen.add(key)
            results.append(item)

    fts = search_fulltext(con, q, limit=limit, doc_types=dts)
    considered += len(fts)
    if fts:
        layers_used.append("fulltext")
    for item in fts:
        key = (item["doc_type"], item["doc_id"])
        if key not in seen:
            seen.add(key)
            results.append(item)

    sem, sem_reason = semantic_candidates(con, q, limit=20)
    considered += len(sem)
    layers_note = {"semantic": sem_reason} if not sem else {}

    if workspace_id:
        results = [r for r in results
                   if _in_workspace(con, r["doc_type"], r["doc_id"], workspace_id)]

    return {"results": results[:limit], "layers_used": layers_used,
            "total_considered": considered, "layer_notes": layers_note}
