"""Server-rendered web UI for the substrate (/s/ routes).

Mount contract (see app.py):
    def handle(con, data_dir, req) -> (status:int, content_type:str, body:bytes) | None

Calls model/store directly (no HTTP round-trips). search/context are optional:
if those modules are not present yet, guarded imports render honest
"search initializing" empty states instead of crashing.

NOTE on the mount contract: it returns (status, content_type, body) with no
header channel, so POST actions cannot issue a 303 Location redirect. Actions
instead render the destination page directly with HTTP 200.
"""
import html
import json
import os
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie

from . import app as _app
from . import model
from . import store
from .model import _HANDOFF_FLOW

try:
    from . import search as _search
except Exception:  # module not built yet
    _search = None

try:
    from . import context as _context
except Exception:  # module not built yet
    _context = None

E = html.escape

CSS = """
body{background:#0f1115;color:#e8eaf0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;font-size:14px;line-height:1.5}
a{color:#7fb0ff;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px 60px}
nav.top{background:#13161c;border-bottom:1px solid #2a2f3a;position:sticky;top:0;z-index:10}
nav.top .inner{max-width:1200px;margin:0 auto;padding:8px 16px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}
nav.top a{font-size:13px;padding:4px 6px;border-radius:4px}
nav.top a:hover{background:#1d222b;text-decoration:none}
nav.top .brand{color:#e8eaf0;font-weight:700;margin-right:8px}
.card{background:#171a21;border:1px solid #2a2f3a;border-radius:8px;padding:14px 16px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.muted{color:#9aa3b2}.small{font-size:12px}
h1{font-size:22px;margin:18px 0 8px}h2{font-size:17px;margin:16px 0 8px}h3{font-size:14px;margin:12px 0 6px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #2a2f3a;padding:6px 8px;text-align:left;vertical-align:top}
th{color:#9aa3b2;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
code,.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
pre{background:#10131a;border:1px solid #2a2f3a;border-radius:6px;padding:10px;overflow:auto;max-height:420px;font-size:12.5px;white-space:pre-wrap}
.badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;border:1px solid #2a2f3a;background:#1d222b;color:#9aa3b2;margin:0 4px 2px 0}
.badge.inferred{border-color:#8a6d3b;color:#e0b45c}
.badge.explicit{border-color:#3b6e4f;color:#7fd69a}
.badge.warn{border-color:#8a4a3b;color:#e08a5c}
.badge.ok{border-color:#3b6e4f;color:#7fd69a}
.tq{color:#9aa3b2;font-size:11px}
form.inline{display:inline}
input[type=text],input[type=number],textarea,select{background:#10131a;color:#e8eaf0;border:1px solid #2a2f3a;border-radius:6px;padding:7px 10px;font-size:13px;width:100%;box-sizing:border-box}
textarea{min-height:80px;font-family:inherit}
input[type=file]{color:#9aa3b2;font-size:13px}
button,.btn{background:#24304a;color:#e8eaf0;border:1px solid #3a4a68;border-radius:6px;padding:7px 14px;font-size:13px;cursor:pointer}
button:hover,.btn:hover{background:#2c3a5c}
button.danger{background:#4a2424;border-color:#683a3a}
label{display:block;font-size:12px;color:#9aa3b2;margin:8px 0 4px;text-transform:uppercase;letter-spacing:.4px}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:200px}
details{background:#10131a;border:1px solid #2a2f3a;border-radius:6px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;color:#7fb0ff}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.gallery img{width:100%;height:140px;object-fit:cover;border-radius:6px;border:1px solid #2a2f3a}
.viewer{text-align:center}.viewer img{max-width:100%;max-height:70vh;border:1px solid #2a2f3a;border-radius:8px}
.pipeline{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0}
.pipe{padding:5px 12px;border-radius:14px;border:1px solid #2a2f3a;font-size:12px;color:#9aa3b2}
.pipe.done{background:#1c2b22;border-color:#3b6e4f;color:#7fd69a}
.pipe.cur{background:#24304a;border-color:#7fb0ff;color:#e8eaf0;font-weight:700}
.pipe-arrow{color:#4a5468}
.depth{border-left:3px solid #2a2f3a;padding-left:12px;margin:10px 0}
.empty{color:#9aa3b2;font-style:italic;padding:18px;text-align:center}
.kbd{background:#10131a;border:1px solid #2a2f3a;border-radius:4px;padding:1px 6px;font-size:12px}
ul.tight{margin:6px 0;padding-left:20px}ul.tight li{margin:3px 0}
"""

NAV = [
    ("Home", "/s/"),
    ("Workspaces", "/s/workspaces"),
    ("Sessions", "/s/sessions"),
    ("Timeline", "/s/timeline"),
    ("Artifacts", "/s/artifacts"),
    ("Collections", "/s/collections"),
    ("Graph", "/s/graph"),
    ("Search", "/s/search"),
    ("Context", "/s/context"),
    ("Checkpoints", "/s/checkpoints"),
    ("Handoffs", "/s/handoffs"),
    ("Capture", "/s/capture"),
    ("Settings", "/s/settings"),
]


def _tokq(req):
    # Only propagate ?token= when the session actually depends on it
    # (token arrived via query). Cookie/Bearer sessions don't need it,
    # which keeps the token out of URLs, logs, and Referer headers.
    if req.token and getattr(req, "token_via_query", False):
        return "?token=" + req.token
    return ""


def _href(req, path):
    t = _tokq(req)
    if not t:
        return path
    return path + ("&" if "?" in path else "?") + "token=" + req.token


def _shell(req, title, body, extra_head="", nav_on=True):
    nav_html = ""
    if nav_on:
        links = "".join(
            '<a href="%s">%s</a>' % (_href(req, p), E(n)) for n, p in NAV)
        nav_html = ('<nav class="top"><div class="inner">'
                    '<span class="brand">Substrate</span>%s</div></nav>' % links)
    page = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style>%s</head>"
            "<body>%s<div class='wrap'>%s</div></body></html>"
            % (E(title), CSS, extra_head, nav_html, body))
    return _app.html_resp(200, page)


def _deny():
    return _app.html_resp(
        403,
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>403</title>"
        "<style>%s</style></head><body><div class='wrap'>"
        "<h1>403 — authentication required</h1>"
        "<p class='muted'>This page requires the owner token. "
        "Append <code>?token=...</code>, send <code>Authorization: Bearer</code>, "
        "or set the <code>substrate_token</code> cookie.</p>"
        "</div></body></html>" % CSS)


def _notfound(msg="Not found"):
    return _app.html_resp(
        404,
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>404</title>"
        "<style>%s</style></head><body><div class='wrap'>"
        "<h1>404</h1><p class='muted'>%s</p></div></body></html>" % (CSS, E(msg)))


def _fmt_ts(ts, tq="exact"):
    if ts is None:
        return '<span class="muted">time unknown</span>'
    try:
        s = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError):
        return '<span class="muted">invalid time</span>'
    if tq and tq != "exact":
        s += ' <span class="tq">(%s)</span>' % E(tq)
    return s


def _fields(req):
    """Split parsed multipart into text fields and files."""
    text, files = {}, {}
    for name, filename, ctype, payload in req.form:
        if filename:
            files[name] = (filename, ctype, payload)
        else:
            text[name] = payload.decode("utf-8", "replace")
    return text, files


_OBJ_ROUTE = {
    "workspace": "/s/workspaces/%s",
    "session": "/s/sessions/%s",
    "artifact": "/s/artifacts/%s",
    "decision": "/s/decisions/%s",
    "task": "/s/tasks/%s",
    "claim": "/s/claims/%s",
    "collection": "/s/collections/%s",
    "checkpoint": "/s/checkpoints/%s",
    "handoff": "/s/handoffs/%s",
    "event": "/s/events/%s",
}


def _obj_label(con, otype, oid):
    """Best-effort human label for a graph/edge endpoint. Returns (label, href|None)."""
    try:
        if otype == "workspace":
            r = model.get_workspace(con, oid)
            return (r["title"], "workspace") if r else (oid, None)
        if otype == "session":
            r = model.get_session(con, oid)
            return (r["title"] or oid, "session") if r else (oid, None)
        if otype == "artifact":
            r = store.get_artifact(con, oid)
            return (r["filename"], "artifact") if r else (oid, None)
        if otype == "decision":
            r = model.get_decision(con, oid)
            return (r["title"], "decision") if r else (oid, None)
        if otype == "task":
            r = model.get_task(con, oid)
            return (r["title"], "task") if r else (oid, None)
        if otype == "claim":
            r = model.get_claim(con, oid)
            return (r["title"], "claim") if r else (oid, None)
        if otype == "collection":
            r = model.get_collection(con, oid)
            return (r["title"], "collection") if r else (oid, None)
        if otype == "checkpoint":
            r = model.get_checkpoint(con, oid)
            return (r["title"], "checkpoint") if r else (oid, None)
        if otype == "handoff":
            r = model.get_handoff(con, oid)
            return ((r["objective"][:60] or oid), "handoff") if r else (oid, None)
        if otype == "event":
            r = model.get_event(con, oid)
            if r:
                t = (r["text"] or "")[:60]
                return (t or oid, "event")
            return (oid, None)
    except Exception:
        pass
    return (oid, None)


def _obj_link(req, con, otype, oid):
    label, kind = _obj_label(con, otype, oid)
    route = _OBJ_ROUTE.get(otype)
    if route and kind:
        return '<a href="%s"><span class="badge">%s</span> %s</a>' % (
            _href(req, route % oid), E(otype), E(label))
    return '<span class="badge">%s</span> <span class="mono">%s</span>' % (E(otype), E(oid))


def _edge_html(req, con, e):
    inf = e.get("inferred")
    badge = ('<span class="badge inferred">inferred</span>' if inf
             else '<span class="badge explicit">explicit</span>')
    src = _obj_link(req, con, e["src_type"], e["src_id"])
    dst = _obj_link(req, con, e["dst_type"], e["dst_id"])
    note = (' <span class="muted small">— %s</span>' % E(e["note"])) if e.get("note") else ""
    return ('<div class="small">%s %s <code>%s</code> → %s%s</div>'
            % (badge, src, E(e["rel"]), dst, note))


def _counts(con):
    out = {}
    for t in (("artifacts", "artifact"), ("events", "event"), ("edges", "edge"),
              ("sessions", "session"), ("workspaces", "workspace"),
              ("decisions", "decision"), ("tasks", "task"), ("claims", "claim"),
              ("collections", "collection"), ("checkpoints", "checkpoint"),
              ("handoffs", "handoff"), ("share_links", "share_link")):
        out[t[0]] = con.execute("SELECT COUNT(*) FROM %s" % t[1]).fetchone()[0]
    return out


def _queue_depth(con):
    return con.execute(
        "SELECT COUNT(*) FROM extract_job WHERE status IN ('pending','processing')"
    ).fetchone()[0]


# ---------------------------------------------------------------- home

def _page_home(req, con):
    counts = _counts(con)
    qd = _queue_depth(con)
    wss = model.list_workspaces(con)[:5]
    sess = model.list_sessions(con, limit=10)
    chks = model.list_checkpoints(con, limit=5)
    h = ["<h1>Substrate</h1>",
         '<p class="muted">Persistent session &amp; knowledge substrate. '
         "Store the whole history; retrieve only the relevant parts.</p>"]
    h.append('<div class="row">')
    for k in ("artifacts", "events", "edges", "sessions", "workspaces"):
        h.append('<div class="card"><div class="small muted">%s</div>'
                 '<div style="font-size:26px">%d</div></div>' % (k, counts[k]))
    h.append('<div class="card"><div class="small muted">extraction queue</div>'
             '<div style="font-size:26px">%d</div></div>' % qd)
    # restore / search-index status: distinguish complete from rebuilding
    try:
        import json as _json
        _row = con.execute("SELECT value FROM kv WHERE key='fts_status'").fetchone()
        if _row:
            _st = _json.loads(_row[0])
            _idx = _st.get("index", "unknown")
            _cls = "badge ok" if _idx == "complete" else "badge warn"
            h.append('<div class="card"><div class="small muted">search index</div>'
                     '<div><span class="%s">%s</span></div>'
                     '<div class="small muted">%d chunks · %d pending · %d failed</div></div>'
                     % (_cls, E(_idx), _st.get("chunks_restored", 0),
                        _st.get("jobs_pending", 0), _st.get("jobs_failed", 0)))
    except Exception:
        pass
    h.append("</div>")
    h.append('<div class="row"><div>')
    h.append("<h2>Recent workspaces</h2>")
    if wss:
        h.append("<ul class='tight'>")
        for w in wss:
            h.append('<li><a href="%s">%s</a> <span class="muted small">%s</span></li>'
                     % (_href(req, "/s/workspaces/" + w["id"]), E(w["title"]), w["id"]))
        h.append("</ul>")
    else:
        h.append('<div class="empty">No workspaces yet.</div>')
    h.append("</div><div>")
    h.append("<h2>Recent sessions</h2>")
    if sess:
        h.append("<ul class='tight'>")
        for s in sess:
            h.append('<li><a href="%s">%s</a> <span class="muted small">%s · %s</span></li>'
                     % (_href(req, "/s/sessions/" + s["id"]),
                        E(s["title"] or s["id"]), s["id"],
                        _fmt_ts(s.get("started_at") or s["created_at"], s["time_quality"])))
        h.append("</ul>")
    else:
        h.append('<div class="empty">No sessions yet.</div>')
    h.append("</div><div>")
    h.append("<h2>Recent checkpoints</h2>")
    if chks:
        h.append("<ul class='tight'>")
        for c in chks:
            h.append('<li><a href="%s">%s</a> <span class="muted small">v%d</span></li>'
                     % (_href(req, "/s/checkpoints/" + c["id"]), E(c["title"]),
                        c["schema_version"]))
        h.append("</ul>")
    else:
        h.append('<div class="empty">No checkpoints yet.</div>')
    h.append("</div></div>")
    return _shell(req, "Substrate — home", "".join(h))


# ---------------------------------------------------------------- workspaces

def _page_workspaces(req, con):
    if req.method == "POST":
        text, _ = _fields(req)
        w = model.create_workspace(con, text.get("title", "").strip() or "Untitled",
                                   text.get("description", "").strip())
        model.audit(con, "owner", "workspace_create", "workspace", w["id"], {})
        return _page_workspace_detail(req, con, w["id"])
    wss = model.list_workspaces(con)
    h = ["<h1>Workspaces</h1>",
         '<div class="card"><h3>New workspace</h3>'
         '<form method="post" action="%s" enctype="multipart/form-data">'
         '<label>Title</label><input type="text" name="title" required>'
         '<label>Description</label><textarea name="description"></textarea>'
         '<p><button type="submit">Create workspace</button></p></form></div>'
         % _href(req, "/s/workspaces")]
    if wss:
        h.append('<div class="grid">')
        for w in wss:
            ns = con.execute("SELECT COUNT(*) FROM session WHERE workspace_id=?",
                             (w["id"],)).fetchone()[0]
            h.append('<div class="card"><h3><a href="%s">%s</a></h3>'
                     '<p class="muted small">%s</p>'
                     '<p class="small muted">%d sessions · created %s</p></div>'
                     % (_href(req, "/s/workspaces/" + w["id"]), E(w["title"]),
                        E(w["description"] or ""), ns,
                        _fmt_ts(w["created_at"], "exact")))
        h.append("</div>")
    else:
        h.append('<div class="empty">No workspaces yet — create one above.</div>')
    return _shell(req, "Workspaces", "".join(h))


def _page_workspace_detail(req, con, wid):
    w = model.get_workspace(con, wid)
    if not w:
        return _notfound("Workspace not found")
    if req.method == "POST":
        text, _ = _fields(req)
        action = text.get("action", "")
        if action == "create_session":
            model.create_session(con, workspace_id=wid,
                                 title=text.get("title", "").strip() or "Untitled session")
        elif action == "create_decision":
            model.record_decision(con, text.get("title", "").strip() or "Untitled",
                                  text.get("body", ""), workspace_id=wid)
        elif action == "create_task":
            model.create_task(con, text.get("title", "").strip() or "Untitled",
                              kind=text.get("kind", "task") or "task",
                              body=text.get("body", ""), workspace_id=wid)
        elif action == "create_claim":
            model.create_claim(con, text.get("title", "").strip() or "Untitled",
                               text.get("body", ""), workspace_id=wid)
    sess = model.list_sessions(con, workspace_id=wid, limit=50)
    decs = model.list_decisions(con, workspace_id=wid, limit=50)
    tasks = model.list_tasks(con, workspace_id=wid, limit=50)
    claims = [_r(con, r) for r in con.execute(
        "SELECT * FROM claim WHERE workspace_id=? ORDER BY created_at DESC LIMIT 50",
        (wid,))]
    chks = model.list_checkpoints(con, workspace_id=wid, limit=20)
    h = ["<h1>%s</h1><p class='muted'>%s</p>"
         % (E(w["title"]), E(w["description"] or "")),
         '<p class="small muted mono">%s</p>' % E(wid)]
    h.append('<div class="row"><div>')
    h.append("<h2>Sessions (%d)</h2>" % len(sess))
    h.append('<div class="card"><h3>New session</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="create_session">'
             '<label>Title</label><input type="text" name="title">'
             '<p><button type="submit">Create session</button></p></form></div>'
             % _href(req, "/s/workspaces/" + wid))
    for s in sess:
        h.append('<div class="small"><a href="%s">%s</a> '
                 '<span class="muted">%s</span></div>'
                 % (_href(req, "/s/sessions/" + s["id"]),
                    E(s["title"] or s["id"]),
                    _fmt_ts(s.get("started_at") or s["created_at"], s["time_quality"])))
    h.append("</div><div>")
    h.append("<h2>Decisions (%d)</h2>" % len(decs))
    h.append('<div class="card"><h3>Record decision</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="create_decision">'
             '<label>Title</label><input type="text" name="title" required>'
             '<label>Body</label><textarea name="body"></textarea>'
             '<p><button type="submit">Record decision</button></p></form></div>'
             % _href(req, "/s/workspaces/" + wid))
    for d in decs:
        h.append('<div class="small"><span class="badge">%s</span>'
                 '<a href="%s">%s</a></div>'
                 % (E(d["status"]), _href(req, "/s/decisions/" + d["id"]), E(d["title"])))
    h.append("<h2>Tasks &amp; questions (%d)</h2>" % len(tasks))
    h.append('<div class="card"><h3>New task / question</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="create_task">'
             '<div class="row"><div><label>Kind</label>'
             '<select name="kind"><option value="task">task</option>'
             '<option value="question">question</option></select></div>'
             '<div><label>Title</label><input type="text" name="title" required></div></div>'
             '<label>Body</label><textarea name="body"></textarea>'
             '<p><button type="submit">Create</button></p></form></div>'
             % _href(req, "/s/workspaces/" + wid))
    for t in tasks:
        h.append('<div class="small"><span class="badge">%s</span> '
                 '<span class="badge">%s</span>'
                 '<a href="%s">%s</a></div>'
                 % (E(t["kind"]), E(t["status"]),
                    _href(req, "/s/tasks/" + t["id"]), E(t["title"])))
    h.append("</div><div>")
    h.append("<h2>Claims (%d)</h2>" % len(claims))
    h.append('<div class="card"><h3>New claim</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="create_claim">'
             '<label>Title</label><input type="text" name="title" required>'
             '<label>Body</label><textarea name="body"></textarea>'
             '<p><button type="submit">Create claim</button></p></form></div>'
             % _href(req, "/s/workspaces/" + wid))
    for c in claims:
        h.append('<div class="small"><span class="badge">%s</span> '
                 '<a href="%s">%s</a></div>'
                 % (E(c["verification_status"]),
                    _href(req, "/s/claims/" + c["id"]), E(c["title"])))
    h.append("<h2>Checkpoints (%d)</h2>" % len(chks))
    for c in chks:
        h.append('<div class="small"><a href="%s">%s</a> '
                 '<span class="muted">v%d</span></div>'
                 % (_href(req, "/s/checkpoints/" + c["id"]), E(c["title"]),
                    c["schema_version"]))
    h.append("</div></div>")
    return _shell(req, "Workspace — " + w["title"], "".join(h))


def _r(con, row):
    return dict(row)


# ---------------------------------------------------------------- sessions

def _page_sessions(req, con):
    wsid = req.q("workspace_id", "")
    sess = model.list_sessions(con, workspace_id=wsid or None, limit=100)
    h = ["<h1>Sessions</h1>"]
    wss = model.list_workspaces(con)
    h.append('<form method="get" action="/s/sessions"><div class="row"><div>'
             '<label>Workspace filter</label><select name="workspace_id" '
             'onchange="this.form.submit()">'
             '<option value="">All workspaces</option>')
    for w in wss:
        h.append('<option value="%s"%s>%s</option>'
                 % (E(w["id"]), " selected" if w["id"] == wsid else "", E(w["title"])))
    h.append("</select></div></div>")
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append("</form>")
    if sess:
        h.append('<table><tr><th>Title</th><th>ID</th><th>Workspace</th>'
                 '<th>Started</th></tr>')
        for s in sess:
            ws = model.get_workspace(con, s["workspace_id"]) if s["workspace_id"] else None
            h.append("<tr><td><a href='%s'>%s</a></td><td class='mono small'>%s</td>"
                     "<td class='small'>%s</td><td class='small'>%s</td></tr>"
                     % (_href(req, "/s/sessions/" + s["id"]),
                        E(s["title"] or "(untitled)"), E(s["id"]),
                        E(ws["title"]) if ws else '<span class="muted">—</span>',
                        _fmt_ts(s.get("started_at") or s["created_at"],
                                s["time_quality"])))
        h.append("</table>")
    else:
        h.append('<div class="empty">No sessions.</div>')
    return _shell(req, "Sessions", "".join(h))


def _page_session_detail(req, con, sid):
    s = model.get_session(con, sid)
    if not s:
        return _notfound("Session not found")
    if req.method == "POST":
        text, files = _fields(req)
        action = text.get("action", "")
        if action == "add_event":
            model.append_event(con, sid, text.get("text", ""),
                               actor=text.get("actor", ""), role=text.get("role", ""),
                               kind=text.get("kind", "message") or "message")
        elif action == "upload_artifact":
            f = files.get("file")
            if f:
                filename, ctype, payload = f
                art = store.register_artifact(
                    con, _data_dir(con), data=payload,
                    filename=filename or "upload",
                    mime=ctype or "application/octet-stream",
                    visibility=text.get("visibility", "private") or "private",
                    source="upload", source_ref="session:" + sid)
                model.add_edge(con, "session", sid, "artifact", art["id"],
                               "references", note=text.get("note", ""))
                model.append_event(con, sid,
                                   "Uploaded %s (%s)" % (filename, art["id"]),
                                   actor=text.get("actor", ""), kind="upload")
    kind_filter = req.q("kind", "")
    kinds = [kind_filter] if kind_filter else None
    events = model.list_events(con, sid, limit=500, kinds=kinds)
    nb = model.neighbors(con, "session", sid, direction="both", limit=200)
    h = ["<h1>%s</h1>" % E(s["title"] or "(untitled session)"),
         '<p class="small muted mono">%s</p>' % E(sid)]
    ws = model.get_workspace(con, s["workspace_id"]) if s["workspace_id"] else None
    h.append('<p class="small">Workspace: %s · Source: <code>%s</code> · '
             'Started: %s</p>'
             % (('<a href="%s">%s</a>' % (_href(req, "/s/workspaces/" + ws["id"]),
                                         E(ws["title"]))) if ws else "—",
                E(s["source"] or ""),
                _fmt_ts(s.get("started_at"), s["time_quality"])))
    h.append('<div class="row"><div>')
    h.append("<h2>Event timeline (%d)</h2>" % len(events))
    h.append('<form method="get" action="%s"><div class="row"><div>'
             '<label>Filter by kind</label><select name="kind" onchange="this.form.submit()">'
             '<option value="">all kinds</option>' % _href(req, "/s/sessions/" + sid))
    for k in ("message", "instruction", "tool_result", "upload", "decision_ref",
              "checkpoint_ref", "handoff_ref", "note"):
        h.append('<option value="%s"%s>%s</option>'
                 % (k, " selected" if k == kind_filter else "", k))
    h.append("</select></div></div>")
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append("</form>")
    if events:
        for e in events:
            h.append('<div class="card" id="%s">'
                     '<div class="small muted">#%d · <span class="badge">%s</span> '
                     '%s · %s</div>'
                     % (E(e["id"]), e["seq"], E(e["kind"]),
                        E(e["actor"]) if e["actor"] else '<span class="muted">no actor</span>',
                        _fmt_ts(e["ts"], e["time_quality"])))
            if e["text"]:
                h.append("<pre>%s</pre>" % E(e["text"]))
            h.append("</div>")
    else:
        h.append('<div class="empty">No events recorded. Never invented — add one below.</div>')
    h.append('<div class="card"><h3>Add event</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="add_event">'
             '<div class="row"><div><label>Kind</label><select name="kind">'
             % _href(req, "/s/sessions/" + sid))
    for k in ("message", "instruction", "tool_result", "upload", "decision_ref",
              "checkpoint_ref", "handoff_ref", "note"):
        h.append('<option value="%s">%s</option>' % (k, k))
    h.append('</select></div><div><label>Actor</label>'
             '<input type="text" name="actor"></div>'
             '<div><label>Role</label><input type="text" name="role"></div></div>'
             '<label>Text</label><textarea name="text"></textarea>'
             '<p><button type="submit">Append event</button></p></form></div>')
    h.append('<div class="card"><h3>Upload artifact</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="upload_artifact">'
             '<label>File</label><input type="file" name="file" required>'
             '<label>Visibility</label><select name="visibility">'
             '<option value="private">private</option>'
             '<option value="public-link">public-link</option></select>'
             '<label>Note</label><input type="text" name="note">'
             '<p><button type="submit">Upload &amp; link to session</button></p>'
             '</form></div>')
    h.append("</div><div>")
    h.append("<h2>Relationships</h2>")
    if nb["out"] or nb["in"]:
        h.append("<h3>Out</h3>")
        for e in nb["out"]:
            h.append(_edge_html(req, con, e))
        h.append("<h3>In</h3>")
        for e in nb["in"]:
            h.append(_edge_html(req, con, e))
    else:
        h.append('<div class="empty">No relationships yet.</div>')
    h.append("</div></div>")
    return _shell(req, "Session", "".join(h))


# ---------------------------------------------------------------- timeline

def _page_timeline(req, con):
    wsid = req.q("workspace_id", "")
    since = req.q("since", "")
    until = req.q("until", "")
    try:
        limit = max(1, min(int(req.q("limit", "100")), 500))
    except ValueError:
        limit = 100
    items = []  # (ts, sortkey, kind, html)

    def in_ws(w):
        return not wsid or w == wsid

    try:
        s_since = float(since) if since else None
        s_until = float(until) if until else None
    except ValueError:
        s_since = s_until = None

    # events (joined to session for workspace + title)
    q = ("SELECT e.*, s.workspace_id, s.title AS stitle FROM event e "
         "JOIN session s ON s.id=e.session_id")
    args, clauses = [], []
    if wsid:
        clauses.append("s.workspace_id=?"); args.append(wsid)
    if s_since is not None:
        clauses.append("COALESCE(e.ts,e.created_at) >= ?"); args.append(s_since)
    if s_until is not None:
        clauses.append("COALESCE(e.ts,e.created_at) <= ?"); args.append(s_until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY COALESCE(e.ts,e.created_at) DESC LIMIT ?"
    args.append(limit)
    for r in con.execute(q, args):
        r = dict(r)
        ts = r["ts"] if r["ts"] is not None else r["created_at"]
        txt = (r["text"] or "")[:160]
        items.append((ts, 0, "event",
                      '<span class="badge">%s</span> <a href="%s">%s</a> '
                      '<span class="muted">in session %s</span>'
                      % (E(r["kind"]), _href(req, "/s/sessions/" + r["session_id"]),
                         E(txt or "(empty)"), E(r["stitle"] or r["session_id"])),
                      _fmt_ts(r["ts"], r["time_quality"])))
    # decisions
    q = "SELECT * FROM decision"
    args, clauses = [], []
    if wsid:
        clauses.append("workspace_id=?"); args.append(wsid)
    if s_since is not None:
        clauses.append("COALESCE(decided_at,created_at) >= ?"); args.append(s_since)
    if s_until is not None:
        clauses.append("COALESCE(decided_at,created_at) <= ?"); args.append(s_until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY COALESCE(decided_at,created_at) DESC LIMIT ?"
    args.append(limit)
    for r in con.execute(q, args):
        r = dict(r)
        ts = r["decided_at"] if r["decided_at"] is not None else r["created_at"]
        items.append((ts, 1, "decision",
                      '<span class="badge">decision</span> '
                      '<a href="%s">%s</a> <span class="badge">%s</span>'
                      % (_href(req, "/s/decisions/" + r["id"]), E(r["title"]),
                         E(r["status"])),
                      _fmt_ts(r["decided_at"], r["time_quality"])))
    # tasks
    q = "SELECT * FROM task"
    args, clauses = [], []
    if wsid:
        clauses.append("workspace_id=?"); args.append(wsid)
    if s_since is not None:
        clauses.append("created_at >= ?"); args.append(s_since)
    if s_until is not None:
        clauses.append("created_at <= ?"); args.append(s_until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    for r in con.execute(q, args):
        r = dict(r)
        items.append((r["created_at"], 2, "task",
                      '<span class="badge">%s</span> <a href="%s">%s</a> '
                      '<span class="badge">%s</span>'
                      % (E(r["kind"]), _href(req, "/s/tasks/" + r["id"]),
                         E(r["title"]), E(r["status"])),
                      _fmt_ts(r["created_at"], "exact")))
    # artifacts
    q = "SELECT * FROM artifact"
    args, clauses = [], []
    if s_since is not None:
        clauses.append("created_at >= ?"); args.append(s_since)
    if s_until is not None:
        clauses.append("created_at <= ?"); args.append(s_until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    for r in con.execute(q, args):
        r = dict(r)
        items.append((r["created_at"], 3, "artifact",
                      '<span class="badge">artifact</span> <a href="%s">%s</a> '
                      '<span class="muted small">%s</span>'
                      % (_href(req, "/s/artifacts/" + r["id"]), E(r["filename"]),
                         E(r["mime"])),
                      _fmt_ts(r["created_at"], "exact")))
    # checkpoints
    q = "SELECT * FROM checkpoint"
    args, clauses = [], []
    if wsid:
        clauses.append("workspace_id=?"); args.append(wsid)
    if s_since is not None:
        clauses.append("created_at >= ?"); args.append(s_since)
    if s_until is not None:
        clauses.append("created_at <= ?"); args.append(s_until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    for r in con.execute(q, args):
        r = dict(r)
        items.append((r["created_at"], 4, "checkpoint",
                      '<span class="badge">checkpoint</span> <a href="%s">%s</a>'
                      % (_href(req, "/s/checkpoints/" + r["id"]), E(r["title"])),
                      _fmt_ts(r["created_at"], "exact")))
    # handoffs
    q = "SELECT * FROM handoff"
    args, clauses = [], []
    if wsid:
        clauses.append("workspace_id=?"); args.append(wsid)
    if s_since is not None:
        clauses.append("created_at >= ?"); args.append(s_since)
    if s_until is not None:
        clauses.append("created_at <= ?"); args.append(s_until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    for r in con.execute(q, args):
        r = dict(r)
        items.append((r["created_at"], 5, "handoff",
                      '<span class="badge">handoff</span> <a href="%s">%s</a> '
                      '<span class="badge">%s</span>'
                      % (_href(req, "/s/handoffs/" + r["id"]),
                         E((r["objective"] or "")[:80] or r["id"]), E(r["status"])),
                      _fmt_ts(r["created_at"], "exact")))

    items.sort(key=lambda x: (-(x[0] or 0), x[1]))
    items = items[:limit]

    h = ["<h1>Timeline</h1>",
         '<p class="muted small">Chronological merge of recorded events only. '
         "Nothing is invented to fill gaps.</p>"]
    wss = model.list_workspaces(con)
    h.append('<form method="get" action="/s/timeline"><div class="row">'
             '<div><label>Workspace</label><select name="workspace_id">'
             '<option value="">all</option>')
    for w in wss:
        h.append('<option value="%s"%s>%s</option>'
                 % (E(w["id"]), " selected" if w["id"] == wsid else "", E(w["title"])))
    h.append('</select></div><div><label>Since (epoch)</label>'
             '<input type="text" name="since" value="%s"></div>'
             '<div><label>Until (epoch)</label>'
             '<input type="text" name="until" value="%s"></div>'
             '<div><label>Limit</label><input type="number" name="limit" value="%d" '
             'min="1" max="500"></div></div>' % (E(since), E(until), limit))
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append('<p><button type="submit">Apply</button></p></form>')
    if items:
        h.append('<table><tr><th>Time</th><th>Entry</th></tr>')
        for ts, _sk, _kind, label, fts in items:
            h.append("<tr><td class='small' style='white-space:nowrap'>%s</td>"
                     "<td>%s</td></tr>" % (fts, label))
        h.append("</table>")
    else:
        h.append('<div class="empty">No recorded entries in this range.</div>')
    return _shell(req, "Timeline", "".join(h))


# ---------------------------------------------------------------- artifacts

def _page_artifacts(req, con):
    q = req.q("q", "").strip()
    mime_f = req.q("mime", "").strip()
    vis_f = req.q("visibility", "").strip()
    try:
        limit = max(1, min(int(req.q("limit", "60")), 200))
    except ValueError:
        limit = 60
    clauses, args = [], []
    if q:
        clauses.append("(filename LIKE ? ESCAPE '\\' OR id=?)")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args.extend([like, q])
    if mime_f:
        clauses.append("mime LIKE ?"); args.append(mime_f + "%")
    if vis_f:
        clauses.append("visibility=?"); args.append(vis_f)
    sql = "SELECT * FROM artifact"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in con.execute(sql, args)]
    h = ["<h1>Artifacts</h1>",
         '<p><a href="%s">Image gallery →</a></p>' % _href(req, "/s/gallery")]
    h.append('<form method="get" action="/s/artifacts"><div class="row">'
             '<div><label>Search filename / id</label>'
             '<input type="text" name="q" value="%s"></div>'
             '<div><label>MIME prefix</label><input type="text" name="mime" value="%s" '
             'placeholder="image/"></div>'
             '<div><label>Visibility</label><select name="visibility">'
             '<option value="">any</option>' % (E(q), E(mime_f)))
    for v in ("private", "public-link"):
        h.append('<option value="%s"%s>%s</option>'
                 % (v, " selected" if v == vis_f else "", v))
    h.append('</select></div></div>')
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append('<p><button type="submit">Filter</button></p></form>')
    if rows:
        h.append('<table><tr><th>Filename</th><th>Type</th><th>Size</th>'
                 '<th>Visibility</th><th>Created</th></tr>')
        for a in rows:
            h.append("<tr><td><a href='%s'>%s</a><div class='mono small muted'>%s</div></td>"
                     "<td class='small'>%s</td><td class='small'>%s</td>"
                     "<td class='small'><span class='badge'>%s</span></td><td class='small'>%s</td></tr>"
                     % (_href(req, "/s/artifacts/" + a["id"]), E(a["filename"]),
                        E(a["id"]), E(a["mime"]), _size(a["size"]),
                        E(a["visibility"]), _fmt_ts(a["created_at"], "exact")))
        h.append("</table>")
    else:
        h.append('<div class="empty">No artifacts match.</div>')
    return _shell(req, "Artifacts", "".join(h))


def _size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def _page_artifact_detail(req, con, aid):
    a = store.get_artifact(con, aid)
    if not a:
        return _notfound("Artifact not found")
    data_dir = _data_dir(con)
    if req.method == "POST":
        text, files = _fields(req)
        action = text.get("action", "")
        if action == "add_revision":
            f = files.get("file")
            if f:
                filename, _ctype, payload = f
                store.add_revision(con, data_dir, aid, data=payload,
                                   change_note=text.get("change_note", ""))
        elif action == "retry_extract":
            _retry_extract(con, aid)
        elif action == "create_share":
            link = model.create_share_link(
                con, "artifact", aid,
                expires_at=(time.time() + float(text.get("expires_hours", "0") or 0) * 3600
                            if (text.get("expires_hours", "0") or "0").strip() not in ("", "0")
                            else None),
                note=text.get("note", ""))
            return _page_share_created(req, con, link, "artifact", aid)
        elif action == "add_to_collection":
            cid = text.get("collection_id", "").strip()
            if cid and model.get_collection(con, cid):
                model.add_collection_member(con, cid, artifact_id=aid,
                                            note=text.get("note", ""))
        a = store.get_artifact(con, aid)  # refresh
    revs = store.revisions(con, aid)
    nb = model.neighbors(con, "artifact", aid, direction="both", limit=300)
    chunks = [dict(r) for r in con.execute(
        "SELECT * FROM extracted_chunk WHERE artifact_id=? ORDER BY chunk_no LIMIT 60",
        (aid,))]
    jobs = [dict(r) for r in con.execute(
        "SELECT * FROM extract_job WHERE artifact_id=? ORDER BY created_at DESC LIMIT 5",
        (aid,))]
    cols = model.list_collections(con, limit=200)
    h = ["<h1>%s</h1>" % E(a["filename"]),
         '<p class="small muted mono">%s</p>' % E(aid)]
    h.append('<div class="row"><div>')
    h.append('<div class="card"><h3>Metadata</h3><table>')
    for k, v in (("filename", a["filename"]), ("mime", a["mime"]),
                 ("size", _size(a["size"])), ("created", None),
                 ("visibility", a["visibility"])):
        if k == "created":
            h.append("<tr><th>created</th><td>%s</td></tr>"
                     % _fmt_ts(a["created_at"], "exact"))
        elif k == "visibility":
            h.append("<tr><th>visibility</th><td><span class='badge'>%s</span></td></tr>"
                     % E(v))
        else:
            h.append("<tr><th>%s</th><td>%s</td></tr>" % (k, E(str(v))))
    h.append("</table>")
    h.append('<p><a class="btn" href="%s">Open content</a> '
             '<span class="muted small">served from blob store / legacy path</span></p>'
             % _href(req, "/s/artifacts/" + aid + "/content"))
    if a["mime"].startswith("image/"):
        h.append('<p><img src="%s" style="max-width:100%%;max-height:50vh;'
                 'border:1px solid #2a2f3a;border-radius:8px"></p>'
                 % _href(req, "/s/artifacts/" + aid + "/content"))
    h.append("</div>")
    h.append('<div class="card"><h3>Provenance</h3><table>')
    h.append("<tr><th>source</th><td><code>%s</code></td></tr>" % E(a["source"] or ""))
    h.append("<tr><th>source_ref</th><td class='mono small'>%s</td></tr>"
             % E(a["source_ref"] or "—"))
    h.append("<tr><th>sha256</th><td class='mono small' style='word-break:break-all'>%s</td></tr>"
             % E(a["blob_sha256"]))
    h.append("<tr><th>visibility</th><td><span class='badge'>%s</span> "
             "<span class='muted small'>private = owner token required everywhere, "
             "including thumbnails, extracted text and search snippets</span></td></tr>"
             % E(a["visibility"]))
    h.append("</table></div>")
    h.append('<div class="card"><h3>Revisions (%d)</h3>' % len(revs))
    h.append("<table><tr><th>#</th><th>sha256</th><th>created</th><th>note</th></tr>")
    for r in revs:
        h.append("<tr><td>%d</td><td class='mono small'>%s…</td><td class='small'>%s</td>"
                 "<td class='small'>%s</td></tr>"
                 % (r["rev_no"], E(r["blob_sha256"][:16]),
                    _fmt_ts(r["created_at"], "exact"), E(r["change_note"] or "")))
    h.append("</table>")
    h.append('<details><summary>Add revision</summary>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="add_revision">'
             '<label>New file bytes</label><input type="file" name="file" required>'
             '<label>Change note</label><input type="text" name="change_note">'
             '<p><button type="submit">Add revision (old stays addressable)</button></p>'
             '</form></details></div>')
    h.append("</div><div>")
    h.append('<div class="card"><h3>Relationships</h3>')
    if nb["out"] or nb["in"]:
        h.append("<h3 class='small muted'>OUT</h3>")
        for e in nb["out"]:
            h.append(_edge_html(req, con, e))
        h.append("<h3 class='small muted'>IN</h3>")
        for e in nb["in"]:
            h.append(_edge_html(req, con, e))
    else:
        h.append('<div class="empty">No relationships recorded.</div>')
    h.append("</div>")
    h.append('<div class="card"><h3>Extracted text (%d chunks)</h3>' % len(chunks))
    if jobs:
        latest = jobs[0]
        st = latest["status"]
        badge_cls = {"done": "badge ok", "failed": "badge err",
                     "unavailable": "badge warn"}.get(st, "badge")
        h.append('<p>Extraction status: <span class="%s">%s</span> ' % (badge_cls, E(st)))
        if latest["last_error"]:
            h.append('<span class="muted small">%s</span>' % E(latest["last_error"][:200]))
        h.append(' <span class="muted small">(attempts: %d)</span></p>' % latest["attempts"])
        if st in ("failed", "unavailable"):
            h.append('<p class="muted small">Use "Retry extraction" below to try again. '
                     'The original file is never modified by extraction.</p>')
    else:
        h.append('<p><span class="badge">stored only</span> '
                 '<span class="muted small">no extraction job yet</span></p>')
    h.append('<p class="muted small">Extraction jobs: ')
    for j in jobs:
        h.append('<span class="badge">%s</span> ' % E(j["status"]))
        if j["last_error"]:
            h.append('<span class="muted small">%s</span> ' % E(j["last_error"][:120]))
    h.append("</p>")
    if chunks:
        methods = sorted({(c.get("backend") or "?", c.get("method") or "?")
                          for c in chunks})
        h.append('<p class="muted small">Sources: %s</p>' %
                 E(", ".join("%s/%s" % m for m in methods)))
        for c in chunks:
            prov = []
            if c.get("backend"):
                prov.append(E(c["backend"]))
            if c.get("method"):
                prov.append(E(c["method"]))
            prov_str = (" · " + " / ".join(prov)) if prov else ""
            h.append('<details><summary>chunk %d%s%s</summary><pre>%s</pre></details>'
                     % (c["chunk_no"],
                        ((" · " + E(c["page_no"])) if c["page_no"] else ""),
                        prov_str,
                        E(c["text"])))
    else:
        h.append('<p class="muted small">No extracted text yet. '
                 "If extraction is unavailable for this type, that is recorded — never faked.</p>")
    h.append('<form method="post" action="%s" enctype="multipart/form-data" class="inline">'
             '<input type="hidden" name="action" value="retry_extract">'
             '<button type="submit">Retry extraction</button></form></div>'
             % _href(req, "/s/artifacts/" + aid))
    h.append('<div class="card"><h3>Share</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="create_share">'
             '<label>Expires in hours (blank = never)</label>'
             '<input type="text" name="expires_hours" placeholder="">'
             '<label>Note</label><input type="text" name="note">'
             '<p><button type="submit">Create share link</button></p></form>'
             '<p class="muted small">Token shown once. Access is logged.</p></div>'
             % _href(req, "/s/artifacts/" + aid))
    h.append('<div class="card"><h3>Add to collection</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="add_to_collection">'
             '<label>Collection</label><select name="collection_id">'
             % _href(req, "/s/artifacts/" + aid))
    for c in cols:
        h.append('<option value="%s">%s</option>' % (E(c["id"]), E(c["title"])))
    h.append('</select><label>Note</label><input type="text" name="note">'
             '<p><button type="submit">Add member</button></p></form></div>')
    h.append("</div></div>")
    return _shell(req, "Artifact — " + a["filename"], "".join(h))


def _page_share_created(req, con, link, scope_type, scope_id):
    token = link["token"]
    url = "/s/share/" + token
    h = ["<h1>Share link created</h1>",
         '<div class="card"><p>The token below is shown <b>once</b>. '
         "Only the hash is stored.</p>",
         '<p><label>Share URL (public-by-token)</label>'
         '<input type="text" id="shareurl" readonly value="%s"></p>'
         % E(url),
         '<p><button onclick="navigator.clipboard.writeText('
         "document.getElementById('shareurl').value)"
         '">Copy link</button></p>',
         '<p class="small muted">Scope: <code>%s</code> / <code>%s</code> · '
         "Expires: %s · Access will be logged.</p>"
         % (E(scope_type), E(scope_id),
            E(str(link["expires_at"])) if link["expires_at"] else "never"),
         "</div>",
         '<p><a href="%s">Back</a></p>' % _href(req, "/")]
    return _shell(req, "Share link created", "".join(h))


def _artifact_content(con, data_dir, aid):
    a = store.get_artifact(con, aid)
    if not a:
        return None, None
    data = store.read_artifact_bytes(con, data_dir, a)
    return a, data


def _retry_extract(con, aid):
    row = con.execute("SELECT id FROM extract_job WHERE artifact_id=? "
                      "ORDER BY created_at DESC LIMIT 1", (aid,)).fetchone()
    now = time.time()
    with con:
        if row:
            con.execute("UPDATE extract_job SET status='pending', attempts=0, "
                        "last_error='', updated_at=? WHERE id=?", (now, row[0]))
        else:
            from .store import new_id
            con.execute("INSERT INTO extract_job(id, artifact_id, kind, status, attempts,"
                        " last_error, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (new_id("job"), aid, "auto", "pending", 0, "", now, now))


def _data_dir(con):
    row = con.execute("SELECT value FROM kv WHERE key='data_dir'").fetchone()
    return row[0] if row else "."


# ---------------------------------------------------------------- gallery

def _page_gallery(req, con):
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM artifact WHERE mime LIKE 'image/%' ORDER BY created_at")]
    view = req.q("view", "")
    ids = [r["id"] for r in rows]
    extra = ""
    h = ["<h1>Gallery</h1>"]
    if view and view in ids:
        i = ids.index(view)
        prev_id = ids[i - 1] if i > 0 else None
        next_id = ids[i + 1] if i < len(ids) - 1 else None
        a = rows[i]
        h.append('<div class="viewer card">')
        h.append('<img src="%s" alt="%s">'
                 % (_href(req, "/s/artifacts/" + a["id"] + "/content"), E(a["filename"])))
        h.append('<p><b>%s</b> <span class="muted small">%s · %s</span></p>'
                 % (E(a["filename"]), E(a["mime"]), _size(a["size"])))
        nav_row = []
        if prev_id:
            nav_row.append('<a id="prevlink" class="btn" href="%s">← Prev</a>'
                           % _href(req, "/s/gallery?view=" + prev_id))
        if next_id:
            nav_row.append('<a id="nextlink" class="btn" href="%s">Next →</a>'
                           % _href(req, "/s/gallery?view=" + next_id))
        nav_row.append('<a class="btn" href="%s">Details</a>'
                       % _href(req, "/s/artifacts/" + a["id"]))
        nav_row.append('<a class="btn" href="%s">Grid</a>' % _href(req, "/s/gallery"))
        h.append("<p>" + " ".join(nav_row) + "</p>")
        h.append('<p class="muted small"><span class="kbd">←</span> '
                 '<span class="kbd">→</span> to navigate</p>')
        h.append("</div>")
        h.append('<div class="card"><h3>Linked records</h3>')
        nb = model.neighbors(con, "artifact", a["id"], direction="both", limit=100)
        if nb["out"] or nb["in"]:
            for e in nb["out"] + nb["in"]:
                h.append(_edge_html(req, con, e))
        else:
            h.append('<p class="muted small">No session/event links recorded. '
                     "Unresolved rather than invented.</p>")
        h.append('<h3>Metadata</h3><table>'
                 '<tr><th>id</th><td class="mono small">%s</td></tr>'
                 '<tr><th>sha256</th><td class="mono small">%s</td></tr>'
                 '<tr><th>source</th><td><code>%s</code></td></tr>'
                 '<tr><th>created</th><td>%s</td></tr></table></div>'
                 % (E(a["id"]), E(a["blob_sha256"][:32] + "…"),
                    E(a["source"] or ""), _fmt_ts(a["created_at"], "exact")))
        extra = ("<script>document.addEventListener('keydown',function(e){"
                 "var p=document.getElementById('prevlink'),"
                 "n=document.getElementById('nextlink');"
                 "if(e.key==='ArrowLeft'&&p){p.click()}"
                 "if(e.key==='ArrowRight'&&n){n.click()}});</script>")
    else:
        if rows:
            h.append('<div class="gallery">')
            for a in rows:
                h.append('<a href="%s"><img loading="lazy" src="%s" alt="%s" '
                         'title="%s"></a>'
                         % (_href(req, "/s/gallery?view=" + a["id"]),
                            _href(req, "/s/artifacts/" + a["id"] + "/content"),
                            E(a["filename"]), E(a["filename"])))
            h.append("</div>")
        else:
            h.append('<div class="empty">No image artifacts yet.</div>')
    return _shell(req, "Gallery", "".join(h), extra_head=extra)


# ---------------------------------------------------------------- collections

def _page_collections(req, con):
    if req.method == "POST":
        text, _ = _fields(req)
        c = model.create_collection(con, text.get("title", "").strip() or "Untitled",
                                    text.get("description", "").strip(),
                                    visibility=text.get("visibility", "private") or "private")
        return _page_collection_detail(req, con, c["id"])
    cols = model.list_collections(con, limit=200)
    h = ["<h1>Collections</h1>",
         '<div class="card"><h3>New collection</h3>'
         '<form method="post" action="%s" enctype="multipart/form-data">'
         '<label>Title</label><input type="text" name="title" required>'
         '<label>Description</label><textarea name="description"></textarea>'
         '<label>Visibility</label><select name="visibility">'
         '<option value="private">private</option>'
         '<option value="public-link">public-link</option></select>'
         '<p><button type="submit">Create collection</button></p></form></div>'
         % _href(req, "/s/collections")]
    if cols:
        h.append('<div class="grid">')
        for c in cols:
            n = con.execute("SELECT COUNT(*) FROM collection_member WHERE collection_id=?",
                            (c["id"],)).fetchone()[0]
            h.append('<div class="card"><h3><a href="%s">%s</a></h3>'
                     '<p class="muted small">%s</p>'
                     '<p class="small"><span class="badge">%s</span> %d members</p></div>'
                     % (_href(req, "/s/collections/" + c["id"]), E(c["title"]),
                        E(c["description"] or ""), E(c["visibility"]), n))
        h.append("</div>")
    else:
        h.append('<div class="empty">No collections yet.</div>')
    return _shell(req, "Collections", "".join(h))


def _page_collection_detail(req, con, cid):
    c = model.get_collection(con, cid)
    if not c:
        return _notfound("Collection not found")
    if req.method == "POST":
        text, _ = _fields(req)
        action = text.get("action", "")
        if action == "add_member":
            art_id = text.get("artifact_id", "").strip() or None
            child_id = text.get("child_collection_id", "").strip() or None
            if art_id and store.get_artifact(con, art_id):
                model.add_collection_member(con, cid, artifact_id=art_id,
                                            note=text.get("note", ""))
            elif child_id and model.get_collection(con, child_id):
                model.add_collection_member(con, cid, child_collection_id=child_id,
                                            note=text.get("note", ""))
        elif action == "create_share":
            link = model.create_share_link(con, "collection", cid, note=text.get("note", ""))
            return _page_share_created(req, con, link, "collection", cid)
    members = model.collection_members(con, cid)
    h = ["<h1>%s</h1>" % E(c["title"]),
         '<p class="muted">%s</p>' % E(c["description"] or ""),
         '<p class="small"><span class="badge">%s</span> '
         '<span class="mono muted">%s</span></p>' % (E(c["visibility"]), E(cid))]
    h.append('<div class="row"><div>')
    h.append("<h2>Entries (%d)</h2>" % len(members))
    if members:
        h.append('<table><tr><th>#</th><th>Entry</th><th>Note</th></tr>')
        for i, m in enumerate(members, 1):
            if m["artifact_id"]:
                a = store.get_artifact(con, m["artifact_id"])
                label = ('<a href="%s">%s</a>'
                         % (_href(req, "/s/artifacts/" + m["artifact_id"]),
                            E(a["filename"] if a else m["artifact_id"])))
            else:
                ch = model.get_collection(con, m["child_collection_id"])
                label = ('<a href="%s">↳ collection: %s</a>'
                         % (_href(req, "/s/collections/" + m["child_collection_id"]),
                            E(ch["title"] if ch else m["child_collection_id"])))
            h.append("<tr><td>%d</td><td>%s</td><td class='small muted'>%s</td></tr>"
                     % (i, label, E(m["note"] or "")))
        h.append("</table>")
    else:
        h.append('<div class="empty">No entries yet. Members reference artifacts '
                 "in place — nothing is copied.</div>")
    h.append('<div class="card"><h3>Add member</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="add_member">'
             '<label>Artifact id</label><input type="text" name="artifact_id" '
             'placeholder="art_...">'
             '<label>…or child collection id</label>'
             '<input type="text" name="child_collection_id" placeholder="col_...">'
             '<label>Note</label><input type="text" name="note">'
             '<p><button type="submit">Add member</button></p></form></div>')
    h.append('<div class="card"><h3>Manifest</h3>'
             '<details><summary>View manifest (JSON)</summary><pre>%s</pre></details>'
             '<p><a class="btn" href="%s">Export manifest</a></p></div>'
             % (E(_collection_manifest(con, cid)),
                _href(req, "/s/collections/" + cid + "/manifest")))
    h.append('<div class="card"><h3>Share</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="create_share">'
             '<label>Note</label><input type="text" name="note">'
             '<p><button type="submit">Create share link for collection</button></p>'
             '</form></div>' % _href(req, "/s/collections/" + cid))
    h.append("</div></div>")
    return _shell(req, "Collection — " + c["title"], "".join(h))


def _collection_manifest(con, cid):
    c = model.get_collection(con, cid)
    members = model.collection_members(con, cid)
    out = {"id": c["id"], "title": c["title"], "description": c["description"],
           "visibility": c["visibility"], "created_at": c["created_at"],
           "members": []}
    for m in members:
        entry = {"position": m["position"], "note": m["note"],
                 "added_at": m["added_at"]}
        if m["artifact_id"]:
            a = store.get_artifact(con, m["artifact_id"])
            entry["artifact_id"] = m["artifact_id"]
            if a:
                entry.update({"filename": a["filename"], "mime": a["mime"],
                              "size": a["size"], "sha256": a["blob_sha256"]})
        else:
            entry["child_collection_id"] = m["child_collection_id"]
        out["members"].append(entry)
    return json.dumps(out, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- graph

def _page_graph(req, con):
    seed = req.q("seed", "").strip()
    try:
        depth = max(1, min(int(req.q("depth", "2")), 4))
    except ValueError:
        depth = 2
    rel_f = req.q("rel", "").strip()
    rel_allow = [r.strip() for r in rel_f.split(",") if r.strip()] or None
    h = ["<h1>Graph</h1>",
         '<div class="card"><form method="get" action="/s/graph">'
         '<div class="row"><div><label>Seed (type:id)</label>'
         '<input type="text" name="seed" value="%s" placeholder="session:ses_...">'
         '</div><div><label>Depth (1–4)</label>'
         '<input type="number" name="depth" value="%d" min="1" max="4"></div>'
         '<div><label>Relationship filter (comma-separated, blank = all)</label>'
         '<input type="text" name="rel" value="%s"></div></div>'
         % (E(seed), depth, E(rel_f))]
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append('<p><button type="submit">Traverse</button></p></form>'
             '<p class="muted small">Bounded breadth-first traversal. '
             "Inferred edges are included but always flagged.</p></div>")
    if seed:
        if ":" not in seed:
            h.append('<div class="card"><span class="badge warn">bad seed</span> '
                     'Use <code>type:id</code>, e.g. <code>session:ses_abc</code>.</div>')
        else:
            stype, sid = seed.split(":", 1)
            try:
                res = model.traverse(con, [(stype.strip(), sid.strip())],
                                     rel_allow=rel_allow, max_depth=depth)
            except ValueError as e:
                h.append('<div class="card">%s</div>' % E(str(e)))
                return _shell(req, "Graph", "".join(h))
            edge_by_id = {e["id"]: e for e in res["edges"]}
            by_depth = {}
            for n in res["nodes"]:
                by_depth.setdefault(n["depth"], []).append(n)
            h.append('<div class="card"><h3>%d nodes · %d edges '
                     '(depth ≤ %d)</h3>' % (len(res["nodes"]), len(res["edges"]), depth))
            for d in sorted(by_depth):
                h.append('<div class="depth"><h3>depth %d (%d)</h3>' % (d, len(by_depth[d])))
                for n in by_depth[d]:
                    crumbs = []
                    for eid in n.get("path", []):
                        e = edge_by_id.get(eid)
                        if e:
                            crumbs.append(E(e["rel"]))
                    crumb_html = (' <span class="muted small">via %s</span>'
                                  % " → ".join(crumbs)) if crumbs else ""
                    h.append("<div class='small'>%s%s</div>"
                             % (_obj_link(req, con, n["obj_type"], n["obj_id"]),
                                crumb_html))
                h.append("</div>")
            h.append("<h3>Edges</h3>")
            for e in res["edges"]:
                h.append(_edge_html(req, con, e))
            h.append("</div>")
    return _shell(req, "Graph", "".join(h))


# ---------------------------------------------------------------- search

def _page_search(req, con):
    q = req.q("q", "").strip()
    doc_type = req.q("doc_type", "").strip()
    h = ["<h1>Search</h1>",
         '<div class="card"><form method="get" action="/s/search">'
         '<div class="row"><div><label>Query</label>'
         '<input type="text" name="q" value="%s"></div>'
         '<div><label>Document type</label><select name="doc_type">'
         '<option value="">all</option>' % E(q)]
    for dt in ("artifact", "event", "decision", "task", "claim", "session", "handoff"):
        h.append('<option value="%s"%s>%s</option>'
                 % (dt, " selected" if dt == doc_type else "", dt))
    h.append("</select></div><div><label>Workspace id (optional)</label>"
             '<input type="text" name="workspace_id" value="%s"></div></div>'
             % E(req.q("workspace_id", "")))
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append('<p><button type="submit">Search</button></p></form>'
             '<p class="muted small">Layers: <b>exact</b> (id / filename / sha256 / '
             "legacy id) then <b>full-text</b> (FTS5 BM25). Semantic retrieval is "
             "deferred — no embedding backend configured.</p></div>")
    if q:
        res, problem = _do_search(con, q, {"doc_type": doc_type} if doc_type else {},
                                  req.q("workspace_id", "").strip() or None)
        if problem:
            h.append('<div class="card"><span class="badge warn">%s</span> '
                     '<span class="muted">%s</span></div>'
                     % (E("search initializing" if "initializing" in problem else "search unavailable"),
                        E(problem)))
        else:
            results = res.get("results", []) if isinstance(res, dict) else (res or [])
            h.append('<div class="card"><h3>%d results '
                     '<span class="muted small">(considered %d)</span></h3>'
                     % (len(results),
                        res.get("total_considered", 0) if isinstance(res, dict) else 0))
            if isinstance(res, dict):
                h.append('<p class="small muted">layers used: %s</p>'
                         % E(", ".join(res.get("layers_used", [])) or "none"))
                for nk, nv in (res.get("layer_notes") or {}).items():
                    h.append('<p class="small"><span class="badge warn">%s</span> %s</p>'
                             % (E(nk), E(str(nv))))
            for r in results:
                h.append('<div style="margin:10px 0;padding-bottom:8px;'
                         'border-bottom:1px solid #2a2f3a">'
                         '<span class="badge">%s</span> %s'
                         '<div class="small">%s</div>'
                         '<div class="small muted">%s</div></div>'
                         % (E(str(r.get("layer", "?"))),
                            _obj_link(req, con, r.get("doc_type", ""),
                                      r.get("doc_id", "")),
                            _safe_snippet(r.get("snippet") or ""),
                            E(str(r.get("why", "")))))
            h.append("</div>")
    return _shell(req, "Search", "".join(h))


def _safe_snippet(snip):
    """Escape a snippet but keep SQLite's <b> match markers as real markup."""
    s = snip.replace("<b>", "\x00B\x00").replace("</b>", "\x00b\x00")
    return E(s).replace("\x00B\x00", "<b>").replace("\x00b\x00", "</b>")


def _do_search(con, q, filters, workspace_id=None):
    if _search is None:
        return None, "search initializing"
    fn = getattr(_search, "search_all", None)
    if not callable(fn):
        return None, "search initializing"
    try:
        return fn(con, q, filters, limit=50, workspace_id=workspace_id), None
    except TypeError:
        try:
            return fn(con, q), None
        except Exception as e:
            return None, "search error: %s" % e
    except Exception as e:
        return None, "search error: %s" % e


# ---------------------------------------------------------------- context builder

def _page_context(req, con):
    h = ["<h1>Context builder</h1>",
         '<div class="card"><form method="get" action="/s/context">'
         '<label>Question / retrieval objective</label>'
         '<textarea name="question">%s</textarea>'
         '<label>Seed ids (one per line, type:id)</label>'
         '<textarea name="seeds">%s</textarea>'
         '<label>Token budget (estimate)</label>'
         '<input type="number" name="budget" value="%s" min="100">'
         % (E(req.q("question", "")), E(req.q("seeds", "")), E(req.q("budget", "4000") or "4000"))]
    if req.token:
        h.append('<input type="hidden" name="token" value="%s">' % E(req.token))
    h.append('<p><button type="submit">Assemble package</button></p></form></div>')
    question = req.q("question", "").strip()
    if question:
        seeds = []
        for line in req.q("seeds", "").splitlines():
            line = line.strip()
            if line and ":" in line:
                t, i = line.split(":", 1)
                seeds.append((t.strip(), i.strip()))
        try:
            budget = int(req.q("budget", "4000") or "4000")
        except ValueError:
            budget = 4000
        pkg, problem = _do_context(con, question, seeds, budget)
        if problem:
            h.append('<div class="card"><span class="badge warn">%s</span> '
                     '<span class="muted">%s</span></div>'
                     % (E("context initializing" if "initializing" in problem else "unavailable"),
                        E(problem)))
        else:
            h.append(_render_package(req, con, pkg))
    return _shell(req, "Context builder", "".join(h))


def _do_context(con, question, seeds, budget):
    if _context is None:
        return None, "search initializing"
    fn = getattr(_context, "build_package", None)
    if not callable(fn):
        return None, "search initializing"
    try:
        return fn(con, question, seeds, budget), None
    except TypeError:
        try:
            return fn(con, question), None
        except Exception as e:
            return None, "context error: %s" % e
    except Exception as e:
        return None, "context error: %s" % e


def _render_package(req, con, pkg):
    h = ['<div class="card"><h2>Context package</h2>']
    h.append('<p class="small muted">Question: %s</p>' % E(pkg.get("question", "")))
    h.append('<p class="small muted">retrieval <code class="mono">%s</code> · '
             'algorithm <code>%s</code> · budget %s tokens</p>'
             % (E(str(pkg.get("retrieval_id", ""))), E(str(pkg.get("algorithm", ""))),
                E(str(pkg.get("budget_tokens", "")))))
    est = pkg.get("token_estimate")
    h.append('<p class="small">Token estimate: <b>%s</b> '
             '<span class="badge warn">estimate — not a model token count</span></p>'
             % E(str(est) if est is not None else "n/a"))
    h.append("<h3>Sources (%d)</h3>" % len(pkg.get("sources", [])))
    for s in pkg.get("sources", []):
        h.append('<div class="card" style="margin:8px 0"><div>%s'
                 % _obj_link(req, con, s.get("doc_type", ""), s.get("doc_id", "")))
        if s.get("title"):
            h.append(' <span class="muted small">%s</span>' % E(str(s["title"])[:120]))
        h.append(' <span class="badge">~%s tok</span></div>'
                 % E(str(s.get("tokens_est", "?"))))
        if s.get("locator"):
            h.append('<div class="small muted">locator: %s</div>' % E(str(s["locator"])))
        if s.get("excerpt"):
            h.append("<pre>%s</pre>" % E(str(s["excerpt"])[:2000]))
        if s.get("why_included"):
            h.append('<div class="small"><b>Why included:</b> %s</div>'
                     % E(str(s["why_included"])))
        if s.get("provenance"):
            h.append('<div class="small muted">provenance: %s</div>'
                     % E(str(s["provenance"])))
        h.append("</div>")
    if pkg.get("relationships"):
        h.append("<h3>Relationships among included sources</h3>")
        for e in pkg["relationships"]:
            if isinstance(e, dict) and "rel" in e:
                h.append(_edge_html(req, con, e))
            else:
                h.append("<div class='small'>%s</div>" % E(str(e)))
    if pkg.get("uncertainties"):
        h.append("<h3>Known uncertainties</h3><ul class='tight'>")
        for u in pkg["uncertainties"]:
            h.append("<li class='small'>%s</li>" % E(str(u)))
        h.append("</ul>")
    if pkg.get("missing_evidence"):
        h.append("<h3>Missing evidence</h3><ul class='tight'>")
        for m in pkg["missing_evidence"]:
            h.append("<li class='small'>%s</li>" % E(str(m)))
        h.append("</ul>")
    if pkg.get("additional_index"):
        h.append("<h3>Additional available material (not loaded)</h3><ul class='tight'>")
        for a in pkg["additional_index"]:
            if isinstance(a, dict):
                h.append("<li class='small'>%s <span class='muted'>(%s)</span></li>"
                         % (_obj_link(req, con, a.get("doc_type", ""),
                                      a.get("doc_id", "")),
                            E(str(a.get("why_not_included", "")))))
            else:
                h.append("<li class='small'>%s</li>" % E(str(a)))
        h.append("</ul>")
    omitted = pkg.get("omitted")
    if omitted:
        if isinstance(omitted, dict):
            h.append('<p class="small muted">Omitted %s: %s</p>'
                     % (E(str(omitted.get("count", ""))),
                        E(str(omitted.get("reasons", "")))))
        else:
            h.append('<p class="small muted">Omitted: %s</p>' % E(str(omitted)))
    h.append("</div>")
    return "".join(h)


# ---------------------------------------------------------------- checkpoints

def _page_checkpoints(req, con):
    if req.method == "POST":
        text, _ = _fields(req)
        raw = text.get("body_json", "").strip()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            return _shell(req, "Checkpoints",
                          "<h1>Checkpoints</h1>"
                          '<div class="card"><span class="badge warn">invalid JSON</span> '
                          '<span class="muted">%s</span></div>' % E(str(e)))
        c = model.create_checkpoint(con, text.get("title", "").strip() or "Untitled",
                                    body,
                                    workspace_id=text.get("workspace_id", "").strip() or None,
                                    session_id=text.get("session_id", "").strip() or None)
        return _page_checkpoint_detail(req, con, c["id"])
    chks = model.list_checkpoints(con, limit=100)
    wss = model.list_workspaces(con)
    h = ["<h1>Checkpoints</h1>",
         '<div class="card"><h3>New checkpoint</h3>'
         '<form method="post" action="%s" enctype="multipart/form-data">'
         '<div class="row"><div><label>Title</label>'
         '<input type="text" name="title" required></div>'
         '<div><label>Workspace</label><select name="workspace_id">'
         '<option value="">—</option>' % _href(req, "/s/checkpoints")]
    for w in wss:
        h.append('<option value="%s">%s</option>' % (E(w["id"]), E(w["title"])))
    h.append('</select></div><div><label>Session id (optional)</label>'
             '<input type="text" name="session_id"></div></div>'
             '<label>Body JSON (references canonical ids)</label>'
             '<textarea name="body_json" style="min-height:160px" '
             'placeholder=\'{"objective": "...", "session_ids": ["ses_..."]}\'></textarea>'
             '<p><button type="submit">Create checkpoint (immutable)</button></p>'
             '</form></div>')
    if chks:
        h.append("<table><tr><th>Title</th><th>Schema</th><th>Created</th></tr>")
        for c in chks:
            h.append("<tr><td><a href='%s'>%s</a></td><td>v%d</td><td class='small'>%s</td></tr>"
                     % (_href(req, "/s/checkpoints/" + c["id"]), E(c["title"]),
                        c["schema_version"], _fmt_ts(c["created_at"], "exact")))
        h.append("</table>")
    else:
        h.append('<div class="empty">No checkpoints yet.</div>')
    return _shell(req, "Checkpoints", "".join(h))


_ID_RE = None


def _linkify_ids(req, con, text):
    """Link bare canonical ids (prefix_xxx) found in text to their pages."""
    import re
    global _ID_RE
    if _ID_RE is None:
        _ID_RE = re.compile(r"\b(ws|ses|ev|art|rev|edg|dec|tsk|clm|evd|col|mem|lnk|chk|ho|aud)_[a-z0-9]{6,}\b")
    route = {"ws": "workspaces", "ses": "sessions", "ev": "events", "art": "artifacts",
             "dec": "decisions", "tsk": "tasks", "clm": "claims", "col": "collections",
             "chk": "checkpoints", "ho": "handoffs"}
    def repl(m):
        oid = m.group(0)
        prefix = oid.split("_")[0]
        r = route.get(prefix)
        if r:
            return '<a class="mono" href="%s">%s</a>' % (_href(req, "/s/" + r + "/" + oid), E(oid))
        return '<span class="mono">%s</span>' % E(oid)
    return _ID_RE.sub(repl, E(text))


def _page_checkpoint_detail(req, con, cid):
    c = model.get_checkpoint(con, cid)
    if not c:
        return _notfound("Checkpoint not found")
    try:
        body = json.loads(c["body_json"])
    except (json.JSONDecodeError, TypeError):
        body = {"_raw": c["body_json"]}
    pretty = json.dumps(body, indent=2, ensure_ascii=False)
    h = ["<h1>%s</h1>" % E(c["title"]),
         '<p class="small muted mono">%s · schema v%d · created %s</p>'
         % (E(cid), c["schema_version"], _fmt_ts(c["created_at"], "exact"))]
    ws = model.get_workspace(con, c["workspace_id"]) if c["workspace_id"] else None
    if ws:
        h.append('<p class="small">Workspace: <a href="%s">%s</a></p>'
                 % (_href(req, "/s/workspaces/" + ws["id"]), E(ws["title"])))
    if c["session_id"]:
        s = model.get_session(con, c["session_id"])
        h.append('<p class="small">Session: <a href="%s">%s</a></p>'
                 % (_href(req, "/s/sessions/" + c["session_id"]),
                    E(s["title"] if s else c["session_id"])))
    h.append('<div class="card"><h3>Body — canonical references</h3>'
             '<pre>%s</pre></div>' % _linkify_ids(req, con, pretty))
    h.append('<p class="muted small">Checkpoints are immutable snapshots; '
             "later sessions never mutate them.</p>")
    return _shell(req, "Checkpoint — " + c["title"], "".join(h))


# ---------------------------------------------------------------- handoffs

def _page_handoffs(req, con):
    if req.method == "POST":
        text, _ = _fields(req)
        ho = model.create_handoff(
            con, text.get("objective", "").strip() or "Untitled handoff",
            payload_text=text.get("payload_text", "") or None,
            workspace_id=text.get("workspace_id", "").strip() or None,
            from_session_id=text.get("from_session_id", "").strip() or None,
            to_env=text.get("to_env", "").strip())
        model.audit(con, "owner", "handoff_create", "handoff", ho["id"], {})
        return _page_handoff_detail(req, con, ho["id"])
    hos = model.list_handoffs(con, limit=100)
    wss = model.list_workspaces(con)
    h = ["<h1>Handoffs</h1>",
         '<div class="card"><h3>New handoff</h3>'
         '<form method="post" action="%s" enctype="multipart/form-data">'
         '<label>Objective</label><input type="text" name="objective" required>'
         '<div class="row"><div><label>Workspace</label><select name="workspace_id">'
         '<option value="">—</option>' % _href(req, "/s/handoffs")]
    for w in wss:
        h.append('<option value="%s">%s</option>' % (E(w["id"]), E(w["title"])))
    h.append('</select></div><div><label>From session id</label>'
             '<input type="text" name="from_session_id"></div>'
             '<div><label>To environment</label>'
             '<input type="text" name="to_env" placeholder="claude-code / agent name">'
             '</div></div>'
             '<label>Payload (stored verbatim as immutable artifact)</label>'
             '<textarea name="payload_text" style="min-height:140px"></textarea>'
             '<p><button type="submit">Create handoff</button></p></form></div>')
    if hos:
        h.append("<table><tr><th>Objective</th><th>Status</th><th>Updated</th></tr>")
        for x in hos:
            h.append("<tr><td><a href='%s'>%s</a></td>"
                     "<td><span class='badge'>%s</span></td><td class='small'>%s</td></tr>"
                     % (_href(req, "/s/handoffs/" + x["id"]),
                        E((x["objective"] or "")[:80] or x["id"]),
                        E(x["status"]), _fmt_ts(x["updated_at"], "exact")))
        h.append("</table>")
    else:
        h.append('<div class="empty">No handoffs yet.</div>')
    return _shell(req, "Handoffs", "".join(h))


_PIPE_ORDER = ["prepared", "ready", "authorized", "sent", "acknowledged",
               "executing", "completed"]


def _page_handoff_detail(req, con, hid):
    x = model.get_handoff(con, hid)
    if not x:
        return _notfound("Handoff not found")
    data_dir = _data_dir(con)
    err_msg = ""
    if req.method == "POST":
        text, _ = _fields(req)
        if text.get("action") == "set_status":
            try:
                model.set_handoff_status(con, hid, text.get("status", ""))
            except (ValueError, KeyError) as e:
                err_msg = str(e)
            x = model.get_handoff(con, hid)
    h = ["<h1>Handoff</h1>",
         '<p class="small muted mono">%s</p>' % E(hid),
         '<div class="card"><h3>Objective</h3><p>%s</p>'
         '<p class="small muted">To: <code>%s</code></p>'
         % (E(x["objective"] or ""), E(x["to_env"] or "—"))]
    # status pipeline
    h.append("<h3>Status</h3><div class='pipeline'>")
    cur = x["status"]
    if cur in _PIPE_ORDER:
        idx = _PIPE_ORDER.index(cur)
        for i, st in enumerate(_PIPE_ORDER):
            cls = "pipe done" if i < idx else ("pipe cur" if i == idx else "pipe")
            h.append('<span class="%s">%s</span>' % (cls, E(st)))
            if i < len(_PIPE_ORDER) - 1:
                h.append('<span class="pipe-arrow">→</span>')
    else:
        h.append('<span class="pipe cur">%s</span>' % E(cur))
    h.append("</div>")
    allowed = _HANDOFF_FLOW.get(cur, ())
    if err_msg:
        h.append('<p><span class="badge warn">%s</span></p>' % E(err_msg))
    if allowed:
        h.append('<form method="post" action="%s" enctype="multipart/form-data" '
                 'class="inline"><input type="hidden" name="action" value="set_status">'
                 '<label>Transition (only legal transitions offered)</label>'
                 '<select name="status">' % _href(req, "/s/handoffs/" + hid))
        for st in allowed:
            h.append('<option value="%s">%s</option>' % (E(st), E(st)))
        h.append('</select> <button type="submit">Apply</button></form>')
    else:
        h.append('<p class="muted small">Terminal state — no further transitions.</p>')
    h.append('<p class="muted small"><b>ready ≠ sent.</b> <b>acknowledged ≠ completed.</b> '
             '<b>completed ≠ verified.</b></p></div>')
    # payload
    h.append('<div class="card"><h3>Payload (verbatim, immutable)</h3>')
    payload_text, verified = "", None
    if x["payload_artifact_id"]:
        a = store.get_artifact(con, x["payload_artifact_id"])
        if a:
            try:
                raw = store.read_artifact_bytes(con, data_dir, a)
                payload_text = raw.decode("utf-8", "replace")
            except Exception as e:
                payload_text = "[bytes unreadable: %s]" % e
            verified = (x["payload_hash"] == a["blob_sha256"]) if x["payload_hash"] else None
            h.append('<p class="small">Artifact: <a href="%s">%s</a></p>'
                     % (_href(req, "/s/artifacts/" + a["id"]), E(a["id"])))
    h.append('<p class="small">sha256: <code class="mono">%s</code> '
             % E(x["payload_hash"] or "—"))
    if verified is True:
        h.append('<span class="badge ok">hash verified ✓</span>')
    elif verified is False:
        h.append('<span class="badge warn">HASH MISMATCH</span>')
    h.append("</p>")
    h.append('<textarea id="payload" readonly style="min-height:220px">%s</textarea>'
             % E(payload_text))
    h.append('<p><button onclick="navigator.clipboard.writeText('
             'document.getElementById(\'payload\').value)">Copy verbatim payload</button> '
             '<span class="muted small">Compare the sha256 above with the submitted copy '
             "to verify byte-identical relay.</span></p></div>")
    if x["from_session_id"]:
        h.append('<p class="small">From session: <a href="%s">%s</a></p>'
                 % (_href(req, "/s/sessions/" + x["from_session_id"]),
                    E(x["from_session_id"])))
    return _shell(req, "Handoff", "".join(h))


# ---------------------------------------------------------------- detail stubs for decisions/tasks/claims/events

def _page_decision_detail(req, con, did):
    d = model.get_decision(con, did)
    if not d:
        return _notfound("Decision not found")
    h = ["<h1>%s</h1>" % E(d["title"]),
         '<p><span class="badge">%s</span> <span class="muted small mono">%s</span></p>'
         % (E(d["status"]), E(did))]
    if d["body"]:
        h.append('<div class="card"><pre>%s</pre></div>' % E(d["body"]))
    h.append('<p class="small muted">Decided: %s</p>'
             % _fmt_ts(d["decided_at"], d["time_quality"]))
    if d["session_id"]:
        h.append('<p class="small">Session: <a href="%s">%s</a></p>'
                 % (_href(req, "/s/sessions/" + d["session_id"]), E(d["session_id"])))
    if d["supersedes_id"]:
        h.append('<p class="small">Supersedes: <a href="%s">%s</a> '
                 '(the earlier record is preserved, never silently replaced)</p>'
                 % (_href(req, "/s/decisions/" + d["supersedes_id"]),
                    E(d["supersedes_id"])))
    nb = model.neighbors(con, "decision", did, direction="both", limit=100)
    h.append('<div class="card"><h3>Relationships</h3>')
    for e in nb["out"] + nb["in"]:
        h.append(_edge_html(req, con, e))
    h.append("</div>")
    return _shell(req, "Decision — " + d["title"], "".join(h))


def _page_task_detail(req, con, tid):
    t = model.get_task(con, tid)
    if not t:
        return _notfound("Task not found")
    if req.method == "POST":
        text, _ = _fields(req)
        if text.get("action") == "set_status":
            model.set_task_status(con, tid, text.get("status", "open"))
            t = model.get_task(con, tid)
    h = ["<h1>%s</h1>" % E(t["title"]),
         '<p><span class="badge">%s</span> <span class="badge">%s</span> '
         '<span class="muted small mono">%s</span></p>'
         % (E(t["kind"]), E(t["status"]), E(tid))]
    if t["body"]:
        h.append('<div class="card"><pre>%s</pre></div>' % E(t["body"]))
    h.append('<div class="card"><h3>Set status</h3>'
             '<form method="post" action="%s" enctype="multipart/form-data">'
             '<input type="hidden" name="action" value="set_status">'
             '<select name="status">' % _href(req, "/s/tasks/" + tid))
    for st in ("open", "in_progress", "blocked", "done", "cancelled",
               "answered", "unresolved"):
        h.append('<option value="%s"%s>%s</option>'
                 % (st, " selected" if st == t["status"] else "", st))
    h.append('</select> <button type="submit">Apply</button></form>'
             '<p class="muted small">A question stays open/unresolved until an '
             "explicit resolution event — never auto-resolved.</p></div>")
    nb = model.neighbors(con, "task", tid, direction="both", limit=100)
    h.append('<div class="card"><h3>Relationships</h3>')
    for e in nb["out"] + nb["in"]:
        h.append(_edge_html(req, con, e))
    h.append("</div>")
    return _shell(req, "Task — " + t["title"], "".join(h))


def _page_claim_detail(req, con, cid):
    c = model.get_claim(con, cid)
    if not c:
        return _notfound("Claim not found")
    ev = model.claim_evidence(con, cid)
    h = ["<h1>%s</h1>" % E(c["title"]),
         '<p><span class="badge">confidence: %s</span> '
         '<span class="badge">%s</span> '
         '<span class="muted small mono">%s</span></p>'
         % (E(c["confidence"]), E(c["verification_status"]), E(cid))]
    if c["body"]:
        h.append('<div class="card"><pre>%s</pre></div>' % E(c["body"]))
    h.append('<div class="card"><h3>Evidence (%d)</h3>' % len(ev))
    if ev:
        h.append("<table><tr><th>Stance</th><th>Source</th><th>Locator</th><th>Note</th></tr>")
        for e in ev:
            if e["artifact_id"]:
                src = _obj_link(req, con, "artifact", e["artifact_id"])
            elif e["event_id"]:
                src = _obj_link(req, con, "event", e["event_id"])
            else:
                src = '<span class="muted">—</span>'
            h.append("<tr><td><span class='badge'>%s</span></td><td>%s</td>"
                     "<td class='small'>%s</td><td class='small'>%s</td></tr>"
                     % (E(e["stance"]), src, E(e["locator"] or ""), E(e["note"] or "")))
        h.append("</table>")
    else:
        h.append('<div class="empty">No evidence linked yet.</div>')
    h.append("</div>")
    return _shell(req, "Claim — " + c["title"], "".join(h))


def _page_event_detail(req, con, eid):
    e = model.get_event(con, eid)
    if not e:
        return _notfound("Event not found")
    s = model.get_session(con, e["session_id"])
    h = ["<h1>Event #%d</h1>" % e["seq"],
         '<p><span class="badge">%s</span> <span class="muted small mono">%s</span></p>'
         % (E(e["kind"]), E(eid)),
         '<p class="small">Session: <a href="%s">%s</a> · Actor: %s · Role: %s · %s</p>'
         % (_href(req, "/s/sessions/" + e["session_id"]),
            E(s["title"] if s else e["session_id"]),
            E(e["actor"] or "—"), E(e["role"] or "—"),
            _fmt_ts(e["ts"], e["time_quality"]))]
    if e["text"]:
        h.append('<div class="card"><pre>%s</pre></div>' % E(e["text"]))
    if e["content_hash"]:
        h.append('<p class="small muted">content sha256: <code class="mono">%s</code></p>'
                 % E(e["content_hash"]))
    nb = model.neighbors(con, "event", eid, direction="both", limit=100)
    h.append('<div class="card"><h3>Relationships</h3>')
    for x in nb["out"] + nb["in"]:
        h.append(_edge_html(req, con, x))
    h.append("</div>")
    return _shell(req, "Event", "".join(h))


# ---------------------------------------------------------------- settings

def _page_settings(req, con):
    data_dir = _data_dir(con)
    if req.method == "POST":
        text, _ = _fields(req)
        if text.get("action") == "retry_job":
            jid = text.get("job_id", "").strip()
            if jid:
                with con:
                    con.execute("UPDATE extract_job SET status='pending', attempts=0, "
                                "last_error='', updated_at=? WHERE id=?",
                                (time.time(), jid))
    # storage usage
    blob_bytes, blob_files = 0, 0
    blobs_root = os.path.join(data_dir, "blobs")
    for root, _dirs, files in os.walk(blobs_root):
        for f in files:
            p = os.path.join(root, f)
            try:
                blob_bytes += os.path.getsize(p)
                blob_files += 1
            except OSError:
                pass
    db_path = os.path.join(data_dir, "substrate.db")
    try:
        db_bytes = os.path.getsize(db_path)
    except OSError:
        db_bytes = 0
    counts = _counts(con)
    jobs = [dict(r) for r in con.execute(
        "SELECT j.*, a.filename FROM extract_job j LEFT JOIN artifact a "
        "ON a.id=j.artifact_id ORDER BY j.updated_at DESC LIMIT 100")]
    fails = sum(1 for j in jobs if j["status"] == "failed")
    audit = model.audit_tail(con, limit=50)
    schema_v = con.execute("SELECT value FROM kv WHERE key='schema_version'").fetchone()
    h = ["<h1>Settings</h1>"]
    h.append('<div class="row"><div><div class="card"><h3>Storage</h3>'
             '<table><tr><th>data_dir</th><td class="mono small">%s</td></tr>'
             '<tr><th>blob store</th><td>%s in %d files</td></tr>'
             '<tr><th>database</th><td>%s</td></tr>'
             '<tr><th>schema version</th><td>v%s</td></tr></table></div></div>'
             '<div><div class="card"><h3>Index health (row counts)</h3><table>'
             % (E(data_dir), _size(blob_bytes), blob_files, _size(db_bytes),
                E(schema_v[0] if schema_v else "?")))
    for k, ck in (("workspace", "workspaces"), ("session", "sessions"),
                  ("event", "events"), ("artifact", "artifacts"),
                  ("edge", "edges"), ("decision", "decisions"),
                  ("task", "tasks"), ("claim", "claims"),
                  ("collection", "collections"), ("checkpoint", "checkpoints"),
                  ("handoff", "handoffs")):
        h.append("<tr><th>%s</th><td>%d</td></tr>" % (k, counts.get(ck, 0)))
    for k in ("evidence", "extract_job", "extracted_chunk", "share_link", "audit_log"):
        n = con.execute("SELECT COUNT(*) FROM %s" % k).fetchone()[0]
        h.append("<tr><th>%s</th><td>%d</td></tr>" % (k, n))
    fts_n = con.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
    h.append("<tr><th>search_fts</th><td>%d</td></tr>" % fts_n)
    h.append("</table></div></div></div>")
    h.append('<div class="card"><h3>Extraction queue (%d)</h3>' % len(jobs))
    if jobs:
        h.append('<table><tr><th>Job</th><th>Artifact</th><th>Status</th>'
                 '<th>Attempts</th><th>Last error</th><th></th></tr>')
        for j in jobs:
            h.append("<tr><td class='mono small'>%s</td><td class='small'>%s</td>"
                     "<td><span class='badge'>%s</span></td><td>%d</td>"
                     "<td class='small muted'>%s</td><td>"
                     % (E(j["id"]), E(j["filename"] or j["artifact_id"]),
                        E(j["status"]), j["attempts"], E((j["last_error"] or "")[:80])))
            if j["status"] in ("failed", "unavailable"):
                h.append('<form method="post" action="%s" enctype="multipart/form-data" '
                         'class="inline"><input type="hidden" name="action" value="retry_job">'
                         '<input type="hidden" name="job_id" value="%s">'
                         '<button type="submit">Retry</button></form>'
                         % (_href(req, "/s/settings"), E(j["id"])))
            h.append("</td></tr>")
        h.append("</table>")
        if fails:
            h.append('<p class="small"><span class="badge warn">%d failed</span> '
                     '<span class="muted">Failures never corrupt the source artifact; '
                     "retry is safe.</span></p>" % fails)
    else:
        h.append('<div class="empty">Queue empty.</div>')
    h.append("</div>")
    # integrity check (read-only; runs on demand)
    h.append('<div class="card"><h3>Integrity check</h3>')
    if req.method == "POST":
        text, _ = _fields(req)
        if text.get("action") == "run_check":
            from . import check as _check
            try:
                rep = _check.check(data_dir, hash_sample=100)
                h.append('<p><span class="badge %s">%s</span></p>' %
                         ("ok" if rep["ok"] else "err", E(rep["summary"])))
                h.append("<pre class='small'>%s</pre>" %
                         E(_check.format_report(rep)))
            except Exception as e:
                h.append('<p class="badge err">check failed: %s</p>' % E(str(e)[:200]))
    h.append('<p class="muted small">Read-only: verifies blob files, hashes, '
             'references, and FTS consistency. Never deletes or repairs.</p>'
             '<form method="post" action="%s" enctype="multipart/form-data" class="inline">'
             '<input type="hidden" name="action" value="run_check">'
             '<button type="submit">Run integrity check</button></form></div>'
             % _href(req, "/s/settings"))
    h.append('<div class="card"><h3>Recent audit (50)</h3>')
    if audit:
        h.append("<table><tr><th>Time</th><th>Actor</th><th>Action</th>"
                 "<th>Scope</th></tr>")
        for a in audit:
            h.append("<tr><td class='small'>%s</td><td class='small'>%s</td>"
                     "<td class='small'><code>%s</code></td>"
                     "<td class='small mono'>%s %s</td></tr>"
                     % (_fmt_ts(a["ts"], "exact"), E(a["actor"]), E(a["action"]),
                        E(a["scope_type"]), E(a["scope_id"])))
        h.append("</table>")
    else:
        h.append('<div class="empty">No audit entries.</div>')
    h.append("</div>")
    h.append('<div class="card"><h3>Link model — what is public, what is private</h3>'
             '<p class="small">Stated plainly, no weasel words:</p><ul class="tight small">'
             "<li><b>Owner token</b> (query <code>?token=</code>, "
             "<code>Authorization: Bearer</code>, or cookie): full access to everything, "
             "including all <code>private</code> objects, thumbnails, extracted text, "
             "and search snippets. A single shared secret — anyone holding it is the owner.</li>"
             "<li><b><code>private</code> artifacts</b>: require the owner token on every "
             "route, including <code>/content</code>, thumbnails, extracted text, and "
             "search results. Never exposed without the token.</li>"
             "<li><b><code>public-link</code> artifacts</b> (legacy <code>/f/&lt;id&gt;</code> "
             "imports): readable by anyone who holds the unguessable URL. This is "
             "<b>not</b> access control — it is obscurity. Treat these as public.</li>"
             "<li><b>Share links</b> (<code>/s/share/&lt;token&gt;</code>): scoped to exactly "
             "one collection or one artifact, optionally expiring, revocable at any time, "
             "and every access is written to the audit log. A share link grants access to "
             "its scope <b>only</b> — no traversal to other objects, no metadata leakage.</li>"
             "<li>Unguessable identifiers are <b>not</b> a privacy mechanism. "
             "If an object must be private, keep its visibility <code>private</code> "
             "and share it only through revocable share links.</li>"
             "</ul></div>")
    return _shell(req, "Settings", "".join(h))


# ---------------------------------------------------------------- share (public-by-token)

def _page_share(req, con, data_dir, token, rest):
    link = model.resolve_share_token(con, token)
    if not link:
        return _notfound("Share link invalid, expired, or revoked.")
    model.audit(con, "share", "share_access", link["scope_type"], link["scope_id"],
                {"link_id": link["id"]})
    parts = [p for p in rest.split("/") if p]
    if link["scope_type"] == "collection":
        c = model.get_collection(con, link["scope_id"])
        if not c:
            return _notfound("Shared collection no longer exists.")
        if parts and parts[0] == "art" and len(parts) > 1:
            return _share_artifact_view(req, con, data_dir, token, c, parts[1])
        if parts and parts[0] == "content" and len(parts) > 1:
            return _share_content(con, data_dir, c, parts[1])
        return _share_collection_view(req, con, token, c)
    if link["scope_type"] == "artifact":
        a = store.get_artifact(con, link["scope_id"])
        if not a:
            return _notfound("Shared artifact no longer exists.")
        if parts and parts[0] == "content":
            return _share_content(con, data_dir, None, a["id"])
        return _share_artifact_view(req, con, data_dir, token, None, a["id"])
    return _notfound("Unknown share scope.")


def _share_member_ids(con, collection):
    ids = set()
    for m in model.collection_members(con, collection["id"]):
        if m["artifact_id"]:
            ids.add(m["artifact_id"])
    return ids


def _share_shell(title, body):
    return _app.html_resp(
        200,
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s</style></head>"
        "<body><div class='wrap'>%s</div></body></html>" % (E(title), CSS, body))


def _share_collection_view(req, con, token, c):
    members = model.collection_members(con, c["id"])
    h = ["<h1>%s</h1>" % E(c["title"]),
         '<p class="muted">%s</p>' % E(c["description"] or ""),
         '<p class="small muted">Shared collection · access is logged</p>',
         "<h2>Entries (%d)</h2>" % len(members)]
    if members:
        h.append('<table><tr><th>#</th><th>Entry</th><th>Note</th></tr>')
        for i, m in enumerate(members, 1):
            if m["artifact_id"]:
                a = store.get_artifact(con, m["artifact_id"])
                label = ('<a href="/s/share/%s/art/%s">%s</a>'
                         % (E(token), E(m["artifact_id"]),
                            E(a["filename"] if a else m["artifact_id"])))
            else:
                label = '<span class="muted">nested collection (not shared)</span>'
            h.append("<tr><td>%d</td><td>%s</td><td class='small muted'>%s</td></tr>"
                     % (i, label, E(m["note"] or "")))
        h.append("</table>")
    else:
        h.append('<div class="empty">This collection is empty.</div>')
    return _share_shell("Shared collection — " + c["title"], "".join(h))


def _share_artifact_view(req, con, data_dir, token, collection, aid):
    # scope enforcement: artifact scope -> only that artifact; collection scope ->
    # only members of the collection. Never leak other objects: no edges shown.
    if collection is not None:
        if aid not in _share_member_ids(con, collection):
            return _notfound("Not in this shared collection.")
    a = store.get_artifact(con, aid)
    if not a:
        return _notfound("Artifact not found.")
    h = ["<h1>%s</h1>" % E(a["filename"]),
         '<p class="small muted">Shared artifact · %s · %s · access is logged</p>'
         % (E(a["mime"]), _size(a["size"])),
         '<p><a class="btn" href="/s/share/%s/content/%s">Download / open</a></p>'
         % (E(token), E(aid))]
    if a["mime"].startswith("image/"):
        h.append('<p><img src="/s/share/%s/content/%s" '
                 'style="max-width:100%%;border:1px solid #2a2f3a;border-radius:8px"></p>'
                 % (E(token), E(aid)))
    chunks = [dict(r) for r in con.execute(
        "SELECT * FROM extracted_chunk WHERE artifact_id=? ORDER BY chunk_no LIMIT 40",
        (aid,))]
    if chunks:
        h.append("<h2>Extracted text</h2>")
        for c in chunks:
            h.append("<pre>%s</pre>" % E(c["text"]))
    h.append('<p class="small muted">Provenance: source <code>%s</code> · sha256 '
             '<code class="mono">%s</code></p>'
             % (E(a["source"] or ""), E(a["blob_sha256"][:32] + "…")))
    if collection is not None:
        h.append('<p><a href="/s/share/%s">← back to collection</a></p>' % E(token))
    return _share_shell("Shared — " + a["filename"], "".join(h))


def _share_content(con, data_dir, collection, aid):
    if collection is not None and aid not in _share_member_ids(con, collection):
        return _notfound("Not in this shared collection.")
    a = store.get_artifact(con, aid)
    if not a:
        return _notfound("Artifact not found.")
    try:
        data = store.read_artifact_bytes(con, data_dir, a)
    except FileNotFoundError:
        return _notfound("Bytes unavailable.")
    return 200, a["mime"] or "application/octet-stream", data


# ---------------------------------------------------------------- router

def handle(con, data_dir, req):
    """Route /s/* requests. Returns (status, content_type, body) or None."""
    path = req.path or "/s/"
    if not path.startswith("/s/") and path != "/s":
        return None
    if path == "/s":
        path = "/s/"

    # public-by-token share routes (no owner auth required)
    if path == "/s/share/" or path.startswith("/s/share/"):
        token = path[len("/s/share/"):].split("/")[0]
        rest = path[len("/s/share/") + len(token):]
        if req.method != "GET":
            return _app.err(405, "method_not_allowed", "share links are read-only")
        return _page_share(req, con, data_dir, token, rest)

    # everything else requires the owner token
    if not req.authed:
        return _deny()

    sub = path[len("/s/"):]
    seg = [p for p in sub.split("/") if p]

    # Establish a cookie session from a valid ?token=, so subsequent
    # navigation doesn't need the token in the URL. The cookie is
    # HttpOnly + SameSite=Lax (+ Secure when served over HTTPS).
    if seg and seg[0] == "session" and req.method == "POST":
        action = req.q("action", "")
        if action == "establish" and req.token_via_query and req.token:
            c = SimpleCookie()
            c["substrate_token"] = req.token
            c["substrate_token"]["httponly"] = True
            c["substrate_token"]["samesite"] = "Lax"
            c["substrate_token"]["path"] = "/"
            # Secure flag is set by the deployment when serving HTTPS;
            # the app cannot reliably detect TLS through the mount contract.
            hdr = c.output(header="").strip()
            _status, _ctype, body = _shell(req, "Session",
                           "<p>Cookie session established. You can now browse "
                           "without <code>?token=</code> in the URL.</p>"
                           '<p><a href="/s/">Continue to Substrate</a></p>')
            return (200, "text/html; charset=utf-8", body,
                    {"Set-Cookie": hdr.split(": ", 1)[1] if ": " in hdr else hdr})
        return _notfound()

    if req.method == "GET" and not seg:
        return _page_home(req, con)
    if seg and seg[0] == "workspaces":
        if len(seg) == 1:
            return _page_workspaces(req, con)
        if len(seg) == 2:
            return _page_workspace_detail(req, con, seg[1])
        return _notfound()
    if seg and seg[0] == "sessions":
        if len(seg) == 1:
            return _page_sessions(req, con)
        if len(seg) == 2:
            return _page_session_detail(req, con, seg[1])
        return _notfound()
    if seg and seg[0] == "timeline" and len(seg) == 1:
        return _page_timeline(req, con)
    if seg and seg[0] == "artifacts":
        if len(seg) == 1:
            return _page_artifacts(req, con)
        if len(seg) == 2:
            return _page_artifact_detail(req, con, seg[1])
        if len(seg) == 3 and seg[2] == "content" and req.method == "GET":
            try:
                a, data = _artifact_content(con, data_dir, seg[1])
            except FileNotFoundError:
                return _notfound("Bytes unavailable")
            if a is None:
                return _notfound("Artifact not found")
            return 200, a["mime"] or "application/octet-stream", data
        return _notfound()
    if seg and seg[0] == "gallery" and len(seg) == 1:
        return _page_gallery(req, con)
    if seg and seg[0] == "collections":
        if len(seg) == 1:
            return _page_collections(req, con)
        if len(seg) == 2:
            return _page_collection_detail(req, con, seg[1])
        if len(seg) == 3 and seg[2] == "manifest" and req.method == "GET":
            c = model.get_collection(con, seg[1])
            if not c:
                return _notfound("Collection not found")
            body = _collection_manifest(con, seg[1]).encode("utf-8")
            return 200, "application/json; charset=utf-8", body
        return _notfound()
    if seg and seg[0] == "graph" and len(seg) == 1:
        return _page_graph(req, con)
    if seg and seg[0] == "search" and len(seg) == 1:
        return _page_search(req, con)
    if seg and seg[0] == "context" and len(seg) == 1:
        return _page_context(req, con)
    if seg and seg[0] == "checkpoints":
        if len(seg) == 1:
            return _page_checkpoints(req, con)
        if len(seg) == 2:
            return _page_checkpoint_detail(req, con, seg[1])
        return _notfound()
    if seg and seg[0] == "handoffs":
        if len(seg) == 1:
            return _page_handoffs(req, con)
        if len(seg) == 2:
            return _page_handoff_detail(req, con, seg[1])
        return _notfound()
    if seg and seg[0] == "settings" and len(seg) == 1:
        return _page_settings(req, con)
    if seg and seg[0] == "decisions" and len(seg) == 2:
        return _page_decision_detail(req, con, seg[1])
    if seg and seg[0] == "tasks" and len(seg) == 2:
        return _page_task_detail(req, con, seg[1])
    if seg and seg[0] == "claims" and len(seg) == 2:
        return _page_claim_detail(req, con, seg[1])
    if seg and seg[0] == "events" and len(seg) == 2:
        return _page_event_detail(req, con, seg[1])
    if seg and seg[0] == "capture" and len(seg) == 1:
        return _page_capture(req, con)
    return _notfound("Unknown UI route: %s" % path)


def _page_capture(req, con):
    """GIS Browser Sidecar: user-mediated handoff capture from ChatGPT."""
    workspaces = con.execute(
        "SELECT id, title FROM workspace ORDER BY title").fetchall()
    ws_opts = "".join(
        '<option value="%s">%s</option>' % (E(r[0]), E(r[1] or r[0]))
        for r in workspaces)
    body = """
<h1>GIS Capture — ChatGPT Handoff</h1>
<p class="muted">Paste content from ChatGPT, structure it, preview the exact
payload, then approve to save. Nothing is written until you click
<strong>Approve &amp; Save</strong>.</p>
<form id="cap" onsubmit="return false">
<label>Workspace
<select id="f_ws">%s</select></label>
<label>Objective / summary
<input id="f_obj" placeholder="One-line objective"></label>
<label>Session summary
<textarea id="f_summary" rows="3" placeholder="What was accomplished"></textarea></label>
<label>Completed task IDs (comma-separated)
<input id="f_done" placeholder="tsk_..."></label>
<label>Pending task IDs (comma-separated)
<input id="f_pend" placeholder="tsk_..."></label>
<label>Evidence (test output, excerpts, IDs)
<textarea id="f_evidence" rows="3"></textarea></label>
<label>Decisions
<textarea id="f_decisions" rows="2"></textarea></label>
<label>Uncertainties
<textarea id="f_uncertain" rows="2"></textarea></label>
<label>Next action
<input id="f_next" placeholder="Concrete next step"></label>
<label>Pasted ChatGPT content
<textarea id="f_paste" rows="6" placeholder="Paste conversation excerpt here"></textarea></label>
<p>
<button type="button" onclick="doPreview()">Preview payload</button>
<button type="button" id="btn_save" onclick="doSave()" disabled>Approve &amp; Save</button>
</p>
</form>
<h2>Payload preview</h2>
<pre id="preview" class="muted">Click "Preview payload" to see the exact JSON.</pre>
<div id="result"></div>
<script>
let lastPayload = null;
let idemKey = null;
function val(id){ return document.getElementById(id).value.trim(); }
function csv(id){ return val(id).split(",").map(s=>s.trim()).filter(Boolean); }
function doPreview(){
    idemKey = "cap-" + Date.now() + "-" + Math.random().toString(36).slice(2,10);
    lastPayload = {
        workspace_id: val("f_ws"),
        objective: val("f_obj"),
        expected: {
            session_summary: val("f_summary"),
            completed: csv("f_done"),
            pending: csv("f_pend"),
            next_action: val("f_next"),
        },
        scope: {
            evidence: val("f_evidence"),
            decisions: val("f_decisions"),
            uncertainties: val("f_uncertain"),
        },
        payload_text: val("f_paste"),
        meta: { source: "gis-capture-page", idempotency_key: idemKey },
    };
    document.getElementById("preview").textContent =
        JSON.stringify(lastPayload, null, 2);
    document.getElementById("btn_save").disabled = !lastPayload.objective;
    document.getElementById("result").innerHTML = "";
}
function authHeaders(){
    const h = {"Content-Type": "application/json", "Idempotency-Key": idemKey};
    const m = new URLSearchParams(window.location.search).get("token");
    if(m) h["Authorization"] = "Bearer " + m;
    return h;
}
async function doSave(){
    if(!lastPayload || !lastPayload.objective){
        alert("Preview first and fill in the objective."); return;
    }
    const btn = document.getElementById("btn_save");
    btn.disabled = true; btn.textContent = "Saving…";
    try{
        const r = await fetch("/api/v1/handoffs", {
            method: "POST",
            headers: authHeaders(),
            credentials: "same-origin",
            body: JSON.stringify(lastPayload),
        });
        const d = await r.json();
        if(r.ok && d.handoff){
            document.getElementById("result").innerHTML =
                '<p class="ok">Saved: <a href="/s/handoffs/' + d.handoff.id +
                '">' + d.handoff.id + "</a></p>";
        } else {
            document.getElementById("result").innerHTML =
                '<p class="err">Failed: ' + (d.error || r.status) + "</p>";
            btn.disabled = false; btn.textContent = "Approve & Save";
        }
    }catch(e){
        document.getElementById("result").innerHTML =
            '<p class="err">Error: ' + e.message + "</p>";
        btn.disabled = false; btn.textContent = "Approve & Save";
    }
}
</script>
""" % ws_opts
    return _shell(req, "GIS Capture", body)
