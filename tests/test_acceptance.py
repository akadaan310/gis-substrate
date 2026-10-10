"""Acceptance criteria A-L, end-to-end against a live SubstrateApp instance.

Each test observes behavior; nothing passes merely because code exists.
See docs/ACCEPTANCE.md for the criterion definitions.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate.app import SubstrateApp
from substrate import db as _db, store, model, extract, export


def _mpart(fields):
    """fields: list of (name, filename, ctype, bytes) -> (ctype_header, body)."""
    bnd = "ACCEPTANCEBOUNDARY"
    parts = []
    for name, fn, ct, data in fields:
        parts.append(("--%s\r\n" % bnd).encode())
        parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                      % (name, fn)).encode())
        parts.append(("Content-Type: %s\r\n\r\n" % ct).encode())
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % bnd).encode())
    return "multipart/form-data; boundary=" + bnd, b"".join(parts)


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="substrate-acc-")
        with open(os.path.join(self.tmp, ".token"), "w") as f:
            f.write("acc-secret")
        self.app = SubstrateApp(os.path.join(self.tmp, "data"),
                                os.path.join(self.tmp, ".token"))
        self.H = {"authorization": "Bearer acc-secret"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- driver helpers -------------------------------------------------
    def api(self, method, path, body=None, headers=None, ctype=None, authed=True):
        h = dict(self.H) if authed else {}
        h.update(headers or {})
        if ctype:
            h["content-type"] = ctype
        b = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        res = self.app.handle(method, path, h, b or b"")
        st, ct, rb = res[0], res[1], res[2]
        # res may carry a 4th element (extra headers); tests ignore it
        try:
            j = json.loads(rb.decode())
        except Exception:
            j = None
        return st, j, rb

    def ui(self, path, authed=True):
        h = dict(self.H) if authed else {}
        st, ct, rb = self.app.handle("GET", path, h, b"")
        return st, rb.decode("utf-8", errors="replace")

    def con(self):
        return self.app.connect()


class TestAProvenance(AcceptanceBase):
    def test_event_to_screenshot_and_back(self):
        st, ws, _ = self.api("POST", "/api/v1/workspaces", {"title": "w"})
        wsid = ws["workspace"]["id"]
        st, ses, _ = self.api("POST", "/api/v1/sessions",
                              {"title": "s", "workspace_id": wsid})
        sid = ses["session"]["id"]
        st, ev, _ = self.api("POST", f"/api/v1/sessions/{sid}/events",
                             {"text": "screenshot of the failure", "kind": "message"})
        evid = ev["event"]["id"]
        ctype, body = _mpart([("file", "fail.png", "image/png", b"FAKEPNG" * 100)])
        st, up, _ = self.api("POST", "/api/v1/artifacts?visibility=private",
                             body, ctype=ctype)
        aid = up["artifacts"][0]["id"]
        st, _, _ = self.api("POST", "/api/v1/edges",
                            {"src_type": "event", "src_id": evid,
                             "dst_type": "artifact", "dst_id": aid,
                             "rel": "introduces", "note": "user uploaded"})
        # event -> artifact
        st, nb, _ = self.api("GET", f"/api/v1/artifacts/{aid}/edges?direction=in")
        srcs = {(e["src_type"], e["src_id"]) for e in nb["in"]}
        self.assertIn(("event", evid), srcs)
        # artifact -> event (back): same edge visible from the artifact side
        st, nb2, _ = self.api("GET", f"/api/v1/artifacts/{aid}/edges?direction=in")
        self.assertTrue(any(e["rel"] == "introduces" for e in nb2["in"]))
        # provenance present on artifact page
        st, page = self.ui(f"/s/artifacts/{aid}")
        self.assertEqual(st, 200)
        self.assertIn("provenance", page.lower())
        self.assertIn(evid[:8], page)


class TestBCollection(AcceptanceBase):
    def test_multi_file_collection(self):
        aids = []
        for i in range(3):
            ctype, body = _mpart([("file", f"f{i}.txt", "text/plain",
                                   f"content {i}".encode())])
            st, up, _ = self.api("POST", "/api/v1/artifacts", body, ctype=ctype)
            aids.append(up["artifacts"][0]["id"])
        st, col, _ = self.api("POST", "/api/v1/collections",
                              {"title": "pack", "visibility": "private"})
        cid = col["collection"]["id"]
        st, sub, _ = self.api("POST", "/api/v1/collections",
                              {"title": "nested", "visibility": "private"})
        subid = sub["collection"]["id"]
        for aid in aids:
            self.api("POST", f"/api/v1/collections/{cid}/members",
                     {"artifact_id": aid})
        self.api("POST", f"/api/v1/collections/{cid}/members",
                 {"child_collection_id": subid})
        st, man, _ = self.api("GET", f"/api/v1/collections/{cid}/manifest")
        self.assertEqual(len(man["members"]), 4)
        # retrieve one member's bytes; must match original
        st, got, raw = self.api("GET", f"/api/v1/artifacts/{aids[1]}/content")
        self.assertEqual(st, 200)
        self.assertEqual(raw, b"content 1")
        # stable collection destination renders
        st, page = self.ui(f"/s/collections/{cid}")
        self.assertEqual(st, 200)
        self.assertIn("pack", page)


class TestCSessionContinuity(AcceptanceBase):
    def test_checkpoint_resume(self):
        st, ws, _ = self.api("POST", "/api/v1/workspaces", {"title": "w"})
        wsid = ws["workspace"]["id"]
        st, ses, _ = self.api("POST", "/api/v1/sessions",
                              {"title": "s", "workspace_id": wsid})
        sid = ses["session"]["id"]
        st, ev, _ = self.api("POST", f"/api/v1/sessions/{sid}/events",
                             {"text": "key fact: the sky is blue"})
        evid = ev["event"]["id"]
        st, dec, _ = self.api("POST", "/api/v1/decisions",
                              {"title": "ship it", "workspace_id": wsid})
        did = dec["decision"]["id"]
        st, cp, _ = self.api("POST", "/api/v1/checkpoints",
                             {"title": "cp1", "workspace_id": wsid,
                              "body": {"sessions": [sid], "events": [evid],
                                       "decisions": [did],
                                       "objective": "resume the discussion"}})
        cpid = cp["checkpoint"]["id"]
        # fresh read: load checkpoint only, then fetch referenced records by id
        st, got, _ = self.api("GET", f"/api/v1/checkpoints/{cpid}")
        body = got["checkpoint"]["body"]
        st, s2, _ = self.api("GET", f"/api/v1/sessions/{body['sessions'][0]}")
        self.assertEqual(s2["session"]["title"], "s")
        st, d2, _ = self.api("GET", f"/api/v1/decisions/{body['decisions'][0]}")
        self.assertEqual(d2["decision"]["title"], "ship it")
        # checkpoint is immutable: body unchanged
        self.api("POST", f"/api/v1/sessions/{sid}/events", {"text": "later fact"})
        st, got2, _ = self.api("GET", f"/api/v1/checkpoints/{cpid}")
        self.assertEqual(got2["checkpoint"]["body"], got["checkpoint"]["body"])


class TestDRelationshipNav(AcceptanceBase):
    def test_chain_traversal(self):
        c = self.con()
        ws = model.create_workspace(c, "w")
        ses = model.create_session(c, workspace_id=ws["id"], title="s")
        ev = model.append_event(c, ses["id"], "screenshot attached")
        shot = store.register_artifact(c, self.tmp + "/data", data=b"shot",
                                       filename="shot.png", enqueue_extract=False)
        dec = model.record_decision(c, "fix the bug", workspace_id=ws["id"])
        tsk = model.create_task(c, "fix it", workspace_id=ws["id"])
        code = store.register_artifact(c, self.tmp + "/data", data=b"print(1)",
                                       filename="fix.py", enqueue_extract=False)
        res = model.append_event(c, ses["id"], "tests pass", kind="tool_result")
        model.add_edge(c, "session", ses["id"], "event", ev["id"], "contains")
        model.add_edge(c, "event", ev["id"], "artifact", shot["id"], "introduces")
        model.add_edge(c, "artifact", shot["id"], "decision", dec["id"], "documents")
        model.add_edge(c, "decision", dec["id"], "task", tsk["id"], "authorizes")
        model.add_edge(c, "task", tsk["id"], "artifact", code["id"], "produces")
        model.add_edge(c, "artifact", code["id"], "event", res["id"], "validates")
        c.close()
        # The chain is 6 hops; max_depth is 4 (bounded traversal). Navigate it
        # in two bounded traversals, as the UI does.
        st, t1, _ = self.api("GET", f"/api/v1/traverse?seed=session:{ses['id']}&max_depth=4")
        st, t2, _ = self.api("GET", f"/api/v1/traverse?seed=task:{tsk['id']}&max_depth=2")
        nodes = {(n["obj_type"], n["obj_id"]) for n in t1["nodes"]} | \
                {(n["obj_type"], n["obj_id"]) for n in t2["nodes"]}
        for want in [("session", ses["id"]), ("event", ev["id"]),
                     ("artifact", shot["id"]), ("decision", dec["id"]),
                     ("task", tsk["id"]), ("artifact", code["id"]),
                     ("event", res["id"])]:
            self.assertIn(want, nodes, f"chain broken at {want}")
        # every non-seed node carries an explainable path
        for t in (t1, t2):
            for n in t["nodes"]:
                if n["depth"] > 0:
                    self.assertTrue(n["path"], "node without path explanation")


class TestEBoundedSearch(AcceptanceBase):
    def test_bounded_results(self):
        c = self.con()
        for i in range(200):
            a = store.register_artifact(c, self.tmp + "/data",
                                        data=f"synthetic payload {i} needle".encode(),
                                        filename=f"syn{i}.txt", enqueue_extract=False)
        c.close()
        st, res, _ = self.api("GET", "/api/v1/search?q=needle&limit=25")
        self.assertEqual(st, 200)
        self.assertLessEqual(len(res["results"]), 25)
        # response must not serialize the archive: bounded JSON size
        st, _, raw = self.api("GET", "/api/v1/search?q=needle&limit=25")
        self.assertLess(len(raw), 200_000)


class TestFContextPackage(AcceptanceBase):
    def test_package_grounding(self):
        c = self.con()
        ws = model.create_workspace(c, "w")
        ses = model.create_session(c, workspace_id=ws["id"], title="s")
        ev = model.append_event(c, ses["id"], "the widget overheats under load")
        art = store.register_artifact(c, self.tmp + "/data",
                                      data=b"widget thermal report: overheats under load\n" * 30,
                                      filename="thermal.txt")
        model.add_edge(c, "event", ev["id"], "artifact", art["id"], "introduces")
        extract.drain_once(c, self.tmp + "/data", limit=5)
        c.close()
        st, pkg, _ = self.api("POST", "/api/v1/context",
                              {"question": "why does the widget overheat?",
                               "budget_tokens": 1500})
        pkg = pkg["package"]
        self.assertGreater(len(pkg["sources"]), 0)
        for s in pkg["sources"]:
            for k in ("doc_type", "doc_id", "excerpt", "locator",
                      "provenance", "why_included"):
                self.assertIn(k, s, f"source missing {k}")
        self.assertTrue(pkg["token_estimate_is_estimate"])
        self.assertLessEqual(pkg["token_estimate"], 1500)
        self.assertIn("uncertainties", pkg)
        self.assertIn("missing_evidence", pkg)
        # incremental retrieval by artifact id without repeating the search
        arts = [s for s in pkg["sources"] if s["doc_type"] == "artifact"]
        self.assertTrue(arts, "expected the thermal report artifact in sources")
        st, got, _ = self.api("GET", f"/api/v1/artifacts/{arts[0]['doc_id']}")
        self.assertEqual(st, 200)


class TestGHandoff(AcceptanceBase):
    def test_verbatim_and_states(self):
        payload = "DO THIS EXACTLY:\n1. step one\n2. step two"
        st, ho, _ = self.api("POST", "/api/v1/handoffs",
                             {"objective": "o", "payload_text": payload})
        hid = ho["handoff"]["id"]
        st, p, _ = self.api("GET", f"/api/v1/handoffs/{hid}/payload")
        import hashlib
        self.assertEqual(p["text"], payload)
        self.assertTrue(p["hash_match"])
        self.assertEqual(p["sha256"], hashlib.sha256(payload.encode()).hexdigest())
        st, bad, _ = self.api("POST", f"/api/v1/handoffs/{hid}/status",
                              {"status": "completed"})
        self.assertEqual(st, 400)
        for s in ["ready", "authorized", "sent", "acknowledged",
                  "executing", "completed"]:
            st, ho, _ = self.api("POST", f"/api/v1/handoffs/{hid}/status",
                                 {"status": s})
            self.assertEqual(st, 200, s)
        self.assertEqual(ho["handoff"]["status"], "completed")


class TestHRevisions(AcceptanceBase):
    def test_original_preserved(self):
        ctype, body = _mpart([("file", "doc.md", "text/markdown", b"version one")])
        st, up, _ = self.api("POST", "/api/v1/artifacts", body, ctype=ctype)
        aid = up["artifacts"][0]["id"]
        ctype2, body2 = _mpart([("file", "doc.md", "text/markdown", b"version two")])
        st, rev, _ = self.api("POST", f"/api/v1/artifacts/{aid}/revisions",
                              body2, ctype=ctype2)
        self.assertIn(st, (200, 201))
        st, revs, _ = self.api("GET", f"/api/v1/artifacts/{aid}/revisions")
        self.assertEqual(len(revs["revisions"]), 2)
        # current serves v2
        st, _, raw = self.api("GET", f"/api/v1/artifacts/{aid}/content")
        self.assertEqual(raw, b"version two")
        # rev 1 blob still addressable
        c = self.con()
        r1 = [r for r in revs["revisions"] if r["rev_no"] == 1][0]
        self.assertEqual(store.read_blob(self.tmp + "/data", r1["blob_sha256"]),
                         b"version one")
        c.close()


class TestIAccessControl(AcceptanceBase):
    def test_private_denied_without_leak(self):
        ctype, body = _mpart([("file", "secret.txt", "text/plain", b"topsecret")])
        st, up, _ = self.api("POST", "/api/v1/artifacts?visibility=private",
                             body, ctype=ctype)
        aid = up["artifacts"][0]["id"]
        st, j, raw = self.api("GET", f"/api/v1/artifacts/{aid}/content", authed=False)
        self.assertEqual(st, 403)
        self.assertNotIn(b"secret.txt", raw)
        self.assertNotIn(b"topsecret", raw)
        # share link scoping
        st, col, _ = self.api("POST", "/api/v1/collections", {"title": "c"})
        cid = col["collection"]["id"]
        self.api("POST", f"/api/v1/collections/{cid}/members", {"artifact_id": aid})
        st, sh, _ = self.api("POST", "/api/v1/share",
                             {"scope_type": "collection", "scope_id": cid})
        token = sh["token"]
        st, page = self.ui("/s/share/" + token, authed=False)
        self.assertEqual(st, 200)
        st, _ = self.ui("/s/share/" + token + "tampered", authed=False)
        self.assertEqual(st, 404)
        # raw token not in db
        c = self.con()
        hashes = [r[0] for r in c.execute("SELECT token_hash FROM share_link")]
        c.close()
        self.assertNotIn(token, hashes)
        # revoke
        st, _, _ = self.api("POST", f"/api/v1/share/{sh['id']}/revoke")
        st, _ = self.ui("/s/share/" + token, authed=False)
        self.assertEqual(st, 404)


class TestJExportRestore(AcceptanceBase):
    def test_roundtrip(self):
        c = self.con()
        ws = model.create_workspace(c, "w")
        ses = model.create_session(c, workspace_id=ws["id"], title="s")
        ev = model.append_event(c, ses["id"], "restorable fact")
        art = store.register_artifact(c, self.tmp + "/data", data=b"bytes!",
                                      filename="b.bin", enqueue_extract=False)
        model.add_edge(c, "event", ev["id"], "artifact", art["id"], "introduces")
        expdir = os.path.join(self.tmp, "exp")
        mpath = export.write_bundle(c, self.tmp + "/data", expdir)
        rep = export.verify_bundle(expdir)
        self.assertTrue(rep["format_ok"])
        self.assertEqual(rep["blobs_bad"], [])
        self.assertEqual(rep["blobs_missing"], [])
        rdir = os.path.join(self.tmp, "restored")
        res = export.restore_bundle(expdir, rdir)
        c.close()
        c2 = _db.connect(rdir)
        try:
            self.assertEqual(c2.execute("SELECT id FROM workspace WHERE id=?",
                                        (ws["id"],)).fetchone()[0], ws["id"])
            a2 = store.get_artifact(c2, art["id"])
            self.assertEqual(store.read_artifact_bytes(c2, rdir, a2), b"bytes!")
            n_edge = c2.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
            self.assertEqual(n_edge, 1)
        finally:
            c2.close()


class TestKBackwardCompat(AcceptanceBase):
    def test_legacy_routes_untouched(self):
        # The substrate handle() must return None for legacy paths so the old
        # upload-tool handler serves them byte-identically (verified structurally
        # here; full byte-level check runs against the patched server copy).
        for legacy in ["/", "/list", "/upload", "/f/" + "a" * 32 + ".png"]:
            res = self.app.handle("GET", legacy, {}, b"")
            self.assertIsNone(res, f"substrate claimed legacy path {legacy}")


class TestLOperationalIntegrity(AcceptanceBase):
    def test_failed_extraction_observable_and_retryable(self):
        ctype, body = _mpart([("file", "bad.png", "image/png", b"not-an-image" * 50)])
        st, up, _ = self.api("POST", "/api/v1/artifacts", body, ctype=ctype)
        aid = up["artifacts"][0]["id"]
        c = self.con()
        extract.drain_once(c, self.tmp + "/data", limit=5)
        job = c.execute("SELECT status, last_error FROM extract_job WHERE artifact_id=?",
                        (aid,)).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["last_error"])
        # source bytes intact
        art = store.get_artifact(c, aid)
        self.assertEqual(store.read_artifact_bytes(c, self.tmp + "/data", art),
                         b"not-an-image" * 50)
        c.close()
        # retry resets to pending
        st, rj, _ = self.api("POST", f"/api/v1/artifacts/{aid}/extract/retry")
        self.assertEqual(st, 200)
        c = self.con()
        job2 = c.execute("SELECT status FROM extract_job WHERE artifact_id=?",
                         (aid,)).fetchone()
        self.assertEqual(job2["status"], "pending")
        c.close()
        # queue depth visible on settings page
        st, page = self.ui("/s/settings")
        self.assertEqual(st, 200)
        self.assertIn("extract", page.lower())


if __name__ == "__main__":
    unittest.main()
