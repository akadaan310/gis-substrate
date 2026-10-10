"""Tests for the read-only integrity checker (P5)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from substrate import db as D, model as M, store as S, check as C


class IntegrityCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-check-")
        self.data = os.path.join(self.tmp, "data")
        self.con = D.connect(self.data)
        D.init_db(self.con)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _artifact(self, data=b"hello"):
        return S.register_artifact(self.con, self.data, data=data,
                                   filename="f.txt", mime="text/plain",
                                   enqueue_extract=False)

    def test_clean_archive_passes(self):
        self._artifact()
        M.create_workspace(self.con, "w")
        rep = C.check(self.data)
        self.assertTrue(rep["ok"], rep["problems"])
        self.assertEqual(rep["checks"]["artifacts"], 1)

    def test_missing_blob_file_detected(self):
        art = self._artifact()
        p = S.blob_path(self.data, art["blob_sha256"])
        os.remove(p)
        rep = C.check(self.data)
        self.assertFalse(rep["ok"])
        kinds = {pr["kind"] for pr in rep["problems"]}
        self.assertIn("blob_file_missing", kinds)
        self.assertIn("artifact_blob_file_missing", kinds)

    def test_corrupted_blob_detected(self):
        art = self._artifact()
        p = S.blob_path(self.data, art["blob_sha256"])
        with open(p, "r+b") as f:
            f.seek(0)
            f.write(b"X")
        rep = C.check(self.data)
        kinds = {pr["kind"] for pr in rep["problems"]}
        self.assertIn("blob_hash_mismatch", kinds)

    def test_orphan_blob_file_reported_not_corruption(self):
        self._artifact()
        # drop an unreferenced file on disk
        import hashlib
        fake_sha = hashlib.sha256(b"orphan").hexdigest()
        p = S.blob_path(self.data, fake_sha)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"orphan")
        rep = C.check(self.data)
        self.assertEqual(rep["checks"]["orphan_blob_files"], 1)
        # orphan files are informational; other checks still pass
        real_problems = [pr for pr in rep["problems"]
                         if pr["kind"] != "orphan_blob_files"]
        self.assertEqual(real_problems, [])

    def test_dangling_edge_detected(self):
        a1 = self._artifact(data=b"one")
        self.con.execute(
            "INSERT INTO edge(id, src_type, src_id, dst_type, dst_id, rel,"
            " note, provenance, inferred, created_at)"
            " VALUES('edge_bad','artifact',?,'artifact','art_missing',"
            " 'references','','explicit',0,0)", (a1["id"],))
        self.con.commit()
        rep = C.check(self.data)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(pr["kind"] == "edge_dangling"
                            for pr in rep["problems"]))

    def test_dangling_chunk_reference_detected(self):
        # SQLite FK constraints normally prevent this; disable them briefly
        # to simulate a corrupted database.
        self.con.execute("PRAGMA foreign_keys=OFF")
        self.con.execute(
            "INSERT INTO extracted_chunk(artifact_id, chunk_no, page_no, text)"
            " VALUES('art_missing',0,'p1','text')")
        self.con.commit()
        self.con.execute("PRAGMA foreign_keys=ON")
        rep = C.check(self.data)
        self.assertTrue(any(pr["kind"] == "dangling_reference"
                            for pr in rep["problems"]))

    def test_stale_extraction_detected(self):
        art = self._artifact(data=b"v1")
        S.add_revision(self.con, self.data, art["id"], data=b"v2")
        # simulate chunks left behind from the old revision
        # (add_revision normally clears them; this simulates a crash mid-way)
        self.con.execute(
            "INSERT INTO extracted_chunk(artifact_id, chunk_no, page_no, text,"
            " revision_id) VALUES(?,0,'p1','old text','rev_old')", (art["id"],))
        self.con.commit()
        rep = C.check(self.data)
        self.assertTrue(any(pr["kind"] == "stale_extraction"
                            for pr in rep["problems"]))

    def test_missing_fts_detected(self):
        art = self._artifact()
        self.con.execute(
            "DELETE FROM search_fts WHERE doc_type='artifact' AND doc_id=?",
            (art["id"],))
        self.con.commit()
        rep = C.check(self.data)
        kinds = {pr["kind"] for pr in rep["problems"]}
        self.assertIn("fts_missing", kinds)

    def test_report_format_has_no_content(self):
        art = self._artifact(data=b"secret-document-contents-12345")
        out = C.format_report(C.check(self.data))
        self.assertNotIn("secret-document-contents", out)
        self.assertIn("all checks passed", out)


if __name__ == "__main__":
    unittest.main()
