"""End-to-end smoke test for substrate.api via SubstrateApp.handle."""
import json, os, sys, tempfile

sys.path.insert(0, "/home/hatch/workspace/substrate")
from substrate.app import SubstrateApp

tmp = tempfile.mkdtemp(prefix="substrate-api-test-")
token_path = os.path.join(tmp, "token")
with open(token_path, "w") as f:
    f.write("test-secret-token")
data_dir = os.path.join(tmp, "data")

app = SubstrateApp(data_dir, token_path)
H_AUTH = {"authorization": "Bearer test-secret-token"}
H_JSON = dict(H_AUTH, **{"content-type": "application/json"})

passed, failed = 0, 0
def check(name, cond, extra=""):
    global passed, failed
    if cond: passed += 1; print("ok  -", name)
    else: failed += 1; print("FAIL-", name, extra)

def call(method, path, headers=None, body=b""):
    res = app.handle(method, path, headers or {}, body)
    st, ct, b = res[0], res[1], res[2]
    try: j = json.loads(b.decode())
    except Exception: j = None
    return st, ct, b, j

def post(path, obj, headers=None, idem=None):
    h = dict(H_JSON); h.update(headers or {})
    if idem: h["idempotency-key"] = idem
    return call("POST", path, h, json.dumps(obj).encode())

def get(path, headers=None):
    return call("GET", path, headers or H_AUTH)

# 1. auth gate
st, _, _, j = call("GET", "/api/v1/health", {})
check("unauth -> 403", st == 403 and j["error"]["code"] == "forbidden", st)

# 2. health
st, _, _, j = get("/api/v1/health")
check("health", st == 200 and j["ok"] and j["schema_version"] == 1, j)

# 3. workspace create + idempotency
st, _, _, j = post("/api/v1/workspaces", {"title": "WS1", "meta": {"a": 1}}, idem="k1")
check("workspace create 201", st == 201 and j["workspace"]["title"] == "WS1", st)
ws = j["workspace"]["id"]
st, _, _, j2 = post("/api/v1/workspaces", {"title": "WS1-CHANGED"}, idem="k1")
check("idempotent replay", st == 200 and j2["workspace"]["id"] == ws and j2.get("idempotent_replay"), (st, j2))
st, _, _, j = post("/api/v1/workspaces", {})
check("workspace missing title 400", st == 400, st)
st, _, _, j = get("/api/v1/workspaces/" + ws)
check("workspace get", st == 200 and j["workspace"]["id"] == ws, st)
st, _, _, j = get("/api/v1/workspaces/ws_nope")
check("workspace 404", st == 404, st)

# 4. sessions + events
st, _, _, j = post("/api/v1/sessions", {"title": "S1", "workspace_id": ws, "time_quality": "exact"})
check("session create", st == 201, st)
ses = j["session"]["id"]
st, _, _, j = post("/api/v1/sessions", {"title": "bad", "time_quality": "nope"})
check("bad time_quality 400", st == 400, st)
st, _, _, j = post("/api/v1/sessions/%s/events" % ses, {"text": "hello", "actor": "abed", "kind": "message"})
check("event append", st == 201 and j["event"]["seq"] == 0, st)
ev = j["event"]["id"]
st, _, _, j = post("/api/v1/sessions/%s/events" % ses, {"text": "x", "kind": "bogus"})
check("bad kind 400", st == 400, st)
st, _, _, j = get("/api/v1/sessions/%s/events?kinds=message" % ses)
check("event list", st == 200 and len(j["events"]) == 1, j)
st, _, _, j = get("/api/v1/sessions?workspace_id=" + ws)
check("session list", st == 200 and len(j["sessions"]) == 1, j)

# 5. artifact upload (multipart) + content + revisions
boundary = "BOUND123"
def mp(files, fields=None):
    parts = []
    for name, fn, ct, data in files:
        parts.append(("--" + boundary + "\r\n").encode() +
            ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, fn)).encode() +
            ("Content-Type: %s\r\n\r\n" % ct).encode() + data + b"\r\n")
    for name, val in (fields or []):
        parts.append(("--" + boundary + "\r\n").encode() +
            ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode() +
            val.encode() + b"\r\n")
    return b"".join(parts) + ("--" + boundary + "--\r\n").encode()

png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
body = mp([("file", "shot.png", "image/png", png)])
st, _, _, j = call("POST", "/api/v1/artifacts?visibility=private&event_id=" + ev,
                   dict(H_AUTH, **{"content-type": "multipart/form-data; boundary=" + boundary}), body)
check("artifact upload", st == 201 and len(j["artifacts"]) == 1, (st, j))
art = j["artifacts"][0]["id"]
check("no source_ref leaked", "source_ref" not in j["artifacts"][0], j["artifacts"][0].keys())
st, ct, b, _ = call("GET", "/api/v1/artifacts/" + art + "/content", H_AUTH)
check("content bytes", st == 200 and b == png and ct.startswith("image/png"), (st, ct, len(b)))
st, _, _, j = get("/api/v1/artifacts/" + art + "/edges?direction=both")
check("artifact edges (event introduces)", st == 200 and any(e["rel"] == "introduces" for e in j["in"]), j)
st, _, _, j = get("/api/v1/artifacts?mime_prefix=image/&filename=shot")
check("artifact list filter", st == 200 and len(j["artifacts"]) == 1, j)
st, _, _, j = get("/api/v1/artifacts?visibility=bogus")
check("bad visibility 400", st == 400, st)
# revision
body = mp([("file", "shot.png", "image/png", png + b"v2")], [("change_note", "second")])
st, _, _, j = call("POST", "/api/v1/artifacts/" + art + "/revisions",
                   dict(H_AUTH, **{"content-type": "multipart/form-data; boundary=" + boundary}), body)
check("revision add", st == 201 and j["rev_no"] == 2, (st, j))
st, _, _, j = get("/api/v1/artifacts/" + art + "/revisions")
check("revisions list", st == 200 and len(j["revisions"]) == 2, j)

# 6. edges + traverse
st, _, _, j = post("/api/v1/edges", {"src_type": "event", "src_id": ev, "dst_type": "artifact",
                                     "dst_id": art, "rel": "documents", "note": "t"})
check("edge create", st == 201, st)
st, _, _, j = post("/api/v1/edges", {"src_type": "event", "src_id": ev, "dst_type": "artifact",
                                     "dst_id": art, "rel": "bogus"})
check("bad rel 400", st == 400, st)
st, _, _, j = get("/api/v1/traverse?seed=event:%s&max_depth=2" % ev)
check("traverse", st == 200 and len(j["nodes"]) >= 2 and len(j["edges"]) >= 2, j)
st, _, _, j = get("/api/v1/traverse?seed=event:%s&max_depth=9" % ev)
check("traverse depth bound 400", st == 400, st)
st, _, _, j = get("/api/v1/traverse?seed=nope:x")
check("traverse bad seed 400", st == 400, st)

# 7. decisions / tasks / claims / evidence
st, _, _, j = post("/api/v1/decisions", {"title": "D1", "workspace_id": ws, "session_id": ses, "status": "active"})
check("decision create", st == 201, st)
dec = j["decision"]["id"]
st, _, _, j = post("/api/v1/decisions", {"title": "D2", "supersedes_id": dec})
check("decision supersede", st == 201, st)
st, _, _, j = get("/api/v1/decisions/" + dec)
check("old decision superseded", st == 200 and j["decision"]["status"] == "superseded", j["decision"]["status"])
st, _, _, j = post("/api/v1/tasks", {"title": "T1", "kind": "question", "workspace_id": ws})
check("task(question) create", st == 201, st)
tsk = j["task"]["id"]
st, _, _, j = call("PATCH", "/api/v1/tasks/" + tsk, H_JSON, json.dumps({"status": "answered"}).encode())
check("question status patch", st == 200 and j["task"]["status"] == "answered", (st, j))
st, _, _, j = call("PATCH", "/api/v1/tasks/" + tsk, H_JSON, json.dumps({"status": "done"}).encode())
check("wrong status enum 400", st == 400, st)
st, _, _, j = post("/api/v1/claims", {"title": "C1", "workspace_id": ws, "verification_status": "unverified"})
check("claim create", st == 201, st)
clm = j["claim"]["id"]
st, _, _, j = post("/api/v1/claims/%s/evidence" % clm, {"stance": "supports", "artifact_id": art, "locator": "p.1"})
check("evidence add", st == 201, st)
st, _, _, j = post("/api/v1/claims/%s/evidence" % clm, {"stance": "bogus", "artifact_id": art})
check("bad stance 400", st == 400, st)
st, _, _, j = get("/api/v1/claims/" + clm)
check("claim with evidence", st == 200 and len(j["claim"]["evidence"]) == 1, j)

# 8. collections
st, _, _, j = post("/api/v1/collections", {"title": "COL1"})
check("collection create", st == 201, st)
col = j["collection"]["id"]
st, _, _, j = post("/api/v1/collections/%s/members" % col, {"artifact_id": art, "note": "m1"})
check("member add", st == 201, st)
st, _, _, j = post("/api/v1/collections/%s/members" % col, {"artifact_id": art, "child_collection_id": col})
check("member xor 400", st == 400, st)
st, _, _, j = post("/api/v1/collections/%s/members" % col, {"child_collection_id": col})
check("self-nest 400", st == 400, st)
st, _, _, j = get("/api/v1/collections/" + col)
check("collection get w/ members", st == 200 and j["member_count"] == 1 and j["members"][0]["artifact"]["id"] == art, j)
st, _, _, j = get("/api/v1/collections/" + col + "/manifest")
check("manifest", st == 200 and j["members"][0]["artifact"]["blob_sha256"], j)

# 9. checkpoints
st, _, _, j = post("/api/v1/checkpoints", {"title": "CHK1", "body": {"sessions": [ses]}, "workspace_id": ws})
check("checkpoint create", st == 201 and j["checkpoint"]["body"] == {"sessions": [ses]}, st)
chk = j["checkpoint"]["id"]
st, _, _, j = get("/api/v1/checkpoints/" + chk)
check("checkpoint get", st == 200, st)

# 10. handoffs: full status pipeline + payload hash
st, _, _, j = post("/api/v1/handoffs", {"objective": "do the thing", "payload_text": "# payload\nverbatim",
                                        "workspace_id": ws, "from_session_id": ses, "to_env": "claude-code"})
check("handoff create", st == 201 and j["handoff"]["status"] == "prepared", st)
ho = j["handoff"]["id"]
st, _, _, j = get("/api/v1/handoffs/%s/payload" % ho)
check("payload verbatim+hash", st == 200 and j["text"] == "# payload\nverbatim" and j["hash_match"], j)
for nxt in ["ready", "authorized", "sent", "acknowledged", "executing", "completed"]:
    st, _, _, j = post("/api/v1/handoffs/%s/status" % ho, {"status": nxt})
    assert st == 200, (nxt, st, j)
check("handoff full pipeline", j["handoff"]["status"] == "completed", j)
st, _, _, j = post("/api/v1/handoffs/%s/status" % ho, {"status": "prepared"})
check("illegal transition 400", st == 400, st)
st, _, _, j = post("/api/v1/handoffs/%s/status" % ho, {"status": "bogus"})
check("bad status 400", st == 400, st)

# 11. share create (token once) + revoke
st, _, _, j = post("/api/v1/share", {"scope_type": "collection", "scope_id": col, "note": "t"})
check("share create", st == 201 and j["share_url"].startswith("/s/share/") and j["token"], st)
lnk = j["id"]
tok = j["token"]
st, _, _, j = post("/api/v1/share/%s/revoke" % lnk, {})
check("share revoke", st == 200 and j["revoked"], st)
st, _, _, j = post("/api/v1/share", {"scope_type": "bogus", "scope_id": col})
check("bad scope 400", st == 400, st)

# 12. search + context (modules present)
st, _, _, j = get("/api/v1/search?q=hello")
check("search 200", st == 200 and j["query"] == "hello" and isinstance(j["results"], dict), (st, j))
st, _, _, j = get("/api/v1/search")
check("search missing q 400", st == 400, st)
st, _, _, j = post("/api/v1/context", {"question": "what was decided?", "seed_ids": ["decision:" + dec], "budget_tokens": 1000})
check("context 200", st == 200 and "package" in j, (st, str(j)[:200]))
st, _, _, j = post("/api/v1/context", {"question": "x", "budget_tokens": 10})
check("context bad budget 400", st == 400, st)

# 13. export (workspace-scoped + full)
st, _, _, j = get("/api/v1/export?workspace_id=" + ws)
check("export ws", st == 200 and j["format"] == "substrate-export" and j["counts"]["events"] == 1, j.get("counts"))
st, ct, b, j = get("/api/v1/export")
check("export full", st == 200 and j["counts"]["artifacts"] == 2 and j["counts"]["revisions"] == 3, j.get("counts"))
check("export has blob_sha256 no bytes", all("blob_sha256" in a and "bytes" not in a for a in j["artifacts"]), "")

# 14. extract jobs + retry
st, _, _, j = get("/api/v1/extract/jobs?status=pending")
check("extract jobs", st == 200 and len(j["jobs"]) >= 1, j)
st, _, _, j = get("/api/v1/extract/jobs?status=bogus")
check("bad job status 400", st == 400, st)
st, _, _, j = post("/api/v1/artifacts/%s/extract/retry" % art, {})
check("extract retry", st == 200 and j["job"]["status"] == "pending", st)

# 15. audit
st, _, _, j = get("/api/v1/audit?limit=5&action=artifact.upload")
check("audit tail", st == 200 and len(j["audit"]) == 1 and j["audit"][0]["actor"] == "owner", j)
st, _, _, j = get("/api/v1/audit?limit=3")
check("audit list", st == 200 and len(j["audit"]) == 3, j)

# 16. no token in audit for share
import sqlite3
con = sqlite3.connect(os.path.join(data_dir, "substrate.db"))
rows = con.execute("SELECT detail_json FROM audit_log WHERE action='share.create'").fetchall()
check("token never in audit", all(tok not in r[0] for r in rows), "")

# 17. unknown route
st, _, _, j = get("/api/v1/nope")
check("unknown route 404", st == 404, st)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
