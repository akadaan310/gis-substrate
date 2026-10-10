"""Core tests: schema, blob store, artifacts, revisions, legacy indexing,
records, edges/traversal, decisions, tasks, claims, collections, checkpoints,
handoffs, share links, audit, export/restore. Stdlib unittest."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate import db as _db
from substrate import store, model, export


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="substrate-test-")
        self.data_dir = os.path.join(self.tmp, "data")
        self.con = _db.connect(self.data_dir)
        _db.init_db(self.con)
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('data_dir',?)",
                             (self.data_dir,))

    def tearDown(self):
        try:
            self.con.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSchema(Base):
    def test_schema_version(self):
        row = self.con.execute("SELECT value FROM kv WHERE key='schema_version'").fetchone()
        self.assertEqual(row[0], str(_db.SCHEMA_VERSION))

    def test_fts5_available(self):
        self.con.execute("INSERT INTO search_fts(doc_type,doc_id,title,body)"
                         " VALUES('t','1','hello','world')")
        rows = self.con.execute("SELECT doc_id FROM search_fts WHERE search_fts MATCH 'hello'").fetchall()
        self.assertEqual([r[0] for r in rows], ["1"])


class TestBlobStore(Base):
    def test_register_and_dedup(self):
        a1 = store.register_artifact(self.con, self.data_dir, data=b"hello",
                                     filename="a.txt", enqueue_extract=False)
        a2 = store.register_artifact(self.con, self.data_dir, data=b"hello",
                                     filename="b.txt", enqueue_extract=False)
        self.assertNotEqual(a1["id"], a2["id"])          # distinct records
        self.assertEqual(a1["blob_sha256"], a2["blob_sha256"])  # same bytes
        # only one blob file on disk
        blobs = []
        for _, _, files in os.walk(os.path.join(self.data_dir, "blobs")):
            blobs.extend(files)
        self.assertEqual(len(blobs), 1)
        self.assertEqual(store.read_artifact_bytes(self.con, self.data_dir, a1), b"hello")

    def test_duplicate_upload_registers_new_artifact(self):
        a1 = store.register_artifact(self.con, self.data_dir, data=b"x", filename="x.bin",
                                     enqueue_extract=False)
        a2 = store.register_artifact(self.con, self.data_dir, data=b"x", filename="x.bin",
                                     enqueue_extract=False)
        self.assertNotEqual(a1["id"], a2["id"])

    def test_revisions(self):
        a = store.register_artifact(self.con, self.data_dir, data=b"v1", filename="d.md",
                                    enqueue_extract=False)
        store.add_revision(self.con, self.data_dir, a["id"], data=b"v2", change_note="fix")
        revs = store.revisions(self.con, a["id"])
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[0]["rev_no"], 1)
        self.assertEqual(revs[1]["change_note"], "fix")
        cur = store.get_artifact(self.con, a["id"])
        self.assertEqual(store.read_artifact_bytes(self.con, self.data_dir, cur), b"v2")
        # rev 1 bytes still addressable via its blob
        self.assertEqual(store.read_blob(self.data_dir, revs[0]["blob_sha256"]), b"v1")

    def test_legacy_index_in_place(self):
        legacy = os.path.join(self.tmp, "legacy")
        os.makedirs(legacy)
        fp = os.path.join(legacy, "abc123.jpg")
        with open(fp, "wb") as f:
            f.write(b"fakejpg")
        art = store.index_legacy_file(self.con, self.data_dir, "abc123.jpg", fp,
                                      orig_name="photo.jpg", mime="image/jpeg")
        self.assertEqual(art["source"], "legacy")
        self.assertEqual(art["visibility"], "public-link")
        self.assertTrue(os.path.isfile(fp))  # not moved
        self.assertEqual(store.read_artifact_bytes(self.con, self.data_dir, art), b"fakejpg")
        # idempotent
        art2 = store.index_legacy_file(self.con, self.data_dir, "abc123.jpg", fp)
        self.assertEqual(art["id"], art2["id"])
        self.assertEqual(store.legacy_artifact_id(self.con, "abc123.jpg"), art["id"])


class TestRecords(Base):
    def test_workspace_session_events(self):
        ws = model.create_workspace(self.con, "FSCD")
        ses = model.create_session(self.con, workspace_id=ws["id"], title="s1",
                                   source="chat", source_id="chat-1")
        e1 = model.append_event(self.con, ses["id"], "hello", actor="abed", role="user")
        e2 = model.append_event(self.con, ses["id"], "hi", actor="muse", role="assistant")
        self.assertEqual(e1["seq"], 0)
        self.assertEqual(e2["seq"], 1)
        evs = model.list_events(self.con, ses["id"])
        self.assertEqual([e["seq"] for e in evs], [0, 1])
        # unknown timestamp stays unknown
        e3 = model.append_event(self.con, ses["id"], "no time", ts=None,
                                time_quality="unknown")
        self.assertIsNone(e3["ts"])
        self.assertEqual(e3["time_quality"], "unknown")

    def test_sessions_do_not_merge(self):
        ws = model.create_workspace(self.con, "w")
        s1 = model.create_session(self.con, workspace_id=ws["id"], title="a")
        s2 = model.create_session(self.con, workspace_id=ws["id"], title="b")
        model.append_event(self.con, s1["id"], "one")
        model.append_event(self.con, s2["id"], "two")
        self.assertEqual(len(model.list_events(self.con, s1["id"])), 1)
        self.assertEqual(len(model.list_events(self.con, s2["id"])), 1)

    def test_edges_and_traverse(self):
        ws = model.create_workspace(self.con, "w")
        ses = model.create_session(self.con, workspace_id=ws["id"], title="s")
        ev = model.append_event(self.con, ses["id"], "look at this")
        art = store.register_artifact(self.con, self.data_dir, data=b"img",
                                      filename="s.png", enqueue_extract=False)
        model.add_edge(self.con, "event", ev["id"], "artifact", art["id"],
                       "introduces", note="user uploaded")
        model.add_edge(self.con, "artifact", art["id"], "artifact", art["id"],
                       "derives_from", provenance="inferred", inferred=True)
        nb = model.neighbors(self.con, "event", ev["id"], direction="out")
        self.assertEqual(len(nb["out"]), 1)
        self.assertEqual(nb["out"][0]["rel"], "introduces")
        t = model.traverse(self.con, [("event", ev["id"])], max_depth=2)
        node_ids = {(n["obj_type"], n["obj_id"]) for n in t["nodes"]}
        self.assertIn(("event", ev["id"]), node_ids)
        self.assertIn(("artifact", art["id"]), node_ids)
        # inferred edge flagged
        inf = [e for e in t["edges"] if e["inferred"]]
        self.assertEqual(len(inf), 1)
        with self.assertRaises(ValueError):
            model.add_edge(self.con, "a", "1", "b", "2", "bogus_rel")

    def test_decision_supersede_preserves_history(self):
        ws = model.create_workspace(self.con, "w")
        d1 = model.record_decision(self.con, "use sqlite", workspace_id=ws["id"])
        d2 = model.record_decision(self.con, "use postgres", workspace_id=ws["id"],
                                   supersedes_id=d1["id"])
        self.assertEqual(model.get_decision(self.con, d1["id"])["status"], "superseded")
        self.assertEqual(d2["supersedes_id"], d1["id"])
        # original row still intact
        self.assertEqual(model.get_decision(self.con, d1["id"])["title"], "use sqlite")

    def test_task_question_status(self):
        ws = model.create_workspace(self.con, "w")
        t = model.create_task(self.con, "build x", workspace_id=ws["id"])
        q = model.create_task(self.con, "why y?", kind="question", workspace_id=ws["id"])
        model.set_task_status(self.con, t["id"], "in_progress")
        self.assertEqual(model.get_task(self.con, t["id"])["status"], "in_progress")
        # question stays open until explicitly resolved
        self.assertEqual(model.get_task(self.con, q["id"])["status"], "open")

    def test_claim_evidence(self):
        ws = model.create_workspace(self.con, "w")
        c = model.create_claim(self.con, "sqlite suffices", workspace_id=ws["id"])
        art = store.register_artifact(self.con, self.data_dir, data=b"bench",
                                      filename="b.txt", enqueue_extract=False)
        model.add_evidence(self.con, c["id"], stance="supports", artifact_id=art["id"],
                           locator="L1-10", note="benchmark output")
        ev = model.claim_evidence(self.con, c["id"])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["stance"], "supports")

    def test_collection(self):
        col = model.create_collection(self.con, "checkpoint pack")
        a1 = store.register_artifact(self.con, self.data_dir, data=b"1", filename="1.txt",
                                     enqueue_extract=False)
        a2 = store.register_artifact(self.con, self.data_dir, data=b"2", filename="2.txt",
                                     enqueue_extract=False)
        model.add_collection_member(self.con, col["id"], artifact_id=a2["id"])
        model.add_collection_member(self.con, col["id"], artifact_id=a1["id"])
        members = model.collection_members(self.con, col["id"])
        self.assertEqual([m["artifact_id"] for m in members], [a2["id"], a1["id"]])

    def test_checkpoint_immutable(self):
        ws = model.create_workspace(self.con, "w")
        c = model.create_checkpoint(self.con, "cp1", {"sessions": ["ses_x"]},
                                    workspace_id=ws["id"])
        body = json.loads(model.get_checkpoint(self.con, c["id"])["body_json"])
        self.assertEqual(body, {"sessions": ["ses_x"]})

    def test_handoff_lifecycle(self):
        ws = model.create_workspace(self.con, "w")
        h = model.create_handoff(self.con, "do the thing", payload_text="PROMPT VERBATIM",
                                 workspace_id=ws["id"], to_env="claude-code")
        self.assertEqual(h["status"], "prepared")
        art = store.get_artifact(self.con, h["payload_artifact_id"])
        self.assertEqual(store.read_artifact_bytes(self.con, self.data_dir, art),
                         b"PROMPT VERBATIM")
        # illegal jump rejected
        with self.assertRaises(ValueError):
            model.set_handoff_status(self.con, h["id"], "completed")
        for s in ("ready", "authorized", "sent", "acknowledged", "executing", "completed"):
            h = model.set_handoff_status(self.con, h["id"], s)
        self.assertEqual(h["status"], "completed")
        # terminal: no further transitions
        with self.assertRaises(ValueError):
            model.set_handoff_status(self.con, h["id"], "failed")

    def test_share_links(self):
        col = model.create_collection(self.con, "c")
        link = model.create_share_link(self.con, "collection", col["id"])
        self.assertIn("token", link)
        resolved = model.resolve_share_token(self.con, link["token"])
        self.assertEqual(resolved["scope_id"], col["id"])
        # raw token not stored
        rows = self.con.execute("SELECT token_hash FROM share_link").fetchall()
        self.assertNotIn(link["token"], [r[0] for r in rows])
        # expiry
        link2 = model.create_share_link(self.con, "collection", col["id"],
                                        expires_at=time.time() - 1)
        self.assertIsNone(model.resolve_share_token(self.con, link2["token"]))
        # revoke
        model.revoke_share_link(self.con, link["id"])
        self.assertIsNone(model.resolve_share_token(self.con, link["token"]))

    def test_audit(self):
        model.audit(self.con, "owner", "test_action", "artifact", "art_x", {"k": 1})
        tail = model.audit_tail(self.con, limit=10, action="test_action")
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["scope_id"], "art_x")


class TestExportRestore(Base):
    def test_export_verify_restore(self):
        ws = model.create_workspace(self.con, "w1")
        ses = model.create_session(self.con, workspace_id=ws["id"], title="s")
        ev = model.append_event(self.con, ses["id"], "hello world")
        art = store.register_artifact(self.con, self.data_dir, data=b"payload",
                                      filename="p.txt", enqueue_extract=False)
        model.add_edge(self.con, "event", ev["id"], "artifact", art["id"], "introduces")
        dec = model.record_decision(self.con, "d", workspace_id=ws["id"])
        exp_dir = os.path.join(self.tmp, "exp")
        mpath = export.write_bundle(self.con, self.data_dir, exp_dir)
        self.assertTrue(os.path.isfile(mpath))
        rep = export.verify_bundle(exp_dir)
        self.assertTrue(rep["format_ok"])
        self.assertEqual(rep["blobs_bad"], [])
        self.assertEqual(rep["blobs_missing"], [])
        # restore into empty dir
        rdir = os.path.join(self.tmp, "restored")
        res = export.restore_bundle(exp_dir, rdir)
        self.assertGreater(res["restored_tables"]["artifact"], 0)
        con2 = _db.connect(rdir)
        try:
            self.assertEqual(con2.execute("SELECT title FROM workspace WHERE id=?",
                                          (ws["id"],)).fetchone()[0], "w1")
            a2 = store.get_artifact(con2, art["id"])
            self.assertEqual(store.read_artifact_bytes(con2, rdir, a2), b"payload")
            # FTS rebuilt
            rows = con2.execute("SELECT doc_id FROM search_fts WHERE search_fts MATCH 'hello'").fetchall()
            self.assertTrue(any(r[0] == ev["id"] for r in rows))
        finally:
            con2.close()
        # restore into non-empty db refused
        with self.assertRaises(RuntimeError):
            export.restore_bundle(exp_dir, self.data_dir)


if __name__ == "__main__":
    unittest.main()


class TestMigration(unittest.TestCase):
    def test_legacy_migration_in_place(self):
        import json as _json
        tmp = tempfile.mkdtemp(prefix="substrate-mig-")
        try:
            legacy = os.path.join(tmp, "files")
            os.makedirs(legacy)
            entries = []
            for i, ext in enumerate([".jpg", ".pdf", ".mp4"]):
                lid = "%032x%s" % (i + 1, ext)
                with open(os.path.join(legacy, lid), "wb") as f:
                    f.write(b"data-%d" % i)
                entries.append({"id": lid, "name": "orig%d%s" % (i, ext),
                                "size": 6, "time": 1700000000.0 + i,
                                "type": "image/jpeg"})
            index = os.path.join(tmp, "index.jsonl")
            with open(index, "w") as f:
                for e in entries:
                    f.write(_json.dumps(e) + "\n")
            data_dir = os.path.join(tmp, "data")
            con = _db.connect(data_dir)
            _db.init_db(con)
            from substrate import migrate as _migrate  # noqa
            # emulate migrate.main() core loop via index_legacy_file
            for lid in sorted(set(e["id"] for e in entries)):
                e = next(x for x in entries if x["id"] == lid)
                art = store.index_legacy_file(con, data_dir, lid,
                                              os.path.join(legacy, lid),
                                              orig_name=e["name"], mime=e["type"])
                self.assertEqual(art["visibility"], "public-link")
                self.assertEqual(art["source"], "legacy")
            # originals untouched
            for e in entries:
                with open(os.path.join(legacy, e["id"]), "rb") as f:
                    self.assertTrue(f.read().startswith(b"data-"))
            # every legacy id resolves
            for e in entries:
                self.assertIsNotNone(store.legacy_artifact_id(con, e["id"]))
            # idempotent re-run
            n0 = con.execute("SELECT COUNT(*) FROM artifact").fetchone()[0]
            for e in entries:
                store.index_legacy_file(con, data_dir, e["id"],
                                        os.path.join(legacy, e["id"]))
            n1 = con.execute("SELECT COUNT(*) FROM artifact").fetchone()[0]
            self.assertEqual(n0, n1)
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNoPathLeak(unittest.TestCase):
    def test_context_provenance_has_no_host_path(self):
        from substrate import context as _ctx, search as _search
        tmp = tempfile.mkdtemp(prefix="substrate-noleak-")
        try:
            data_dir = os.path.join(tmp, "data")
            con = _db.connect(data_dir)
            _db.init_db(con)
            legacy_dir = os.path.join(tmp, "legacy")
            os.makedirs(legacy_dir)
            lid = "a" * 32 + ".jpg"
            with open(os.path.join(legacy_dir, lid), "wb") as f:
                f.write(b"img-bytes")
            art = store.index_legacy_file(con, data_dir, lid,
                                          os.path.join(legacy_dir, lid),
                                          orig_name="shot.jpg", mime="image/jpeg")
            pkg = _ctx.build_package(
                con, "shot", budget_tokens=2000, data_dir=data_dir)
            provs = [s["provenance"] for s in pkg["sources"]]
            self.assertTrue(any("legacy_id=%s" % lid in p for p in provs),
                            "legacy_id should appear in provenance")
            for p in provs:
                self.assertNotIn(legacy_dir, p,
                                 "host path leaked into provenance: %s" % p)
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
