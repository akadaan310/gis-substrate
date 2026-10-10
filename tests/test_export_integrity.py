"""Regression tests for the export/restore integrity defects found in the
2026-10-09 production-readiness audit (commit a3abb7a).

P1A: unscoped export must include every artifact; every exported
     relationship must have valid endpoints; restore must reject
     dangling references.
P1B: extracted text must survive export/restore and stay searchable;
     restored FTS must not silently be metadata-only.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from substrate import db as D, model as M, store as S, export as X, extract as E


class ExportRestoreIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-exportfix-")
        self.src = os.path.join(self.tmp, "src")
        self.con = D.connect(self.src)
        D.init_db(self.con)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rich_archive(self):
        """Build an archive exercising every inclusion path."""
        con = self.con
        ws = M.create_workspace(con, "w1")
        ses = M.create_session(con, workspace_id=ws["id"], title="s1")
        ev = M.append_event(con, ses["id"], "event text", actor="t", kind="m")
        # 1. unreferenced artifact (the audit's dropped case)
        a_lonely = S.register_artifact(
            con, self.src, data=b"lonely bytes", filename="lonely.txt",
            mime="text/plain", enqueue_extract=False)
        # 2. artifact in a collection
        a_coll = S.register_artifact(
            con, self.src, data=b"collection bytes", filename="in-coll.txt",
            mime="text/plain", enqueue_extract=False)
        coll = M.create_collection(con, "c1", "test collection")
        M.add_collection_member(con, coll["id"], a_coll["id"])
        # 3. artifacts connected only by an edge
        a_e1 = S.register_artifact(
            con, self.src, data=b"edge one", filename="e1.txt",
            mime="text/plain", enqueue_extract=False)
        a_e2 = S.register_artifact(
            con, self.src, data=b"edge two", filename="e2.txt",
            mime="text/plain", enqueue_extract=False)
        M.add_edge(con, "artifact", a_e1["id"], "artifact", a_e2["id"],
                   "references", provenance="explicit")
        # 4. records referencing artifacts
        a_ev = S.register_artifact(
            con, self.src, data=b"evidence bytes", filename="ev.txt",
            mime="text/plain", enqueue_extract=False)
        claim = M.create_claim(con, workspace_id=ws["id"], title="cl",
                               body="claim body")
        M.add_evidence(con, claim["id"], artifact_id=a_ev["id"],
                       locator="p1", note="n")
        dec = M.record_decision(con, "d", body="decided",
                                workspace_id=ws["id"])
        task = M.create_task(con, workspace_id=ws["id"], title="t",
                             body="todo")
        # 5. duplicate blob content, distinct identities
        a_dup1 = S.register_artifact(
            con, self.src, data=b"same-bytes", filename="dup1.txt",
            mime="text/plain", enqueue_extract=False)
        a_dup2 = S.register_artifact(
            con, self.src, data=b"same-bytes", filename="dup2.txt",
            mime="text/plain", enqueue_extract=False)
        self.assertNotEqual(a_dup1["id"], a_dup2["id"])
        return {"lonely": a_lonely, "coll": a_coll, "e1": a_e1, "e2": a_e2,
                "ev": a_ev, "dup1": a_dup1, "dup2": a_dup2}

    def _counts(self, con):
        return {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                for t in ("artifact", "artifact_revision", "edge",
                          "collection_member", "extracted_chunk")}

    def test_unscoped_export_is_complete(self):
        """P1A: every artifact is exported; every edge endpoint resolves."""
        ids = self._rich_archive()
        dest = os.path.join(self.tmp, "bundle")
        X.write_bundle(D.connect(self.src), self.src, dest)
        rep = X.verify_bundle(dest)
        self.assertTrue(rep["format_ok"])
        self.assertTrue(rep["structure_ok"], rep["structure_problems"])
        # all 7 artifacts exported, including the unreferenced one
        import json
        manifest = json.load(open(os.path.join(dest, "manifest.json")))
        exported = {a["id"] for a in manifest["tables"]["artifact"]}
        for k, a in ids.items():
            self.assertIn(a["id"], exported, "dropped: %s" % k)
        # every edge endpoint resolves within the bundle
        all_ids = exported | {
            r["id"] for t in manifest["tables"].values() for r in t
            if isinstance(r, dict) and r.get("id")}
        for e in manifest["tables"]["edge"]:
            self.assertIn(e["src_id"], all_ids)
            self.assertIn(e["dst_id"], all_ids)

    def test_restore_rejects_dangling_references(self):
        """P1A: a bundle with an edge to a missing artifact is refused."""
        self._rich_archive()
        dest = os.path.join(self.tmp, "bundle")
        X.write_bundle(D.connect(self.src), self.src, dest)
        import json
        mpath = os.path.join(dest, "manifest.json")
        manifest = json.load(open(mpath))
        # sabotage: drop one artifact but keep an edge pointing at it
        victim = manifest["tables"]["artifact"][0]["id"]
        manifest["tables"]["artifact"] = [
            a for a in manifest["tables"]["artifact"] if a["id"] != victim]
        manifest["tables"]["edge"].append({
            "id": "edge_evil", "src_type": "artifact", "src_id": victim,
            "dst_type": "artifact",
            "dst_id": manifest["tables"]["artifact"][0]["id"],
            "rel": "references", "note": "", "provenance": "explicit",
            "inferred": 0, "created_at": 0})
        json.dump(manifest, open(mpath, "w"))
        rep = X.verify_bundle(dest)
        self.assertFalse(rep["structure_ok"])
        self.assertTrue(any("edge_evil" in p for p in rep["structure_problems"]))
        with self.assertRaises(RuntimeError):
            X.restore_bundle(dest, os.path.join(self.tmp, "rst"))

    def test_scoped_export_inclusion_rules(self):
        """P1A: scoped exports document and enforce their inclusion rules."""
        ids = self._rich_archive()
        ws_id = self.con.execute(
            "SELECT id FROM workspace").fetchone()[0]
        dest = os.path.join(self.tmp, "scoped")
        X.write_bundle(D.connect(self.src), self.src, dest, workspace_id=ws_id)
        rep = X.verify_bundle(dest)
        self.assertTrue(rep["structure_ok"], rep["structure_problems"])
        import json
        manifest = json.load(open(os.path.join(dest, "manifest.json")))
        exported = {a["id"] for a in manifest["tables"]["artifact"]}
        # the evidence artifact (referenced by an in-scope claim) is included
        self.assertIn(ids["ev"]["id"], exported)
        # every exported edge has both endpoints in the bundle
        all_ids = {r["id"] for t in manifest["tables"].values() for r in t
                   if isinstance(r, dict) and r.get("id")}
        for e in manifest["tables"]["edge"]:
            self.assertIn(e["src_id"], all_ids)
            self.assertIn(e["dst_id"], all_ids)

    def test_extracted_text_survives_restore_and_search(self):
        """P1B: a unique term from extracted content is searchable post-restore."""
        con = self.con
        marker = "quixotic-zyzzyva-plugh"
        art = S.register_artifact(
            con, self.src,
            data=("body text with the unique marker %s buried inside" % marker
                  ).encode(),
            filename="doc.txt", mime="text/plain")
        # run extraction synchronously
        E.drain_once(con, self.src, limit=10)
        from substrate import search as SE
        hits = SE.search_fulltext(con, marker)
        self.assertTrue(any(h["doc_id"] == art["id"] for h in hits),
                        "marker not searchable before export")
        dest = os.path.join(self.tmp, "bundle")
        X.write_bundle(D.connect(self.src), self.src, dest)
        rdir = os.path.join(self.tmp, "restored")
        X.restore_bundle(dest, rdir)
        rcon = D.connect(rdir)
        try:
            rhits = SE.search_fulltext(rcon, marker)
            self.assertTrue(any(h["doc_id"] == art["id"] for h in rhits),
                            "marker lost after restore")
            # chunk provenance survived
            chunks = rcon.execute(
                "SELECT backend, method, revision_id FROM extracted_chunk "
                "WHERE artifact_id=?", (art["id"],)).fetchall()
            self.assertTrue(len(chunks) > 0)
            self.assertTrue(chunks[0][0])  # backend recorded
            # fts_status distinguishes complete from rebuilding
            import json as _json
            row = rcon.execute(
                "SELECT value FROM kv WHERE key='fts_status'").fetchone()
            self.assertIsNotNone(row)
            st = _json.loads(row[0])
            self.assertEqual(st["index"], "complete")
        finally:
            rcon.close()

    def test_full_roundtrip_preserves_everything(self):
        """P1A+P1B: identifiers, revisions, relationships, chunks, hashes."""
        ids = self._rich_archive()
        # add a second revision to one artifact
        S.add_revision(self.con, self.src, ids["lonely"]["id"],
                       data=b"lonely bytes v2", change_note="v2")
        before = self._counts(self.con)
        dest = os.path.join(self.tmp, "bundle")
        X.write_bundle(D.connect(self.src), self.src, dest)
        rdir = os.path.join(self.tmp, "restored")
        X.restore_bundle(dest, rdir)
        rcon = D.connect(rdir)
        try:
            after = self._counts(rcon)
            for t in ("artifact", "artifact_revision", "edge",
                      "collection_member"):
                self.assertEqual(before[t], after[t], "count mismatch: %s" % t)
            # identifiers stable
            for k, a in ids.items():
                row = rcon.execute("SELECT id, filename FROM artifact WHERE id=?",
                                   (a["id"],)).fetchone()
                self.assertIsNotNone(row, "lost artifact: %s" % k)
                self.assertEqual(row[1], a["filename"])
            # bytes identical
            for k, a in ids.items():
                orig_row = dict(self.con.execute(
                    "SELECT * FROM artifact WHERE id=?", (a["id"],)).fetchone())
                rest_row = dict(rcon.execute(
                    "SELECT * FROM artifact WHERE id=?", (a["id"],)).fetchone())
                orig = S.read_artifact_bytes(self.con, self.src, orig_row)
                rest = S.read_artifact_bytes(rcon, rdir, rest_row)
                self.assertEqual(orig, rest, "bytes differ: %s" % k)
            # edge endpoints all resolve
            art_ids = {r[0] for r in rcon.execute("SELECT id FROM artifact")}
            all_ids = art_ids | {r[0] for r in
                                 rcon.execute("SELECT id FROM workspace")} | \
                {r[0] for r in rcon.execute("SELECT id FROM session")} | \
                {r[0] for r in rcon.execute("SELECT id FROM event")} | \
                {r[0] for r in rcon.execute("SELECT id FROM decision")} | \
                {r[0] for r in rcon.execute("SELECT id FROM task")} | \
                {r[0] for r in rcon.execute("SELECT id FROM claim")} | \
                {r[0] for r in rcon.execute("SELECT id FROM collection")} | \
                {r[0] for r in rcon.execute("SELECT id FROM handoff")}
            for eid, s, d in rcon.execute(
                    "SELECT id, src_id, dst_id FROM edge"):
                self.assertIn(s, all_ids, "dangling edge %s src" % eid)
                self.assertIn(d, all_ids, "dangling edge %s dst" % eid)
        finally:
            rcon.close()


if __name__ == "__main__":
    unittest.main()
