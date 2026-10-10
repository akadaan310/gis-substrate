"""Canonical record operations: workspaces, sessions, events, edges, decisions,
tasks/questions, claims/evidence, collections, checkpoints, handoffs, share links, audit.

All writes are explicit. Nothing here invents timestamps or merges histories.
"""
import hashlib
import json
import secrets
import time

from . import db as _db
from .store import new_id, fts_upsert_event, fts_upsert_record

TIME_Q = ("exact", "imported", "estimated", "unknown")
HANDOFF_STATUS = ("prepared", "ready", "authorized", "sent", "acknowledged",
                  "executing", "completed", "failed", "abandoned")
# allowed transitions (None = any from prepared)
_HANDOFF_FLOW = {
    "prepared": ("ready", "abandoned"),
    "ready": ("authorized", "prepared", "abandoned"),
    "authorized": ("sent", "ready", "abandoned"),
    "sent": ("acknowledged", "failed", "abandoned"),
    "acknowledged": ("executing", "failed", "abandoned"),
    "executing": ("completed", "failed", "abandoned"),
    "completed": (),
    "failed": ("prepared",),
    "abandoned": (),
}
EDGE_REL = ("contains", "references", "introduces", "documents", "produces",
            "authorizes", "changes", "validates", "supports", "refutes",
            "derives_from", "cites", "contradicts", "qualifies", "summarizes",
            "supersedes", "resumes", "blocks", "depends_on", "investigates",
            "resolves", "requires_verification", "member_of", "captures",
            "acknowledges", "embeds", "renders")


def _j(v):
    return json.dumps(v) if not isinstance(v, str) else v


def _row(r):
    return dict(r) if r else None


def _tq(v):
    return v if v in TIME_Q else "exact"


# ---------------------------------------------------------------- workspaces

def create_workspace(con, title, description="", meta=None):
    wid = new_id("ws")
    now = time.time()
    with con:
        con.execute("INSERT INTO workspace(id,title,description,created_at,meta_json)"
                    " VALUES(?,?,?,?,?)", (wid, title, description, now, _j(meta or {})))
    return get_workspace(con, wid)


def get_workspace(con, wid):
    return _row(con.execute("SELECT * FROM workspace WHERE id=?", (wid,)).fetchone())


def list_workspaces(con):
    return [_row(r) for r in con.execute("SELECT * FROM workspace ORDER BY created_at DESC")]


# ---------------------------------------------------------------- sessions

def create_session(con, workspace_id=None, title="", started_at=None, ended_at=None,
                   time_quality="exact", source="manual", source_id=None, meta=None):
    sid = new_id("ses")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO session(id, workspace_id, title, started_at, ended_at,
                                    time_quality, source, source_id, meta_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sid, workspace_id, title, started_at, ended_at, _tq(time_quality),
             source, source_id, _j(meta or {}), now))
    return get_session(con, sid)


def get_session(con, sid):
    return _row(con.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone())


def list_sessions(con, workspace_id=None, limit=50, cursor=None):
    q = "SELECT * FROM session"
    args = []
    clauses = []
    if workspace_id:
        clauses.append("workspace_id=?")
        args.append(workspace_id)
    if cursor:
        clauses.append("(created_at, id) < (?, ?)")
        args.extend(cursor)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in con.execute(q, args)]


# ---------------------------------------------------------------- events

def append_event(con, session_id, text, actor="", role="", kind="message",
                 ts=None, time_quality="exact", meta=None):
    if not get_session(con, session_id):
        raise KeyError("session not found: %s" % session_id)
    row = con.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM event WHERE session_id=?",
                      (session_id,)).fetchone()
    seq = row[0]
    eid = new_id("ev")
    now = time.time()
    chash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    with con:
        con.execute(
            """INSERT INTO event(id, session_id, seq, ts, time_quality, actor, role,
                                 kind, text, content_hash, meta_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, session_id, seq, ts, _tq(time_quality), actor, role, kind, text,
             chash, _j(meta or {}), now))
        fts_upsert_event(con, eid, "%s / %s" % (actor, kind), text or "")
    return get_event(con, eid)


def get_event(con, eid):
    return _row(con.execute("SELECT * FROM event WHERE id=?", (eid,)).fetchone())


def list_events(con, session_id, limit=200, cursor_seq=None, kinds=None):
    q = "SELECT * FROM event WHERE session_id=?"
    args = [session_id]
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args.extend(kinds)
    if cursor_seq is not None:
        q += " AND seq < ?"
        args.append(cursor_seq)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    rows = [_row(r) for r in con.execute(q, args)]
    rows.reverse()
    return rows


# ---------------------------------------------------------------- edges

def add_edge(con, src_type, src_id, dst_type, dst_id, rel, note="",
             provenance="explicit", inferred=False):
    if rel not in EDGE_REL:
        raise ValueError("unknown relationship: %s" % rel)
    if provenance not in ("explicit", "inferred"):
        raise ValueError("bad provenance")
    eid = new_id("edg")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO edge(id, src_type, src_id, dst_type, dst_id, rel,
                                created_at, provenance, inferred, note)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (eid, src_type, src_id, dst_type, dst_id, rel, now, provenance,
             1 if inferred else 0, note))
    return eid


def neighbors(con, obj_type, obj_id, rel=None, direction="out", limit=500):
    """One-hop neighborhood. direction: out|in|both."""
    out, inn = [], []
    args = []
    if direction in ("out", "both"):
        q = "SELECT * FROM edge WHERE src_type=? AND src_id=?"
        a = [obj_type, obj_id]
        if rel:
            q += " AND rel=?"; a.append(rel)
        q += " ORDER BY created_at DESC LIMIT ?"; a.append(limit)
        out = [_row(r) for r in con.execute(q, a)]
    if direction in ("in", "both"):
        q = "SELECT * FROM edge WHERE dst_type=? AND dst_id=?"
        a = [obj_type, obj_id]
        if rel:
            q += " AND rel=?"; a.append(rel)
        q += " ORDER BY created_at DESC LIMIT ?"; a.append(limit)
        inn = [_row(r) for r in con.execute(q, a)]
    return {"out": out, "in": inn}


def traverse(con, seeds, rel_allow=None, max_depth=2, per_depth_budget=200):
    """Bounded BFS over edges. Returns nodes + edges + the path for each node.

    seeds: [(obj_type, obj_id)]. Never loads the whole graph.
    Inferred edges are included but flagged on every returned edge.
    """
    if max_depth < 1 or max_depth > 4:
        raise ValueError("max_depth must be 1..4")
    seen = set()
    nodes = {}
    edges = {}
    frontier = []
    for t, i in seeds:
        key = (t, i)
        seen.add(key)
        nodes[key] = {"obj_type": t, "obj_id": i, "depth": 0, "path": []}
        frontier.append(key)
    for depth in range(1, max_depth + 1):
        nxt = []
        for t, i in frontier:
            q = "SELECT * FROM edge WHERE (src_type=? AND src_id=?) OR (dst_type=? AND dst_id=?)"
            a = [t, i, t, i]
            if rel_allow:
                q += " AND rel IN (%s)" % ",".join("?" * len(rel_allow))
                a.extend(rel_allow)
            q += " ORDER BY created_at DESC LIMIT ?"
            a.append(per_depth_budget)
            for r in con.execute(q, a):
                r = dict(r)
                edges[r["id"]] = r
                other = ((r["dst_type"], r["dst_id"]) if (r["src_type"], r["src_id"]) == (t, i)
                         else (r["src_type"], r["src_id"]))
                if other not in seen:
                    seen.add(other)
                    nodes[other] = {"obj_type": other[0], "obj_id": other[1],
                                    "depth": depth,
                                    "path": nodes[(t, i)]["path"] + [r["id"]]}
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    return {
        "nodes": [{"obj_type": t, "obj_id": i, **v} for (t, i), v in nodes.items()],
        "edges": list(edges.values()),
    }


# ---------------------------------------------------------------- decisions

def record_decision(con, title, body="", workspace_id=None, session_id=None,
                    status="active", decided_at=None, time_quality="exact",
                    supersedes_id=None, meta=None):
    did = new_id("dec")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO decision(id, workspace_id, session_id, title, body, status,
                                    decided_at, time_quality, supersedes_id, meta_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (did, workspace_id, session_id, title, body, status, decided_at,
             _tq(time_quality), supersedes_id, _j(meta or {}), now))
        if supersedes_id:
            con.execute("UPDATE decision SET status='superseded' WHERE id=?",
                        (supersedes_id,))
        fts_upsert_record(con, "decision", did, title, body)
    return get_decision(con, did)


def get_decision(con, did):
    return _row(con.execute("SELECT * FROM decision WHERE id=?", (did,)).fetchone())


def list_decisions(con, workspace_id=None, status=None, limit=100):
    q = "SELECT * FROM decision"
    a = []
    c = []
    if workspace_id:
        c.append("workspace_id=?"); a.append(workspace_id)
    if status:
        c.append("status=?"); a.append(status)
    if c:
        q += " WHERE " + " AND ".join(c)
    q += " ORDER BY COALESCE(decided_at, created_at) DESC LIMIT ?"
    a.append(limit)
    return [_row(r) for r in con.execute(q, a)]


# ---------------------------------------------------------------- tasks & questions & protocols
#
# Design decision (2026-10-09, continuity milestone):
# Protocols are represented as tasks with kind="protocol" rather than a
# separate table. Rationale:
# - Protocols share the full task lifecycle: workspace scope, status
#   transitions, revision history (updated_at), FTS indexing, audit trail.
# - Relationships (edges) already link any typed records; no new edge types needed.
# - A separate protocol table would duplicate workspace scoping, status
#   management, FTS, and audit — significant schema + code duplication for
#   no additional integrity.
# - Structured steps live in meta_json["steps"] (list of {text, done}).
#   This keeps the schema stable while allowing structured checklists.
# - Backward compatible: existing "task"/"question" records and queries
#   are unaffected. kind="protocol" is additive.

TASK_KINDS = ("task", "question", "protocol")

def create_task(con, title, kind="task", body="", workspace_id=None,
                status="open", meta=None):
    if kind not in TASK_KINDS:
        raise ValueError("kind must be task|question|protocol")
    tid = new_id("tsk")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO task(id, workspace_id, kind, title, body, status,
                                created_at, updated_at, meta_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (tid, workspace_id, kind, title, body, status, now, now, _j(meta or {})))
        fts_upsert_record(con, "task", tid, title, body)
    return get_task(con, tid)


def get_task(con, tid):
    return _row(con.execute("SELECT * FROM task WHERE id=?", (tid,)).fetchone())


def set_task_status(con, tid, status, note=""):
    return update_task(con, tid, status=status, note=note)


def update_task(con, tid, status=None, title=None, body=None, meta=None, note=""):
    """Partial update of a task. Only provided fields are changed.
    Updates FTS index if title/body change. Audits status changes."""
    t = get_task(con, tid)
    if not t:
        raise KeyError("task not found: %s" % tid)
    sets = []
    args = []
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if title is not None:
        sets.append("title=?")
        args.append(title)
    if body is not None:
        sets.append("body=?")
        args.append(body)
    if meta is not None:
        sets.append("meta_json=?")
        args.append(_j(meta))
    if not sets:
        return t
    sets.append("updated_at=?")
    args.append(time.time())
    args.append(tid)
    now = time.time()
    with con:
        con.execute("UPDATE task SET %s WHERE id=?" % ", ".join(sets), args)
        if title is not None or body is not None:
            fts_upsert_record(con, "task", tid,
                              title if title is not None else t["title"],
                              body if body is not None else t["body"])
    if status is not None and status != t["status"] and note:
        audit(con, "system", "task_status", "task", tid,
              {"status": status, "note": note})
    return get_task(con, tid)


def list_tasks(con, workspace_id=None, kind=None, status=None, limit=100):
    q = "SELECT * FROM task"
    a = []
    c = []
    if workspace_id:
        c.append("workspace_id=?"); a.append(workspace_id)
    if kind:
        c.append("kind=?"); a.append(kind)
    if status:
        c.append("status=?"); a.append(status)
    if c:
        q += " WHERE " + " AND ".join(c)
    q += " ORDER BY updated_at DESC LIMIT ?"
    a.append(limit)
    return [_row(r) for r in con.execute(q, a)]


# ---------------------------------------------------------------- claims & evidence

def create_claim(con, title, body="", workspace_id=None, confidence="unknown",
                 verification_status="unverified", meta=None):
    cid = new_id("clm")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO claim(id, workspace_id, title, body, confidence,
                                 verification_status, created_at, meta_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (cid, workspace_id, title, body, confidence, verification_status,
             now, _j(meta or {})))
        fts_upsert_record(con, "claim", cid, title, body)
    return get_claim(con, cid)


def get_claim(con, cid):
    return _row(con.execute("SELECT * FROM claim WHERE id=?", (cid,)).fetchone())


def add_evidence(con, claim_id, stance="context", artifact_id=None, event_id=None,
                 locator="", note=""):
    if not get_claim(con, claim_id):
        raise KeyError("claim not found: %s" % claim_id)
    eid = new_id("evd")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO evidence(id, claim_id, artifact_id, event_id, locator,
                                    stance, note, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (eid, claim_id, artifact_id, event_id, locator, stance, note, now))
    return eid


def claim_evidence(con, claim_id):
    return [_row(r) for r in
            con.execute("SELECT * FROM evidence WHERE claim_id=? ORDER BY created_at",
                        (claim_id,))]


# ---------------------------------------------------------------- collections

def create_collection(con, title, description="", visibility="private", meta=None):
    cid = new_id("col")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO collection(id, title, description, visibility,
                                      created_at, updated_at, meta_json)
               VALUES(?,?,?,?,?,?,?)""",
            (cid, title, description, visibility, now, now, _j(meta or {})))
    return get_collection(con, cid)


def get_collection(con, cid):
    return _row(con.execute("SELECT * FROM collection WHERE id=?", (cid,)).fetchone())


def add_collection_member(con, collection_id, artifact_id=None,
                          child_collection_id=None, note="", position=None):
    if not get_collection(con, collection_id):
        raise KeyError("collection not found")
    if position is None:
        row = con.execute("SELECT COALESCE(MAX(position),-1)+1 FROM collection_member "
                          "WHERE collection_id=?", (collection_id,)).fetchone()
        position = row[0]
    mid = new_id("mem")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO collection_member(id, collection_id, artifact_id,
                                             child_collection_id, position, note, added_at)
               VALUES(?,?,?,?,?,?,?)""",
            (mid, collection_id, artifact_id, child_collection_id, position, note, now))
        con.execute("UPDATE collection SET updated_at=? WHERE id=?", (now, collection_id))
    return mid


def collection_members(con, collection_id):
    return [_row(r) for r in con.execute(
        "SELECT * FROM collection_member WHERE collection_id=? ORDER BY position, added_at",
        (collection_id,))]


def list_collections(con, limit=100):
    return [_row(r) for r in con.execute(
        "SELECT * FROM collection ORDER BY updated_at DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- checkpoints

def create_checkpoint(con, title, body, workspace_id=None, session_id=None):
    """body: dict referencing canonical ids. Immutable once written."""
    cid = new_id("chk")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO checkpoint(id, workspace_id, session_id, title, created_at,
                                      schema_version, body_json)
               VALUES(?,?,?,?,?,?,?)""",
            (cid, workspace_id, session_id, title, now, _db.SCHEMA_VERSION,
             json.dumps(body)))
    return get_checkpoint(con, cid)


def get_checkpoint(con, cid):
    return _row(con.execute("SELECT * FROM checkpoint WHERE id=?", (cid,)).fetchone())


def list_checkpoints(con, workspace_id=None, limit=50):
    q = "SELECT * FROM checkpoint"
    a = []
    if workspace_id:
        q += " WHERE workspace_id=?"
        a.append(workspace_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    a.append(limit)
    return [_row(r) for r in con.execute(q, a)]


# ---------------------------------------------------------------- handoffs

def create_handoff(con, objective, payload_artifact_id=None, payload_text=None,
                   workspace_id=None, from_session_id=None, to_env="",
                   scope=None, expected=None, meta=None):
    """Payload is immutable: text payloads are stored as artifacts (rev 1, never edited)."""
    hid = new_id("ho")
    now = time.time()
    phash = None
    if payload_text is not None and payload_artifact_id is None:
        from .store import register_artifact
        art = register_artifact(con, _data_dir(con), data=payload_text.encode("utf-8"),
                                filename="handoff-payload.md", mime="text/markdown",
                                visibility="private", source="generated",
                                meta={"handoff": True}, enqueue_extract=False)
        payload_artifact_id = art["id"]
        phash = art["blob_sha256"]
    elif payload_artifact_id:
        from .store import get_artifact
        art = get_artifact(con, payload_artifact_id)
        phash = art["blob_sha256"] if art else None
    with con:
        con.execute(
            """INSERT INTO handoff(id, workspace_id, from_session_id, to_env, objective,
                                   payload_artifact_id, payload_hash, status,
                                   scope_json, expected_json, created_at, updated_at, meta_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hid, workspace_id, from_session_id, to_env, objective,
             payload_artifact_id, phash, "prepared",
             _j(scope or {}), _j(expected or {}), now, now, _j(meta or {})))
    return get_handoff(con, hid)


def _data_dir(con):
    # stored in kv at init by the app layer; fallback: sibling of db file
    return con.execute("SELECT value FROM kv WHERE key='data_dir'").fetchone()[0]


def get_handoff(con, hid):
    return _row(con.execute("SELECT * FROM handoff WHERE id=?", (hid,)).fetchone())


def set_handoff_status(con, hid, status):
    h = get_handoff(con, hid)
    if not h:
        raise KeyError("handoff not found: %s" % hid)
    if status not in HANDOFF_STATUS:
        raise ValueError("bad status")
    allowed = _HANDOFF_FLOW[h["status"]]
    if status not in allowed:
        raise ValueError("illegal transition %s -> %s" % (h["status"], status))
    now = time.time()
    with con:
        con.execute("UPDATE handoff SET status=?, updated_at=? WHERE id=?",
                    (status, now, hid))
    audit(con, "system", "handoff_status", "handoff", hid,
          {"from": h["status"], "to": status})
    return get_handoff(con, hid)


def list_handoffs(con, workspace_id=None, status=None, limit=100):
    q = "SELECT * FROM handoff"
    a = []
    c = []
    if workspace_id:
        c.append("workspace_id=?"); a.append(workspace_id)
    if status:
        c.append("status=?"); a.append(status)
    if c:
        q += " WHERE " + " AND ".join(c)
    q += " ORDER BY updated_at DESC LIMIT ?"
    a.append(limit)
    return [_row(r) for r in con.execute(q, a)]


# ---------------------------------------------------------------- share links

def create_share_link(con, scope_type, scope_id, expires_at=None, note=""):
    token = secrets.token_urlsafe(32)
    thash = hashlib.sha256(token.encode()).hexdigest()
    lid = new_id("lnk")
    now = time.time()
    with con:
        con.execute(
            """INSERT INTO share_link(id, token_hash, scope_type, scope_id,
                                      created_at, expires_at, revoked_at, note)
               VALUES(?,?,?,?,?,?,NULL,?)""",
            (lid, thash, scope_type, scope_id, now, expires_at, note))
    return {"id": lid, "token": token, "expires_at": expires_at}


def resolve_share_token(con, token):
    thash = hashlib.sha256(token.encode()).hexdigest()
    row = con.execute("SELECT * FROM share_link WHERE token_hash=?", (thash,)).fetchone()
    if not row:
        return None
    link = dict(row)
    now = time.time()
    if link["revoked_at"]:
        return None
    if link["expires_at"] and link["expires_at"] < now:
        return None
    return link


def revoke_share_link(con, lid):
    now = time.time()
    with con:
        con.execute("UPDATE share_link SET revoked_at=? WHERE id=?", (now, lid))


# ---------------------------------------------------------------- audit

def audit(con, actor, action, scope_type="", scope_id="", detail=None):
    aid = new_id("aud")
    with con:
        con.execute(
            "INSERT INTO audit_log(id, ts, actor, action, scope_type, scope_id, detail_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (aid, time.time(), actor, action, scope_type, scope_id, _j(detail or {})))
    return aid


def audit_tail(con, limit=200, action=None):
    q = "SELECT * FROM audit_log"
    a = []
    if action:
        q += " WHERE action=?"
        a.append(action)
    q += " ORDER BY ts DESC LIMIT ?"
    a.append(limit)
    return [_row(r) for r in con.execute(q, a)]
