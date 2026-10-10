"""REST API for the substrate: /api/v1/* routes.

Mount contract (see app.py):
    def handle(con, data_dir, req) -> (status:int, content_type:str, body:bytes) | None

Rules enforced here:
  * every /api/v1/* route requires req.authed (403 otherwise)
  * all inputs validated; enums from model.py (EDGE_REL, HANDOFF_STATUS, TIME_Q)
    plus the DESIGN.md vocabularies for event kinds, task/decision/claim/evidence
    statuses, visibility, and extract-job statuses
  * bounded pagination: limit default 50, max 500 (app.page_args)
  * blob filesystem paths are never exposed (artifact views drop source_ref,
    which may hold a legacy host path)
  * secrets are never logged; the share token is shown only in the create response
  * POST create routes honour the "idempotency-key" header:
    key = sha256(method + path + header value); the created object's identity is
    stored in kv as "idem:<key>" and replays return the stored object
  * every mutation writes an audit row via model.audit(con, "owner", ...)
"""
import base64
import hashlib
import json
import time
from datetime import datetime, timezone

from . import app as _app
from . import model as M
from . import store as S
from .db import SCHEMA_VERSION

json_resp = _app.json_resp
err = _app.err
page_args = _app.page_args

# ---------------------------------------------------------------- vocabularies

TASK_STATUS = ("open", "in_progress", "blocked", "done", "cancelled")
QUESTION_STATUS = ("open", "answered", "unresolved")
# Protocols share the task status lifecycle: a protocol is "done" when executed.
PROTOCOL_STATUS = TASK_STATUS
TASK_KINDS = ("task", "question", "protocol")
DECISION_STATUS = ("proposed", "active", "superseded", "reverted")
CLAIM_VERIFICATION = ("unverified", "supported", "partially_supported",
                      "contested", "refuted")
EVIDENCE_STANCE = ("supports", "refutes", "reproduces", "derives_from",
                   "cites", "contradicts", "qualifies", "context")
EVENT_KIND = ("message", "instruction", "tool_result", "upload", "decision_ref",
              "checkpoint_ref", "handoff_ref", "note")
VISIBILITY = ("private", "public-link")
EXTRACT_STATUS = ("pending", "running", "done", "failed", "unavailable")
SHARE_SCOPES = ("collection", "artifact")
OBJ_TYPES = ("workspace", "session", "event", "artifact", "decision", "task",
             "claim", "evidence", "collection", "checkpoint", "handoff")

_SAFE_INLINE_PREFIX = ("image/", "text/", "video/", "audio/")
_SAFE_INLINE_EXACT = ("application/pdf",)


class _ExportTooBig(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _audit(con, action, scope_type="", scope_id="", detail=None):
    M.audit(con, "owner", action, scope_type, scope_id, detail or {})


def _json_body(req):
    """Return (dict, None) or (None, err_response)."""
    if not req.body:
        return {}, None
    try:
        obj = json.loads(req.body.decode("utf-8"))
    except Exception:
        return None, err(400, "bad_json", "request body must be valid JSON")
    if not isinstance(obj, dict):
        return None, err(400, "bad_json", "request body must be a JSON object")
    return obj, None


def _missing(body, *fields):
    return [f for f in fields if body.get(f) in (None, "")]


def _parse_ts(v, what="timestamp"):
    """Accept unix epoch (number or numeric string) or ISO-8601. None/'' -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        raise ValueError("bad %s: %r (use unix epoch or ISO-8601)" % (what, v))


def _get_obj(con, obj_type, obj_id):
    if obj_type == "workspace":
        return M.get_workspace(con, obj_id)
    if obj_type == "session":
        return M.get_session(con, obj_id)
    if obj_type == "event":
        return M.get_event(con, obj_id)
    if obj_type == "artifact":
        return S.get_artifact(con, obj_id)
    if obj_type == "decision":
        return M.get_decision(con, obj_id)
    if obj_type == "task":
        return M.get_task(con, obj_id)
    if obj_type == "claim":
        return M.get_claim(con, obj_id)
    if obj_type == "evidence":
        r = con.execute("SELECT * FROM evidence WHERE id=?", (obj_id,)).fetchone()
        return dict(r) if r else None
    if obj_type == "collection":
        return M.get_collection(con, obj_id)
    if obj_type == "checkpoint":
        return M.get_checkpoint(con, obj_id)
    if obj_type == "handoff":
        return M.get_handoff(con, obj_id)
    return None


def _not_found(obj_type, obj_id):
    return err(404, "not_found", "%s not found: %s" % (obj_type, obj_id))


def _valid_id(iid, prefix):
    """Validate identifier format: prefix_ + alphanumeric."""
    return (isinstance(iid, str) and iid.startswith(prefix + "_") and
            len(iid) > len(prefix) + 1 and
            all(c.isalnum() or c == "_" for c in iid))


def _artifact_view(art):
    """Public artifact shape. Never exposes source_ref (may be a host path)."""
    return {k: art[k] for k in ("id", "blob_sha256", "filename", "mime", "size",
                                "created_at", "visibility", "source", "meta_json")
            if k in art}


def _edge_view(e):
    return dict(e)


# ------------------------------------------------------------- idempotency

def _idem_lookup(con, req):
    """Return (digest_or_None, stored_payload_or_None)."""
    key = req.headers.get("idempotency-key", "")
    if not key:
        return None, None
    digest = hashlib.sha256(
        (req.method + "\n" + req.path + "\n" + key).encode("utf-8")).hexdigest()
    row = con.execute("SELECT value FROM kv WHERE key=?",
                      ("idem:" + digest,)).fetchone()
    if not row:
        return digest, None
    try:
        return digest, json.loads(row[0])
    except Exception:
        return digest, None


def _idem_store(con, digest, obj_type, obj_id):
    if not digest:
        return
    with con:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                    ("idem:" + digest,
                     json.dumps({"type": obj_type, "id": obj_id})))


def _idem_store_ids(con, digest, obj_type, ids):
    if not digest:
        return
    with con:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
                    ("idem:" + digest,
                     json.dumps({"type": obj_type, "ids": ids})))


def _idem_replay(con, payload):
    """Rebuild the create response for a stored idempotency payload, or None."""
    t = payload.get("type")
    if t == "artifact_list":
        arts = []
        for i in payload.get("ids", []):
            a = S.get_artifact(con, i)
            if a:
                arts.append(_artifact_view(a))
        return json_resp(200, {"artifacts": arts, "idempotent_replay": True})
    oid = payload.get("id")
    obj = _get_obj(con, t, oid)
    if obj is None:
        return None
    if t == "artifact":
        obj = _artifact_view(obj)
    return json_resp(200, {t: obj, "idempotent_replay": True})


def _created(con, req, digest, obj_type, obj, status=201):
    _idem_store(con, digest, obj_type, obj["id"])
    return json_resp(status, {obj_type: obj})


# ------------------------------------------------------------- misc

def _parse_seed(s):
    if ":" not in s:
        raise ValueError("seed must look like <type>:<id>, got %r" % (s,))
    t, i = s.split(":", 1)
    t, i = t.strip(), i.strip()
    if t not in OBJ_TYPES or not i:
        raise ValueError("bad seed %r: type must be one of %s" % (s, ",".join(OBJ_TYPES)))
    return t, i


def _in_clause(ids):
    ids = list(ids)
    if not ids:
        return "(NULL)", []
    return "(" + ",".join("?" * len(ids)) + ")", ids


# =====================================================================
# Router
# =====================================================================

def handle(con, data_dir, req):
    """Route /api/v1/* requests. Returns (status, ctype, body) or None."""
    if not req.path.startswith("/api/v1/"):
        return None
    if not req.authed:
        _audit(con, "auth.denied", "route", req.path,
               {"method": req.method})  # no secrets logged
        return err(403, "forbidden", "owner token required")

    segs = [s for s in req.path[len("/api/v1/"):].split("/") if s not in ("", ".")]
    if not segs:
        return err(404, "not_found", "unknown route")

    method = req.method.upper()
    try:
        return _route(con, data_dir, req, method, segs)
    except _ExportTooBig:
        return err(400, "export_too_big",
                   "export refused: more than 200000 events; "
                   "pass workspace_id to partition the export")
    except ValueError as e:
        return err(400, "bad_request", str(e))
    except KeyError as e:
        return err(404, "not_found", str(e).strip("'\""))


def _route(con, data_dir, req, method, segs):
    r0 = segs[0]

    # -- health -------------------------------------------------------
    if r0 == "health" and len(segs) == 1 and method == "GET":
        return json_resp(200, {"ok": True, "schema_version": SCHEMA_VERSION,
                               "time": time.time()})

    # -- workspaces ---------------------------------------------------
    if r0 == "workspaces":
        if len(segs) == 1:
            if method == "GET":
                return json_resp(200, {"workspaces": M.list_workspaces(con)})
            if method == "POST":
                return _ws_create(con, req)
        if len(segs) == 2 and method == "GET":
            ws = M.get_workspace(con, segs[1])
            if not ws:
                return _not_found("workspace", segs[1])
            return json_resp(200, {"workspace": ws})
        if len(segs) == 3 and segs[2] == "context" and method == "GET":
            return _ws_context(con, req, segs[1])

    # -- sessions ------------------------------------------------------
    if r0 == "sessions":
        if len(segs) == 1:
            if method == "GET":
                return _ses_list(con, req)
            if method == "POST":
                return _ses_create(con, req)
        if len(segs) == 2 and method == "GET":
            s = M.get_session(con, segs[1])
            if not s:
                return _not_found("session", segs[1])
            return json_resp(200, {"session": s})
        if len(segs) == 3 and segs[2] == "events":
            if method == "GET":
                return _ev_list(con, req, segs[1])
            if method == "POST":
                return _ev_append(con, req, segs[1])

    # -- artifacts ------------------------------------------------------
    if r0 == "artifacts":
        if len(segs) == 1:
            if method == "GET":
                return _art_list(con, req)
            if method == "POST":
                return _art_upload(con, data_dir, req)
        if len(segs) == 2 and method == "GET":
            return _art_get(con, segs[1])
        if len(segs) == 3 and segs[2] == "content" and method == "GET":
            return _art_content(con, data_dir, segs[1])
        if len(segs) == 3 and segs[2] == "revisions" and method == "GET":
            return _art_revisions(con, segs[1])
        if len(segs) == 3 and segs[2] == "revisions" and method == "POST":
            return _art_add_revision(con, data_dir, req, segs[1])
        if len(segs) == 3 and segs[2] == "edges" and method == "GET":
            return _art_edges(con, req, segs[1])
        if len(segs) == 4 and segs[2] == "extract" and segs[3] == "retry" \
                and method == "POST":
            return _extract_retry(con, segs[1])

    # -- edges -----------------------------------------------------------
    if r0 == "edges" and len(segs) == 1 and method == "POST":
        return _edge_create(con, req)
    if r0 == "traverse" and len(segs) == 1 and method == "GET":
        return _traverse(con, req)

    # -- decisions --------------------------------------------------------
    if r0 == "decisions":
        if len(segs) == 1:
            if method == "GET":
                return _dec_list(con, req)
            if method == "POST":
                return _dec_create(con, req)
        if len(segs) == 2 and method == "GET":
            d = M.get_decision(con, segs[1])
            if not d:
                return _not_found("decision", segs[1])
            return json_resp(200, {"decision": d})

    # -- tasks -------------------------------------------------------------
    if r0 == "tasks":
        if len(segs) == 1:
            if method == "GET":
                return _task_list(con, req)
            if method == "POST":
                return _task_create(con, req)
        if len(segs) == 2 and method == "GET":
            return _task_get(con, req, segs[1])
        if len(segs) == 2 and method == "PATCH":
            return _task_patch(con, req, segs[1])

    # -- claims & evidence --------------------------------------------------
    if r0 == "claims":
        if len(segs) == 1:
            if method == "GET":
                return _claim_list(con, req)
            if method == "POST":
                return _claim_create(con, req)
        if len(segs) == 2 and method == "GET":
            return _claim_get(con, segs[1])
        if len(segs) == 3 and segs[2] == "evidence" and method == "POST":
            return _evidence_add(con, req, segs[1])

    # -- collections ----------------------------------------------------------
    if r0 == "collections":
        if len(segs) == 1:
            if method == "GET":
                return _col_list(con, req)
            if method == "POST":
                return _col_create(con, req)
        if len(segs) == 2 and method == "GET":
            return _col_get(con, segs[1])
        if len(segs) == 3 and segs[2] == "members" and method == "POST":
            return _col_add_member(con, req, segs[1])
        if len(segs) == 3 and segs[2] == "manifest" and method == "GET":
            return _col_manifest(con, segs[1])

    # -- checkpoints ------------------------------------------------------------
    if r0 == "checkpoints":
        if len(segs) == 1:
            if method == "GET":
                return _chk_list(con, req)
            if method == "POST":
                return _chk_create(con, req)
        if len(segs) == 2 and method == "GET":
            return _chk_get(con, segs[1])

    # -- handoffs ------------------------------------------------------------------
    if r0 == "handoffs":
        if len(segs) == 1:
            if method == "GET":
                return _ho_list(con, req)
            if method == "POST":
                return _ho_create(con, data_dir, req)
        if len(segs) == 2 and method == "GET":
            return _ho_get(con, segs[1])
        if len(segs) == 3 and segs[2] == "status" and method == "POST":
            return _ho_status(con, req, segs[1])
        if len(segs) == 3 and segs[2] == "payload" and method == "GET":
            return _ho_payload(con, data_dir, segs[1])

    # -- search / context -------------------------------------------------------------
    if r0 == "search" and len(segs) == 1 and method == "GET":
        return _search(con, data_dir, req)
    if r0 == "context" and len(segs) == 1 and method == "POST":
        return _context(con, data_dir, req)

    # -- export --------------------------------------------------------------------------
    if r0 == "export" and len(segs) == 1 and method == "GET":
        return _export(con, req)

    # -- share ------------------------------------------------------------------------------
    if r0 == "share":
        if len(segs) == 1 and method == "POST":
            return _share_create(con, req)
        if len(segs) == 3 and segs[2] == "revoke" and method == "POST":
            return _share_revoke(con, segs[1])

    # -- extract jobs --------------------------------------------------------------------------
    if r0 == "extract" and len(segs) == 2 and segs[1] == "jobs" and method == "GET":
        return _extract_jobs(con, req)

    # -- audit --------------------------------------------------------------------------------------
    if r0 == "audit" and len(segs) == 1 and method == "GET":
        return _audit_list(con, req)

    return err(404, "not_found", "unknown route: %s" % req.path)


# =====================================================================
# Workspaces
# =====================================================================

def _ws_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "title")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    ws = M.create_workspace(con, body["title"], body.get("description", ""), meta)
    _audit(con, "workspace.create", "workspace", ws["id"], {"title": ws["title"]})
    return _created(con, req, digest, "workspace", ws)


def _ws_context(con, req, wid):
    """Read-only workspace context package for continuity.

    Returns a bounded, structured package: protocols, tasks by status,
    decisions, graph relationships, and recommended next actions.
    Limits and omissions are explicit — nothing is silently truncated.
    """
    if not _valid_id(wid, "ws"):
        return err(400, "bad_request", "invalid workspace id format")
    ws = M.get_workspace(con, wid)
    if not ws:
        return _not_found("workspace", wid)
    limit, _c = page_args(req)
    limit = min(limit, 50)  # hard cap for context packages

    protocols = M.list_tasks(con, workspace_id=wid, kind="protocol", limit=limit)
    tasks = M.list_tasks(con, workspace_id=wid, kind="task", limit=limit)
    questions = M.list_tasks(con, workspace_id=wid, kind="question", limit=limit)
    decisions = M.list_decisions(con, workspace_id=wid, limit=limit)

    # Graph relationships: edges touching any task/protocol in this workspace
    task_ids = {t["id"] for t in protocols + tasks + questions}
    edges = []
    for tid in sorted(task_ids):
        nb = M.neighbors(con, "task", tid, direction="both", limit=50)
        edges.extend(nb.get("out", []))
        edges.extend(nb.get("in", []))
    # Deduplicate edges by id
    seen = set()
    uniq_edges = []
    for e in edges:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            uniq_edges.append(e)

    # Next actions: open/in_progress/blocked tasks+protocols, most recent first
    actionable = [t for t in protocols + tasks
                  if t["status"] in ("open", "in_progress", "blocked")]
    actionable.sort(key=lambda t: t["updated_at"], reverse=True)

    # Explicit truncation flags
    truncated = {
        "protocols": len(protocols) >= limit,
        "tasks": len(tasks) >= limit,
        "questions": len(questions) >= limit,
        "decisions": len(decisions) >= limit,
    }

    pkg = {
        "workspace": ws,
        "protocols": protocols,
        "tasks": tasks,
        "questions": questions,
        "decisions": decisions,
        "edges": uniq_edges,
        "next_actions": [
            {"id": t["id"], "kind": t["kind"], "title": t["title"],
             "status": t["status"], "updated_at": t["updated_at"]}
            for t in actionable[:10]
        ],
        "counts": {
            "protocols": len(protocols),
            "tasks": len(tasks),
            "questions": len(questions),
            "decisions": len(decisions),
            "edges": len(uniq_edges),
        },
        "truncated": truncated,
        "limits": {"per_section": limit, "next_actions": 10, "edges_per_task": 50},
    }
    _audit(con, "workspace.context", "workspace", wid,
           {"protocols": len(protocols), "tasks": len(tasks)})
    return json_resp(200, {"context": pkg})


# =====================================================================
# Sessions & events
# =====================================================================

def _ses_list(con, req):
    limit, cursor_raw = page_args(req)
    wid = req.q("workspace_id", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    cursor = None
    if cursor_raw:
        try:
            ts_s, cid = cursor_raw.split(",", 1)
            cursor = (float(ts_s), cid)
        except Exception:
            return err(400, "bad_request",
                       "cursor must look like <created_at>,<id>")
    sessions = M.list_sessions(con, workspace_id=wid, limit=limit + 1, cursor=cursor)
    next_cursor = None
    if len(sessions) > limit:
        # sessions are newest-first; the page is the first `limit` rows and
        # the cursor anchors on the page's oldest row (not the extra probe row)
        last = sessions[limit - 1]
        next_cursor = "%r,%s" % (last["created_at"], last["id"])
        sessions = sessions[:limit]
    return json_resp(200, {"sessions": sessions, "limit": limit,
                           "next_cursor": next_cursor})


def _ses_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    wid = body.get("workspace_id")
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    tq = body.get("time_quality", "exact")
    if tq not in M.TIME_Q:
        return err(400, "bad_request",
                   "time_quality must be one of %s" % ",".join(M.TIME_Q))
    try:
        started_at = _parse_ts(body.get("started_at"), "started_at")
        ended_at = _parse_ts(body.get("ended_at"), "ended_at")
    except ValueError as ve:
        return err(400, "bad_request", str(ve))
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    s = M.create_session(con, workspace_id=wid, title=body.get("title", ""),
                         started_at=started_at, ended_at=ended_at,
                         time_quality=tq, source=body.get("source", "manual"),
                         source_id=body.get("source_id"), meta=meta)
    _audit(con, "session.create", "session", s["id"],
           {"workspace_id": wid, "title": s["title"]})
    return _created(con, req, digest, "session", s)


def _ev_list(con, req, sid):
    if not M.get_session(con, sid):
        return _not_found("session", sid)
    limit, cursor_raw = page_args(req)
    kinds = None
    if req.q("kinds", ""):
        kinds = [k.strip() for k in req.q("kinds").split(",") if k.strip()]
        bad = [k for k in kinds if k not in EVENT_KIND]
        if bad:
            return err(400, "bad_request",
                       "unknown event kind(s): %s (allowed: %s)"
                       % (",".join(bad), ",".join(EVENT_KIND)))
    cursor_seq = None
    if cursor_raw:
        try:
            cursor_seq = int(cursor_raw)
        except ValueError:
            return err(400, "bad_request", "cursor must be an event seq integer")
    events = M.list_events(con, sid, limit=limit + 1, cursor_seq=cursor_seq,
                           kinds=kinds)
    next_cursor = None
    if len(events) > limit:
        # model returns events oldest-first; the page is the NEWEST `limit`
        # rows. Drop the oldest probe row, then anchor the cursor on the
        # page's oldest row (cursor is exclusive: seq < cursor).
        events = events[1:]
        next_cursor = str(events[0]["seq"])
    return json_resp(200, {"events": events, "limit": limit,
                           "next_cursor": next_cursor})


def _ev_append(con, req, sid):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    if not M.get_session(con, sid):
        return _not_found("session", sid)
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "text")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    kind = body.get("kind", "message")
    if kind not in EVENT_KIND:
        return err(400, "bad_request",
                   "kind must be one of %s" % ",".join(EVENT_KIND))
    tq = body.get("time_quality", "exact")
    if tq not in M.TIME_Q:
        return err(400, "bad_request",
                   "time_quality must be one of %s" % ",".join(M.TIME_Q))
    try:
        ts = _parse_ts(body.get("ts"), "ts")
    except ValueError as ve:
        return err(400, "bad_request", str(ve))
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    try:
        ev = M.append_event(con, sid, body["text"], actor=body.get("actor", ""),
                            role=body.get("role", ""), kind=kind, ts=ts,
                            time_quality=tq, meta=meta)
    except KeyError as ke:
        return err(404, "not_found", str(ke).strip("'\""))
    _audit(con, "event.append", "event", ev["id"],
           {"session_id": sid, "kind": kind, "text_len": len(body["text"])})
    return _created(con, req, digest, "event", ev)


# =====================================================================
# Artifacts
# =====================================================================

# Maximum bytes for a single artifact upload (consistent with the legacy
# upload-tool policy of 25MB per file). Distinct from MAX_BODY (the HTTP
# request limit): a request may carry several files as long as each file
# and the total request stay within their respective limits.
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


def _art_upload(con, data_dir, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    visibility = req.q("visibility", "private")
    if visibility not in VISIBILITY:
        return err(400, "bad_request",
                   "visibility must be one of %s" % ",".join(VISIBILITY))
    wid = req.q("workspace_id", "") or None
    sid = req.q("session_id", "") or None
    eid = req.q("event_id", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if sid and not M.get_session(con, sid):
        return _not_found("session", sid)
    if eid and not M.get_event(con, eid):
        return _not_found("event", eid)
    files = [(n, fn, ct, data) for (n, fn, ct, data) in req.form if fn]
    if not files:
        return err(400, "bad_request",
                   "multipart body with at least one file field is required")
    # Per-file size enforcement BEFORE any artifact/revision/blob is written,
    # so a rejected upload leaves no partial state behind.
    for (_name, filename, _ctype, data) in files:
        if len(data) > MAX_ARTIFACT_BYTES:
            return err(413, "file_too_large",
                       "file %r is %d bytes; maximum per artifact is %d bytes" %
                       (filename, len(data), MAX_ARTIFACT_BYTES))
    arts = []
    for (_name, filename, ctype, data) in files:
        art = S.register_artifact(con, data_dir, data=data, filename=filename,
                                  mime=ctype, visibility=visibility,
                                  source="upload")
        arts.append(art)
        if wid:
            M.add_edge(con, "workspace", wid, "artifact", art["id"], "contains",
                       note="upload linked at ingest")
        if sid:
            M.add_edge(con, "session", sid, "artifact", art["id"], "references",
                       note="upload linked at ingest")
        if eid:
            M.add_edge(con, "event", eid, "artifact", art["id"], "introduces",
                       note="upload linked at ingest")
        _audit(con, "artifact.upload", "artifact", art["id"],
               {"filename": filename, "mime": art["mime"], "size": art["size"],
                "visibility": visibility, "workspace_id": wid,
                "session_id": sid, "event_id": eid})
    ids = [a["id"] for a in arts]
    _idem_store_ids(con, digest, "artifact_list", ids)
    return json_resp(201, {"artifacts": [_artifact_view(a) for a in arts]})


def _art_list(con, req):
    limit, _cursor = page_args(req)
    wid = req.q("workspace_id", "") or None
    mime_prefix = req.q("mime_prefix", "") or None
    visibility = req.q("visibility", "") or None
    filename = req.q("filename", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if visibility and visibility not in VISIBILITY:
        return err(400, "bad_request",
                   "visibility must be one of %s" % ",".join(VISIBILITY))
    try:
        since = _parse_ts(req.q("since", "") or None, "since")
        until = _parse_ts(req.q("until", "") or None, "until")
    except ValueError as ve:
        return err(400, "bad_request", str(ve))

    joins, wheres, args = [], [], []
    if wid:
        joins.append(
            "JOIN edge ews ON ews.dst_type='artifact' AND ews.dst_id=artifact.id "
            "AND ews.src_type='workspace' AND ews.src_id=? "
            "AND ews.rel IN ('contains','references')")
        args.append(wid)
    if mime_prefix:
        wheres.append("artifact.mime LIKE ?")
        args.append(mime_prefix + "%")
    if visibility:
        wheres.append("artifact.visibility=?")
        args.append(visibility)
    if since is not None:
        wheres.append("artifact.created_at>=?")
        args.append(since)
    if until is not None:
        wheres.append("artifact.created_at<=?")
        args.append(until)
    if filename:
        wheres.append("artifact.filename LIKE ?")
        args.append("%" + filename + "%")
    q = "SELECT DISTINCT artifact.* FROM artifact"
    if joins:
        q += " " + " ".join(joins)
    if wheres:
        q += " WHERE " + " AND ".join(wheres)
    q += " ORDER BY artifact.created_at DESC LIMIT ?"
    args.append(limit)
    rows = [_artifact_view(dict(r)) for r in con.execute(q, args)]
    return json_resp(200, {"artifacts": rows, "limit": limit})


def _art_get(con, aid):
    art = S.get_artifact(con, aid)
    if not art:
        return _not_found("artifact", aid)
    return json_resp(200, {"artifact": _artifact_view(art)})


def _art_content(con, data_dir, aid):
    art = S.get_artifact(con, aid)
    if not art:
        return _not_found("artifact", aid)
    try:
        data = S.read_artifact_bytes(con, data_dir, art)
    except FileNotFoundError:
        return err(404, "unavailable",
                   "bytes unavailable for artifact %s" % aid)
    mime = art.get("mime") or "application/octet-stream"
    ctype = mime
    if ctype.startswith("text/") and "charset" not in ctype:
        ctype += "; charset=utf-8"
    # Content-Disposition: safe types (images, text, pdf, av) render inline;
    # everything else (HTML, SVG, XML, ...) downloads as an attachment so
    # stored content can never execute as the serving origin.
    safe = (mime.startswith(("image/", "text/", "video/", "audio/")) or
            mime == "application/pdf")
    disp = "inline" if safe else "attachment"
    # RFC 6266: quote the filename, strip path separators
    fname = (art.get("filename") or "artifact").replace("\\", "_").replace(
        '"', "_").replace("/", "_")[:200]
    headers = {"Content-Disposition": '%s; filename="%s"' % (disp, fname)}
    return 200, ctype, data, headers


def _art_revisions(con, aid):
    if not S.get_artifact(con, aid):
        return _not_found("artifact", aid)
    revs = S.revisions(con, aid)
    return json_resp(200, {"artifact_id": aid, "revisions": revs})


def _art_add_revision(con, data_dir, req, aid):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    if not S.get_artifact(con, aid):
        return _not_found("artifact", aid)
    files = [(fn, data) for (_n, fn, _ct, data) in req.form if fn]
    if not files:
        return err(400, "bad_request",
                   "multipart body with a file field is required")
    filename, data = files[0]
    change_note = req.q("change_note", "")
    if not change_note:
        for (n, fn, _ct, payload) in req.form:
            if n == "change_note" and not fn:
                try:
                    change_note = payload.decode("utf-8")
                except Exception:
                    change_note = ""
                break
    try:
        rev_no = S.add_revision(con, data_dir, aid, data=data,
                                change_note=change_note)
    except KeyError as ke:
        return err(404, "not_found", str(ke).strip("'\""))
    _audit(con, "artifact.revision", "artifact", aid,
           {"rev_no": rev_no, "size": len(data),
            "change_note_len": len(change_note)})
    _idem_store(con, digest, "artifact", aid)
    art = S.get_artifact(con, aid)
    return json_resp(201, {"artifact": _artifact_view(art), "rev_no": rev_no,
                           "filename": filename})


def _art_edges(con, req, aid):
    if not S.get_artifact(con, aid):
        return _not_found("artifact", aid)
    direction = req.q("direction", "both")
    if direction not in ("out", "in", "both"):
        return err(400, "bad_request", "direction must be out|in|both")
    rel = req.q("rel", "") or None
    if rel and rel not in M.EDGE_REL:
        return err(400, "bad_request",
                   "rel must be one of %s" % ",".join(M.EDGE_REL))
    limit, _c = page_args(req)
    nb = M.neighbors(con, "artifact", aid, rel=rel, direction=direction,
                     limit=limit)
    return json_resp(200, {"artifact_id": aid, "direction": direction,
                           "out": [_edge_view(e) for e in nb["out"]],
                           "in": [_edge_view(e) for e in nb["in"]]})


def _extract_retry(con, aid):
    art = S.get_artifact(con, aid)
    if not art:
        return _not_found("artifact", aid)
    job = con.execute("SELECT * FROM extract_job WHERE artifact_id=? "
                      "ORDER BY created_at DESC LIMIT 1", (aid,)).fetchone()
    if not job:
        return err(404, "not_found", "no extract job for artifact %s" % aid)
    job = dict(job)
    now = time.time()
    with con:
        con.execute("UPDATE extract_job SET status='pending', last_error='', "
                    "attempts=0, updated_at=? WHERE id=?", (now, job["id"]))
    _audit(con, "extract.retry", "artifact", aid,
           {"job_id": job["id"], "was": job["status"]})
    job["status"] = "pending"
    job["last_error"] = ""
    job["updated_at"] = now
    return json_resp(200, {"job": job})


# =====================================================================
# Edges & traversal
# =====================================================================

def _edge_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "src_type", "src_id", "dst_type", "dst_id", "rel")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    st, sid, dt, did = (body["src_type"], body["src_id"],
                        body["dst_type"], body["dst_id"])
    for t in (st, dt):
        if t not in OBJ_TYPES:
            return err(400, "bad_request",
                       "object type must be one of %s" % ",".join(OBJ_TYPES))
    if _get_obj(con, st, sid) is None:
        return _not_found(st, sid)
    if _get_obj(con, dt, did) is None:
        return _not_found(dt, did)
    rel = body["rel"]
    if rel not in M.EDGE_REL:
        return err(400, "bad_request",
                   "rel must be one of %s" % ",".join(M.EDGE_REL))
    inferred = bool(body.get("inferred", False))
    provenance = body.get("provenance", "explicit")
    if inferred:
        provenance = "inferred"
    if provenance not in ("explicit", "inferred"):
        return err(400, "bad_request", "provenance must be explicit|inferred")
    eid = M.add_edge(con, st, sid, dt, did, rel, note=body.get("note", ""),
                     provenance=provenance, inferred=inferred)
    _audit(con, "edge.create", "edge", eid,
           {"src": "%s:%s" % (st, sid), "dst": "%s:%s" % (dt, did),
            "rel": rel, "inferred": inferred})
    edge = dict(con.execute("SELECT * FROM edge WHERE id=?", (eid,)).fetchone())
    _idem_store(con, digest, "edge", eid)
    return json_resp(201, {"edge": edge})


def _traverse(con, req):
    seed_raw = req.q("seed", "")
    if not seed_raw:
        return err(400, "bad_request",
                   "seed is required (comma-separated <type>:<id> values)")
    try:
        seeds = [_parse_seed(s) for s in seed_raw.split(",") if s.strip()]
    except ValueError as ve:
        return err(400, "bad_request", str(ve))
    if not seeds:
        return err(400, "bad_request", "at least one seed is required")
    rel_allow = None
    if req.q("rel", ""):
        rel_allow = [r.strip() for r in req.q("rel").split(",") if r.strip()]
        bad = [r for r in rel_allow if r not in M.EDGE_REL]
        if bad:
            return err(400, "bad_request",
                       "unknown rel(s): %s (allowed: %s)"
                       % (",".join(bad), ",".join(M.EDGE_REL)))
    try:
        max_depth = int(req.q("max_depth", "2"))
    except ValueError:
        return err(400, "bad_request", "max_depth must be an integer 1..4")
    if max_depth < 1 or max_depth > 4:
        return err(400, "bad_request", "max_depth must be 1..4")
    for t, i in seeds:
        if _get_obj(con, t, i) is None:
            return _not_found(t, i)
    result = M.traverse(con, seeds, rel_allow=rel_allow, max_depth=max_depth)
    # Annotate nodes with their canonical record where cheap to fetch; the
    # traversal itself stays bounded (model.traverse caps depth and breadth).
    for node in result["nodes"]:
        obj = _get_obj(con, node["obj_type"], node["obj_id"])
        if node["obj_type"] == "artifact" and obj:
            obj = _artifact_view(obj)
        node["record"] = obj
    return json_resp(200, {"seeds": [{"obj_type": t, "obj_id": i}
                                    for t, i in seeds],
                           "max_depth": max_depth,
                           "rel": rel_allow,
                           "nodes": result["nodes"],
                           "edges": [_edge_view(e) for e in result["edges"]]})


# =====================================================================
# Decisions
# =====================================================================

def _dec_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "title")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    wid = body.get("workspace_id")
    sid = body.get("session_id")
    sup = body.get("supersedes_id")
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if sid and not M.get_session(con, sid):
        return _not_found("session", sid)
    if sup and not M.get_decision(con, sup):
        return _not_found("decision", sup)
    status = body.get("status", "active")
    if status not in DECISION_STATUS:
        return err(400, "bad_request",
                   "status must be one of %s" % ",".join(DECISION_STATUS))
    tq = body.get("time_quality", "exact")
    if tq not in M.TIME_Q:
        return err(400, "bad_request",
                   "time_quality must be one of %s" % ",".join(M.TIME_Q))
    try:
        decided_at = _parse_ts(body.get("decided_at"), "decided_at")
    except ValueError as ve:
        return err(400, "bad_request", str(ve))
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    d = M.record_decision(con, body["title"], body=body.get("body", ""),
                          workspace_id=wid, session_id=sid, status=status,
                          decided_at=decided_at, time_quality=tq,
                          supersedes_id=sup, meta=meta)
    _audit(con, "decision.record", "decision", d["id"],
           {"title": d["title"], "status": status, "supersedes_id": sup})
    return _created(con, req, digest, "decision", d)


def _dec_list(con, req):
    limit, _c = page_args(req)
    wid = req.q("workspace_id", "") or None
    status = req.q("status", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if status and status not in DECISION_STATUS:
        return err(400, "bad_request",
                   "status must be one of %s" % ",".join(DECISION_STATUS))
    return json_resp(200, {"decisions": M.list_decisions(con, workspace_id=wid,
                                                         status=status,
                                                         limit=limit)})


# =====================================================================
# Tasks & questions
# =====================================================================

def _task_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "title")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    kind = body.get("kind", "task")
    if kind not in TASK_KINDS:
        return err(400, "bad_request", "kind must be task|question|protocol")
    wid = body.get("workspace_id")
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    t = M.create_task(con, body["title"], kind=kind, body=body.get("body", ""),
                      workspace_id=wid, meta=meta)
    _audit(con, "task.create", "task", t["id"],
           {"title": t["title"], "kind": kind})
    return _created(con, req, digest, "task", t)


def _task_list(con, req):
    limit, _c = page_args(req)
    wid = req.q("workspace_id", "") or None
    kind = req.q("kind", "") or None
    status = req.q("status", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if kind and kind not in TASK_KINDS:
        return err(400, "bad_request", "kind must be task|question|protocol")
    if status and status not in TASK_STATUS + QUESTION_STATUS:
        return err(400, "bad_request",
                   "status must be one of %s" % ",".join(TASK_STATUS + QUESTION_STATUS))
    return json_resp(200, {"tasks": M.list_tasks(con, workspace_id=wid, kind=kind,
                                                 status=status, limit=limit)})


def _task_get(con, req, tid):
    if not _valid_id(tid, "tsk"):
        return err(400, "bad_request", "invalid task id format")
    t = M.get_task(con, tid)
    if not t:
        return _not_found("task", tid)
    # Include provenance: edges in/out
    nb = M.neighbors(con, "task", tid, direction="both", limit=100)
    return json_resp(200, {"task": t, "edges_out": nb.get("out", []),
                           "edges_in": nb.get("in", [])})


def _task_patch(con, req, tid):
    if not _valid_id(tid, "tsk"):
        return err(400, "bad_request", "invalid task id format")
    t = M.get_task(con, tid)
    if not t:
        return _not_found("task", tid)
    body, e = _json_body(req)
    if e:
        return e
    # Partial update: status, title, body, meta are all optional.
    # At least one must be present.
    patchable = ("status", "title", "body", "meta")
    if not any(k in body for k in patchable):
        return err(400, "bad_request",
                   "at least one of status|title|body|meta is required")
    updates = {}
    if "status" in body:
        status = body["status"]
        allowed = TASK_STATUS if t["kind"] in ("task", "protocol") else QUESTION_STATUS
        if status not in allowed:
            return err(400, "bad_request",
                       "status for kind=%s must be one of %s"
                       % (t["kind"], ",".join(allowed)))
        updates["status"] = status
    if "title" in body:
        title = body["title"]
        if not isinstance(title, str) or not title.strip():
            return err(400, "bad_request", "title must be a non-empty string")
        updates["title"] = title.strip()
    if "body" in body:
        b = body["body"]
        if not isinstance(b, str):
            return err(400, "bad_request", "body must be a string")
        updates["body"] = b
    if "meta" in body:
        meta = body["meta"]
        if not isinstance(meta, dict):
            return err(400, "bad_request", "meta must be an object")
        updates["meta"] = meta
    try:
        t2 = M.update_task(con, tid, note=body.get("note", ""), **updates)
    except KeyError as ke:
        return err(404, "not_found", str(ke).strip("'\""))
    _audit(con, "task.patch", "task", tid,
           {"from_status": t["status"], "fields": sorted(updates.keys())})
    return json_resp(200, {"task": t2})


# =====================================================================
# Claims & evidence
# =====================================================================

def _claim_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "title")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    wid = body.get("workspace_id")
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    vs = body.get("verification_status", "unverified")
    if vs not in CLAIM_VERIFICATION:
        return err(400, "bad_request",
                   "verification_status must be one of %s" % ",".join(CLAIM_VERIFICATION))
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    c = M.create_claim(con, body["title"], body=body.get("body", ""),
                       workspace_id=wid,
                       confidence=body.get("confidence", "unknown"),
                       verification_status=vs, meta=meta)
    _audit(con, "claim.create", "claim", c["id"], {"title": c["title"]})
    return _created(con, req, digest, "claim", c)


def _claim_list(con, req):
    limit, _c = page_args(req)
    wid = req.q("workspace_id", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    q = "SELECT * FROM claim"
    args = []
    if wid:
        q += " WHERE workspace_id=?"
        args.append(wid)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in con.execute(q, args)]
    return json_resp(200, {"claims": rows})


def _claim_get(con, cid):
    c = M.get_claim(con, cid)
    if not c:
        return _not_found("claim", cid)
    c["evidence"] = M.claim_evidence(con, cid)
    return json_resp(200, {"claim": c})


def _evidence_add(con, req, cid):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    if not M.get_claim(con, cid):
        return _not_found("claim", cid)
    body, e = _json_body(req)
    if e:
        return e
    stance = body.get("stance", "context")
    if stance not in EVIDENCE_STANCE:
        return err(400, "bad_request",
                   "stance must be one of %s" % ",".join(EVIDENCE_STANCE))
    aid = body.get("artifact_id")
    evid = body.get("event_id")
    if aid and not S.get_artifact(con, aid):
        return _not_found("artifact", aid)
    if evid and not M.get_event(con, evid):
        return _not_found("event", evid)
    if not aid and not evid:
        return err(400, "bad_request",
                   "at least one of artifact_id or event_id is required")
    try:
        evid_id = M.add_evidence(con, cid, stance=stance, artifact_id=aid,
                                 event_id=evid, locator=body.get("locator", ""),
                                 note=body.get("note", ""))
    except KeyError as ke:
        return err(404, "not_found", str(ke).strip("'\""))
    _audit(con, "evidence.add", "evidence", evid_id,
           {"claim_id": cid, "stance": stance,
            "artifact_id": aid, "event_id": evid})
    evd = dict(con.execute("SELECT * FROM evidence WHERE id=?", (evid_id,)).fetchone())
    _idem_store(con, digest, "evidence", evid_id)
    return json_resp(201, {"evidence": evd})


# =====================================================================
# Collections
# =====================================================================

def _col_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "title")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    visibility = body.get("visibility", "private")
    if visibility not in VISIBILITY:
        return err(400, "bad_request",
                   "visibility must be one of %s" % ",".join(VISIBILITY))
    meta = body.get("meta", {})
    if not isinstance(meta, dict):
        return err(400, "bad_request", "meta must be an object")
    c = M.create_collection(con, body["title"],
                            description=body.get("description", ""),
                            visibility=visibility, meta=meta)
    _audit(con, "collection.create", "collection", c["id"], {"title": c["title"]})
    return _created(con, req, digest, "collection", c)


def _col_list(con, req):
    limit, _c = page_args(req)
    return json_resp(200, {"collections": M.list_collections(con, limit=limit)})


def _member_view(con, m):
    m = dict(m)
    if m.get("artifact_id"):
        art = S.get_artifact(con, m["artifact_id"])
        m["artifact"] = _artifact_view(art) if art else None
    if m.get("child_collection_id"):
        child = M.get_collection(con, m["child_collection_id"])
        m["child_collection"] = ({"id": child["id"], "title": child["title"],
                                  "description": child.get("description", "")}
                                 if child else None)
    return m


def _col_get(con, cid):
    c = M.get_collection(con, cid)
    if not c:
        return _not_found("collection", cid)
    members = [_member_view(con, m) for m in M.collection_members(con, cid)]
    return json_resp(200, {"collection": c, "members": members,
                           "member_count": len(members)})


def _col_add_member(con, req, cid):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    if not M.get_collection(con, cid):
        return _not_found("collection", cid)
    body, e = _json_body(req)
    if e:
        return e
    aid = body.get("artifact_id")
    child = body.get("child_collection_id")
    if bool(aid) == bool(child):
        return err(400, "bad_request",
                   "exactly one of artifact_id or child_collection_id is required")
    if aid and not S.get_artifact(con, aid):
        return _not_found("artifact", aid)
    if child:
        if not M.get_collection(con, child):
            return _not_found("collection", child)
        if child == cid:
            return err(400, "bad_request",
                       "a collection cannot contain itself")
    position = body.get("position")
    if position is not None:
        try:
            position = float(position)
        except (TypeError, ValueError):
            return err(400, "bad_request", "position must be a number")
    try:
        mid = M.add_collection_member(con, cid, artifact_id=aid,
                                      child_collection_id=child,
                                      note=body.get("note", ""),
                                      position=position)
    except KeyError as ke:
        return err(404, "not_found", str(ke).strip("'\""))
    _audit(con, "collection.member_add", "collection", cid,
           {"member_id": mid, "artifact_id": aid, "child_collection_id": child})
    member = dict(con.execute("SELECT * FROM collection_member WHERE id=?",
                              (mid,)).fetchone())
    _idem_store(con, digest, "collection_member", mid)
    return json_resp(201, {"member": _member_view(con, member)})


def _col_manifest(con, cid):
    c = M.get_collection(con, cid)
    if not c:
        return _not_found("collection", cid)
    members = []
    for m in M.collection_members(con, cid):
        m = dict(m)
        entry = {"member_id": m["id"], "position": m["position"],
                 "note": m["note"], "added_at": m["added_at"]}
        if m.get("artifact_id"):
            art = S.get_artifact(con, m["artifact_id"])
            entry["artifact"] = _artifact_view(art) if art else None
        if m.get("child_collection_id"):
            child = M.get_collection(con, m["child_collection_id"])
            entry["child_collection"] = ({"id": child["id"], "title": child["title"]}
                                         if child else None)
        members.append(entry)
    return json_resp(200, {"format": "substrate-collection-manifest",
                           "version": 1,
                           "collection": c,
                           "members": members,
                           "exported_at": time.time()})


# =====================================================================
# Checkpoints
# =====================================================================

def _chk_create(con, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "title", "body")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    if not isinstance(body["body"], dict):
        return err(400, "bad_request",
                   "body must be an object referencing canonical ids")
    wid = body.get("workspace_id")
    sid = body.get("session_id")
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if sid and not M.get_session(con, sid):
        return _not_found("session", sid)
    c = M.create_checkpoint(con, body["title"], body["body"],
                            workspace_id=wid, session_id=sid)
    _audit(con, "checkpoint.create", "checkpoint", c["id"],
           {"title": c["title"], "workspace_id": wid, "session_id": sid})
    return _created(con, req, digest, "checkpoint", _chk_view(c))


def _chk_view(c):
    c = dict(c)
    try:
        c["body"] = json.loads(c.get("body_json") or "{}")
    except Exception:
        c["body"] = {}
    c.pop("body_json", None)
    return c


def _chk_list(con, req):
    limit, _c = page_args(req)
    wid = req.q("workspace_id", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    rows = [_chk_view(c) for c in M.list_checkpoints(con, workspace_id=wid,
                                                    limit=limit)]
    return json_resp(200, {"checkpoints": rows})


def _chk_get(con, cid):
    c = M.get_checkpoint(con, cid)
    if not c:
        return _not_found("checkpoint", cid)
    return json_resp(200, {"checkpoint": _chk_view(c)})


# =====================================================================
# Handoffs
# =====================================================================

def _ensure_data_dir_kv(con, data_dir):
    # model.create_handoff reads data_dir from kv (set by app init); tolerate
    # a bare connection in tests by filling it in.
    row = con.execute("SELECT value FROM kv WHERE key='data_dir'").fetchone()
    if not row:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('data_dir', ?)",
                    (data_dir,))


def _ho_create(con, data_dir, req):
    digest, replay = _idem_lookup(con, req)
    if replay is not None:
        r = _idem_replay(con, replay)
        if r:
            return r
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "objective")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    wid = body.get("workspace_id")
    fsid = body.get("from_session_id")
    paid = body.get("payload_artifact_id")
    ptext = body.get("payload_text")
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if fsid and not M.get_session(con, fsid):
        return _not_found("session", fsid)
    if paid and not S.get_artifact(con, paid):
        return _not_found("artifact", paid)
    if ptext is not None and not isinstance(ptext, str):
        return err(400, "bad_request", "payload_text must be a string")
    scope = body.get("scope", {})
    expected = body.get("expected", {})
    meta = body.get("meta", {})
    for name, v in (("scope", scope), ("expected", expected), ("meta", meta)):
        if not isinstance(v, dict):
            return err(400, "bad_request", "%s must be an object" % name)
    _ensure_data_dir_kv(con, data_dir)
    h = M.create_handoff(con, body["objective"], payload_artifact_id=paid,
                         payload_text=ptext, workspace_id=wid,
                         from_session_id=fsid, to_env=body.get("to_env", ""),
                         scope=scope, expected=expected, meta=meta)
    _audit(con, "handoff.create", "handoff", h["id"],
           {"objective_len": len(body["objective"]),
            "to_env": h["to_env"],
            "payload_artifact_id": h["payload_artifact_id"]})
    return _created(con, req, digest, "handoff", h)


def _ho_list(con, req):
    limit, _c = page_args(req)
    wid = req.q("workspace_id", "") or None
    status = req.q("status", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    if status and status not in M.HANDOFF_STATUS:
        return err(400, "bad_request",
                   "status must be one of %s" % ",".join(M.HANDOFF_STATUS))
    return json_resp(200, {"handoffs": M.list_handoffs(con, workspace_id=wid,
                                                      status=status,
                                                      limit=limit)})


def _ho_get(con, hid):
    h = M.get_handoff(con, hid)
    if not h:
        return _not_found("handoff", hid)
    return json_resp(200, {"handoff": h})


def _ho_status(con, req, hid):
    h = M.get_handoff(con, hid)
    if not h:
        return _not_found("handoff", hid)
    body, e = _json_body(req)
    if e:
        return e
    status = body.get("status")
    if not status:
        return err(400, "bad_request", "status is required")
    if status not in M.HANDOFF_STATUS:
        return err(400, "bad_request",
                   "status must be one of %s" % ",".join(M.HANDOFF_STATUS))
    try:
        h2 = M.set_handoff_status(con, hid, status)
    except (KeyError, ValueError) as ex:
        code = 404 if isinstance(ex, KeyError) else 400
        return err(code, "bad_request" if code == 400 else "not_found",
                   str(ex).strip("'\""))
    _audit(con, "handoff.status", "handoff", hid,
           {"from": h["status"], "to": status})
    return json_resp(200, {"handoff": h2})


def _ho_payload(con, data_dir, hid):
    h = M.get_handoff(con, hid)
    if not h:
        return _not_found("handoff", hid)
    paid = h.get("payload_artifact_id")
    if not paid:
        return err(404, "not_found", "handoff %s has no payload" % hid)
    art = S.get_artifact(con, paid)
    if not art:
        return err(404, "not_found",
                   "payload artifact %s missing" % paid)
    try:
        data = S.read_artifact_bytes(con, data_dir, art)
    except FileNotFoundError:
        return err(404, "unavailable",
                   "payload bytes unavailable for handoff %s" % hid)
    recorded = h.get("payload_hash")
    actual = art["blob_sha256"]
    resp = {"handoff_id": hid, "artifact_id": paid, "mime": art["mime"],
            "size": art["size"], "sha256": actual,
            "recorded_hash": recorded,
            "hash_match": bool(recorded) and recorded == actual}
    try:
        resp["encoding"] = "utf-8"
        resp["text"] = data.decode("utf-8")
    except UnicodeDecodeError:
        resp["encoding"] = "base64"
        resp["base64"] = base64.b64encode(data).decode("ascii")
    return json_resp(200, resp)


# =====================================================================
# Search & context (layered retrieval; guarded while modules are pending)
# =====================================================================

def _search(con, data_dir, req):
    try:
        from . import search as _search_mod
    except ImportError:
        return err(501, "not_ready", "search module pending")
    q = req.q("q", "")
    if not q:
        return err(400, "bad_request", "q is required")
    limit, _c = page_args(req)
    doc_types = [d.strip() for d in req.q("doc_types", "").split(",") if d.strip()] or None
    wid = req.q("workspace_id", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)
    try:
        res = _search_mod.search_all(con, q, limit=limit, doc_types=doc_types,
                                     workspace_id=wid, data_dir=data_dir)
    except TypeError:
        # tolerate an older search_all(con, q) signature
        res = _search_mod.search_all(con, q)
    return json_resp(200, {"query": q, "results": res})


def _context(con, data_dir, req):
    try:
        from . import context as _context_mod
    except ImportError:
        return err(501, "not_ready", "context module pending")
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "question")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    seed_ids = body.get("seed_ids", [])
    if not isinstance(seed_ids, list):
        return err(400, "bad_request", "seed_ids must be a list of <type>:<id>")
    try:
        seeds = [_parse_seed(s) for s in seed_ids if s]
    except ValueError as ve:
        return err(400, "bad_request", str(ve))
    budget = body.get("budget_tokens", 4000)
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        return err(400, "bad_request", "budget_tokens must be an integer")
    if budget < 100 or budget > 200000:
        return err(400, "bad_request", "budget_tokens must be 100..200000")
    filters = body.get("filters", {})
    if not isinstance(filters, dict):
        return err(400, "bad_request", "filters must be an object")
    try:
        pkg = _context_mod.build_package(con, body["question"],
                                         seed_ids=seeds,
                                         budget_tokens=budget,
                                         filters=filters,
                                         data_dir=data_dir)
    except TypeError:
        pkg = _context_mod.build_package(con, body["question"])
    _audit(con, "context.build", "",
           "", {"question_len": len(body["question"]),
                "budget_tokens": budget, "seed_count": len(seeds)})
    return json_resp(200, {"package": pkg})


# =====================================================================
# Export
# =====================================================================

_EXPORT_EVENT_LIMIT = 200000


def _export(con, req):
    wid = req.q("workspace_id", "") or None
    if wid and not M.get_workspace(con, wid):
        return _not_found("workspace", wid)

    if wid:
        workspaces = [M.get_workspace(con, wid)]
        sessions = [dict(r) for r in con.execute(
            "SELECT * FROM session WHERE workspace_id=? ORDER BY created_at", (wid,))]
    else:
        workspaces = M.list_workspaces(con)
        n_events = con.execute("SELECT COUNT(*) FROM event").fetchone()[0]
        if n_events > _EXPORT_EVENT_LIMIT:
            raise _ExportTooBig()
        sessions = [dict(r) for r in con.execute(
            "SELECT * FROM session ORDER BY created_at")]
    sids = [s["id"] for s in sessions]

    ph, sargs = _in_clause(sids)
    events = [dict(r) for r in con.execute(
        "SELECT * FROM event WHERE session_id IN %s ORDER BY session_id, seq" % ph,
        sargs)] if sids else []
    eids = [e["id"] for e in events]

    def _ws_filtered(table):
        if not wid:
            return [dict(r) for r in con.execute("SELECT * FROM %s" % table)]
        return [dict(r) for r in con.execute(
            "SELECT * FROM %s WHERE workspace_id=?" % table, (wid,))]

    decisions = _ws_filtered("decision")
    tasks = _ws_filtered("task")
    claims = _ws_filtered("claim")
    checkpoints = _ws_filtered("checkpoint")
    handoffs = _ws_filtered("handoff")

    if wid:
        eh, eargs = _in_clause(eids)
        evid_rows = [dict(r) for r in con.execute(
            "SELECT * FROM evidence WHERE claim_id IN (SELECT id FROM claim "
            "WHERE workspace_id=?)", (wid,))]
        # artifacts reachable from this workspace: workspace edges, session
        # edges, event edges, handoff payloads, collection members of ws cols
        aids = set()
        for r in con.execute(
                "SELECT dst_id FROM edge WHERE src_type='workspace' AND src_id=? "
                "AND dst_type='artifact'", (wid,)):
            aids.add(r[0])
        if sids:
            ph2, a2 = _in_clause(sids)
            for r in con.execute(
                    "SELECT dst_id FROM edge WHERE src_type='session' AND src_id IN %s "
                    "AND dst_type='artifact'" % ph2, a2):
                aids.add(r[0])
        if eids:
            for r in con.execute(
                    "SELECT dst_id FROM edge WHERE src_type='event' AND src_id IN %s "
                    "AND dst_type='artifact'" % eh, eargs):
                aids.add(r[0])
        for h in handoffs:
            if h.get("payload_artifact_id"):
                aids.add(h["payload_artifact_id"])
        ah, aargs = _in_clause(aids)
        artifacts = [_artifact_view(dict(r)) for r in con.execute(
            "SELECT * FROM artifact WHERE id IN %s" % ah, aargs)] if aids else []
        # collections that hold those artifacts (or are ws-linked)
        cids = set()
        if aids:
            for r in con.execute(
                    "SELECT collection_id FROM collection_member WHERE artifact_id IN %s"
                    % ah, aargs):
                cids.add(r[0])
        ch, cargs = _in_clause(cids)
        collections = [dict(r) for r in con.execute(
            "SELECT * FROM collection WHERE id IN %s" % ch, cargs)] if cids else []
        members = [dict(r) for r in con.execute(
            "SELECT * FROM collection_member WHERE collection_id IN %s ORDER BY "
            "collection_id, position, added_at" % ch, cargs)] if cids else []
        # edges touching any exported object
        allids = {wid} | set(sids) | set(eids) | set(aids) | cids | \
                 {d["id"] for d in decisions} | {t["id"] for t in tasks} | \
                 {c["id"] for c in claims} | {c["id"] for c in checkpoints} | \
                 {h["id"] for h in handoffs} | \
                 {ev["id"] for ev in evid_rows}
        ih, iargs = _in_clause(allids)
        edges = [dict(r) for r in con.execute(
            "SELECT * FROM edge WHERE src_id IN %s OR dst_id IN %s" % (ih, ih),
            iargs + iargs)] if allids else []
        revisions = [dict(r) for r in con.execute(
            "SELECT * FROM artifact_revision WHERE artifact_id IN %s ORDER BY "
            "artifact_id, rev_no" % ah, aargs)] if aids else []
        legacy = [dict(r) for r in con.execute(
            "SELECT * FROM legacy_file WHERE artifact_id IN %s" % ah, aargs)] if aids else []
        evidence = evid_rows
    else:
        artifacts = [_artifact_view(dict(r)) for r in con.execute(
            "SELECT * FROM artifact ORDER BY created_at")]
        edges = [dict(r) for r in con.execute("SELECT * FROM edge ORDER BY created_at")]
        evidence = [dict(r) for r in con.execute("SELECT * FROM evidence ORDER BY created_at")]
        collections = [dict(r) for r in con.execute(
            "SELECT * FROM collection ORDER BY created_at")]
        members = [dict(r) for r in con.execute(
            "SELECT * FROM collection_member ORDER BY collection_id, position, added_at")]
        revisions = [dict(r) for r in con.execute(
            "SELECT * FROM artifact_revision ORDER BY artifact_id, rev_no")]
        legacy = [dict(r) for r in con.execute("SELECT * FROM legacy_file")]

    manifest = {
        "format": "substrate-export",
        "version": 1,
        "exported_at": time.time(),
        "schema_version": SCHEMA_VERSION,
        "workspace_id": wid,
        "workspaces": workspaces,
        "sessions": sessions,
        "events": events,
        "artifacts": artifacts,  # metadata + blob_sha256, no bytes
        "edges": edges,
        "decisions": decisions,
        "tasks": tasks,
        "claims": claims,
        "evidence": evidence,
        "collections": collections,
        "collection_members": members,
        "checkpoints": checkpoints,
        "handoffs": handoffs,
        "revisions": revisions,
        "legacy_map": legacy,
        "counts": {
            "workspaces": len(workspaces), "sessions": len(sessions),
            "events": len(events), "artifacts": len(artifacts),
            "edges": len(edges), "decisions": len(decisions),
            "tasks": len(tasks), "claims": len(claims),
            "evidence": len(evidence), "collections": len(collections),
            "collection_members": len(members), "checkpoints": len(checkpoints),
            "handoffs": len(handoffs), "revisions": len(revisions),
            "legacy_map": len(legacy),
        },
    }
    _audit(con, "export", "workspace" if wid else "", wid or "",
           {"counts": manifest["counts"]})
    body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    # NOTE: served as a JSON download; the mount contract has no header
    # channel, so Content-Disposition: attachment is left to the serving layer.
    return 200, "application/json; charset=utf-8", body


# =====================================================================
# Share links
# =====================================================================

def _share_create(con, req):
    # NOTE: deliberately NOT idempotent — the token is shown only once.
    body, e = _json_body(req)
    if e:
        return e
    miss = _missing(body, "scope_type", "scope_id")
    if miss:
        return err(400, "bad_request", "missing required field(s): %s" % ",".join(miss))
    scope_type = body["scope_type"]
    scope_id = body["scope_id"]
    if scope_type not in SHARE_SCOPES:
        return err(400, "bad_request",
                   "scope_type must be one of %s" % ",".join(SHARE_SCOPES))
    if _get_obj(con, scope_type, scope_id) is None:
        return _not_found(scope_type, scope_id)
    try:
        expires_at = _parse_ts(body.get("expires_at"), "expires_at")
    except ValueError as ve:
        return err(400, "bad_request", str(ve))
    link = M.create_share_link(con, scope_type, scope_id,
                               expires_at=expires_at,
                               note=body.get("note", ""))
    # The token is NEVER written to the audit log — shown only here, once.
    _audit(con, "share.create", "share_link", link["id"],
           {"scope_type": scope_type, "scope_id": scope_id,
            "expires_at": expires_at})
    return json_resp(201, {"id": link["id"], "token": link["token"],
                           "share_url": "/s/share/" + link["token"],
                           "scope_type": scope_type, "scope_id": scope_id,
                           "expires_at": link["expires_at"]})


def _share_revoke(con, lid):
    row = con.execute("SELECT * FROM share_link WHERE id=?", (lid,)).fetchone()
    if not row:
        return _not_found("share_link", lid)
    M.revoke_share_link(con, lid)
    _audit(con, "share.revoke", "share_link", lid, {})
    return json_resp(200, {"id": lid, "revoked": True})


# =====================================================================
# Extract jobs
# =====================================================================

def _extract_jobs(con, req):
    limit, _c = page_args(req)
    status = req.q("status", "") or None
    if status and status not in EXTRACT_STATUS:
        return err(400, "bad_request",
                   "status must be one of %s" % ",".join(EXTRACT_STATUS))
    q = "SELECT * FROM extract_job"
    args = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in con.execute(q, args)]
    return json_resp(200, {"jobs": rows, "limit": limit})


# =====================================================================
# Audit
# =====================================================================

def _audit_list(con, req):
    limit, _c = page_args(req)
    action = req.q("action", "") or None
    return json_resp(200, {"audit": M.audit_tail(con, limit=limit, action=action),
                           "limit": limit})
