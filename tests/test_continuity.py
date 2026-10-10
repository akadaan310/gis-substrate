"""Continuity milestone tests: protocols, task GET/PATCH, workspace context."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate import db as D
from substrate import model as M
from substrate.app import SubstrateApp


class ContinuityModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.tok = os.path.join(self.tmp, "token")
        open(self.tok, "w").write("test-secret")
        self.app = SubstrateApp(self.data, self.tok)
        self.con = self.app.connect()

    def test_protocol_kind_accepted(self):
        t = M.create_task(self.con, "Deploy checklist", kind="protocol",
                          body="Step 1: backup. Step 2: deploy.",
                          meta={"steps": [{"text": "backup", "done": False}]})
        self.assertEqual(t["kind"], "protocol")
        self.assertEqual(t["status"], "open")

    def test_protocol_kind_rejected_invalid(self):
        with self.assertRaises(ValueError):
            M.create_task(self.con, "Bad", kind="runbook")

    def test_update_task_partial(self):
        t = M.create_task(self.con, "Original", kind="task", body="v1")
        t2 = M.update_task(self.con, t["id"], title="Renamed")
        self.assertEqual(t2["title"], "Renamed")
        self.assertEqual(t2["body"], "v1")  # unchanged
        self.assertEqual(t2["status"], "open")  # unchanged

    def test_update_task_status(self):
        t = M.create_task(self.con, "Work", kind="task")
        t2 = M.update_task(self.con, t["id"], status="in_progress", note="started")
        self.assertEqual(t2["status"], "in_progress")

    def test_update_task_fts(self):
        t = M.create_task(self.con, "Alpha task", kind="task", body="alpha body")
        M.update_task(self.con, t["id"], body="beta body updated")
        # FTS should find the new body
        rows = self.con.execute(
            "SELECT doc_id FROM search_fts WHERE search_fts MATCH 'beta'").fetchall()
        self.assertTrue(any(r[0] == t["id"] for r in rows))

    def test_update_nonexistent(self):
        with self.assertRaises(KeyError):
            M.update_task(self.con, "tsk_nonexistent123", status="done")


class ContinuityApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.tok = os.path.join(self.tmp, "token")
        open(self.tok, "w").write("test-secret")
        self.app = SubstrateApp(self.data, self.tok)
        self.con = self.app.connect()
        self.ws = M.create_workspace(self.con, "Test WS")

    def _req(self, method, path, headers=None, body=b""):
        h = {"authorization": "Bearer test-secret"}
        if headers:
            h.update(headers)
        return self.app.handle(method, path, h, body)

    def test_create_protocol_via_api(self):
        body = json.dumps({
            "title": "API Protocol", "kind": "protocol",
            "body": "Steps here", "workspace_id": self.ws["id"],
            "meta": {"steps": [{"text": "s1", "done": False}]},
        }).encode()
        code, ctype, rbody = self._req("POST", "/api/v1/tasks", body=body)[:3]
        self.assertEqual(code, 201)
        d = json.loads(rbody)
        self.assertEqual(d["task"]["kind"], "protocol")

    def test_create_protocol_invalid_kind(self):
        body = json.dumps({"title": "Bad", "kind": "runbook"}).encode()
        code, _, rbody = self._req("POST", "/api/v1/tasks", body=body)[:3]
        self.assertEqual(code, 400)

    def test_get_single_task(self):
        t = M.create_task(self.con, "Get me", kind="task",
                          workspace_id=self.ws["id"])
        code, _, rbody = self._req("GET", "/api/v1/tasks/" + t["id"])[:3]
        self.assertEqual(code, 200)
        d = json.loads(rbody)
        self.assertEqual(d["task"]["id"], t["id"])
        self.assertIn("edges_out", d)
        self.assertIn("edges_in", d)

    def test_get_task_invalid_id(self):
        code, _, _ = self._req("GET", "/api/v1/tasks/not-a-valid-id")[:3]
        self.assertEqual(code, 400)

    def test_get_task_not_found(self):
        code, _, _ = self._req("GET", "/api/v1/tasks/tsk_doesnotexist123")[:3]
        self.assertEqual(code, 404)

    def test_get_task_unauth(self):
        t = M.create_task(self.con, "Secret", kind="task")
        code, _, _ = self.app.handle(
            "GET", "/api/v1/tasks/" + t["id"], {}, b"")[:3]
        self.assertEqual(code, 403)

    def test_patch_task_status(self):
        t = M.create_task(self.con, "Patch me", kind="task",
                          workspace_id=self.ws["id"])
        body = json.dumps({"status": "in_progress", "note": "starting"}).encode()
        # Simulate PATCH via direct app.handle (mount handles method routing)
        code, _, rbody = self._req("PATCH", "/api/v1/tasks/" + t["id"], body=body)[:3]
        self.assertEqual(code, 200)
        d = json.loads(rbody)
        self.assertEqual(d["task"]["status"], "in_progress")

    def test_patch_task_invalid_status(self):
        t = M.create_task(self.con, "Patch bad", kind="task")
        body = json.dumps({"status": "bogus"}).encode()
        code, _, _ = self._req("PATCH", "/api/v1/tasks/" + t["id"], body=body)[:3]
        self.assertEqual(code, 400)

    def test_patch_task_empty(self):
        t = M.create_task(self.con, "Patch empty", kind="task")
        body = json.dumps({}).encode()
        code, _, _ = self._req("PATCH", "/api/v1/tasks/" + t["id"], body=body)[:3]
        self.assertEqual(code, 400)

    def test_patch_task_unauth(self):
        t = M.create_task(self.con, "Nope", kind="task")
        body = json.dumps({"status": "done"}).encode()
        code, _, _ = self.app.handle(
            "PATCH", "/api/v1/tasks/" + t["id"], {}, body)[:3]
        self.assertEqual(code, 403)

    def test_workspace_context(self):
        p = M.create_task(self.con, "Proto", kind="protocol",
                          workspace_id=self.ws["id"])
        t = M.create_task(self.con, "Task1", kind="task",
                          workspace_id=self.ws["id"], status="in_progress")
        M.record_decision(self.con, "Decide X", "We chose X",
                          workspace_id=self.ws["id"])
        M.add_edge(self.con, "task", t["id"], "task", p["id"], "derives_from")
        code, _, rbody = self._req(
            "GET", "/api/v1/workspaces/" + self.ws["id"] + "/context")[:3]
        self.assertEqual(code, 200)
        d = json.loads(rbody)
        ctx = d["context"]
        self.assertEqual(len(ctx["protocols"]), 1)
        self.assertEqual(len(ctx["tasks"]), 1)
        self.assertEqual(len(ctx["decisions"]), 1)
        self.assertTrue(len(ctx["edges"]) >= 1)
        self.assertTrue(len(ctx["next_actions"]) >= 1)
        self.assertIn("truncated", ctx)
        self.assertIn("limits", ctx)
        # Next action should be the in_progress task
        self.assertEqual(ctx["next_actions"][0]["id"], t["id"])

    def test_workspace_context_not_found(self):
        code, _, _ = self._req(
            "GET", "/api/v1/workspaces/ws_nonexistent123/context")[:3]
        self.assertEqual(code, 404)

    def test_workspace_context_unauth(self):
        code, _, _ = self.app.handle(
            "GET", "/api/v1/workspaces/" + self.ws["id"] + "/context", {}, b"")[:3]
        self.assertEqual(code, 403)

    def test_cross_workspace_isolation(self):
        ws2 = M.create_workspace(self.con, "Other WS")
        M.create_task(self.con, "In WS1", kind="task", workspace_id=self.ws["id"])
        M.create_task(self.con, "In WS2", kind="task", workspace_id=ws2["id"])
        code, _, rbody = self._req(
            "GET", "/api/v1/workspaces/" + self.ws["id"] + "/context")[:3]
        d = json.loads(rbody)
        titles = [t["title"] for t in d["context"]["tasks"]]
        self.assertIn("In WS1", titles)
        self.assertNotIn("In WS2", titles)


if __name__ == "__main__":
    unittest.main()
