"""Extraction pipeline tests (P2): PDF embedded text, DOCX, image OCR,
format states, and provenance.

Tests requiring optional system dependencies (tesseract) are marked and
SKIPPED (not silently passed) when the dependency is unavailable.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from substrate import db as D, store as S, extract as E
from substrate import search as SE

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def has_tool(name):
    import shutil as _sh
    return bool(_sh.which(name))


class ExtractionFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-extract-")
        self.src = os.path.join(self.tmp, "src")
        self.con = D.connect(self.src)
        D.init_db(self.con)

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_file(self, path, mime):
        with open(path, "rb") as f:
            data = f.read()
        return S.register_artifact(
            self.con, self.src, data=data,
            filename=os.path.basename(path), mime=mime)

    def _chunks(self, aid):
        return self.con.execute(
            "SELECT page_no, text, backend, method, revision_id, source_sha256"
            " FROM extracted_chunk WHERE artifact_id=? ORDER BY chunk_no",
            (aid,)).fetchall()

    def test_pdf_embedded_text(self):
        """PDF with embedded text: extracted per page, no OCR needed."""
        art = self._register_file(os.path.join(FIX, "test-embedded.pdf"),
                                  "application/pdf")
        E.drain_once(self.con, self.src, limit=5)
        chunks = self._chunks(art["id"])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "p1")
        self.assertEqual(chunks[1][0], "p2")
        self.assertIn("searchable content", chunks[0][1])
        self.assertIn("zyzzyva-q", chunks[1][1])
        # provenance: embedded-text, not OCR
        self.assertEqual(chunks[0][3], "embedded-text")
        self.assertTrue(chunks[0][2])  # backend recorded
        self.assertTrue(chunks[0][5])  # source_sha256 recorded
        # searchable
        hits = SE.search_fulltext(self.con, "zyzzyva-q")
        self.assertTrue(any(h["doc_id"] == art["id"] for h in hits))

    def test_docx_extraction(self):
        """DOCX text extracted via stdlib; paragraphs preserved."""
        art = self._register_file(
            os.path.join(FIX, "test-doc.docx"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        E.drain_once(self.con, self.src, limit=5)
        chunks = self._chunks(art["id"])
        self.assertTrue(len(chunks) >= 1)
        full = "\n".join(c[1] for c in chunks)
        self.assertIn("docx-alpha-bravo", full)
        self.assertIn("docx-charlie-delta", full)
        self.assertEqual(chunks[0][3], "ooxml-text")
        hits = SE.search_fulltext(self.con, "docx-charlie-delta")
        self.assertTrue(any(h["doc_id"] == art["id"] for h in hits))

    def test_docx_malformed(self):
        """Invalid DOCX fails honestly without touching the artifact."""
        art = S.register_artifact(
            self.con, self.src, data=b"not a zip at all",
            filename="bad.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        E.drain_once(self.con, self.src, limit=5)
        job = self.con.execute(
            "SELECT status, last_error FROM extract_job WHERE artifact_id=?",
            (art["id"],)).fetchone()
        self.assertEqual(job[0], "failed")
        self.assertIn("zip", job[1].lower())
        # artifact bytes intact
        row = dict(self.con.execute("SELECT * FROM artifact WHERE id=?",
                                    (art["id"],)).fetchone())
        self.assertEqual(
            S.read_artifact_bytes(self.con, self.src, row), b"not a zip at all")

    @unittest.skipUnless(has_tool("tesseract"), "tesseract not installed")
    def test_image_ocr(self):
        """Image OCR produces text with backend provenance (needs tesseract)."""
        art = self._register_file(os.path.join(FIX, "test-ocr.png"),
                                  "image/png")
        E.drain_once(self.con, self.src, limit=5)
        chunks = self._chunks(art["id"])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][3], "ocr")
        self.assertIn("tesseract", chunks[0][2].lower())
        # OCR text is approximate; check for a distinctive token
        text = chunks[0][1].lower().replace(" ", "")
        self.assertIn("alpha-beta-gamma", text,
                      "OCR text was: %r" % chunks[0][1][:100])

    def test_image_without_tesseract_reports_honestly(self):
        """Without tesseract, image extraction reports unavailable (not fake)."""
        if has_tool("tesseract"):
            self.skipTest("tesseract is installed; cannot test missing-dep path")
        art = self._register_file(os.path.join(FIX, "test-ocr.png"),
                                  "image/png")
        E.drain_once(self.con, self.src, limit=5)
        meta = self.con.execute(
            "SELECT meta_json FROM artifact WHERE id=?",
            (art["id"],)).fetchone()[0]
        import json
        info = json.loads(meta).get("image", {})
        self.assertIn("error", info.get("ocr", {}))
        self.assertIn("tesseract", info["ocr"]["error"])

    def test_new_revision_does_not_serve_stale_content(self):
        """After add_revision, old extracted text is gone from FTS."""
        art = S.register_artifact(
            self.con, self.src, data=b"original content with marker OLD-MARKER-XYZ",
            filename="rev.txt", mime="text/plain")
        E.drain_once(self.con, self.src, limit=5)
        self.assertTrue(SE.search_fulltext(self.con, "OLD-MARKER-XYZ"))
        S.add_revision(self.con, self.src, art["id"],
                       data=b"new content with marker NEW-MARKER-XYZ",
                       change_note="v2")
        # old marker must NOT be searchable; new content not yet extracted
        self.assertFalse(SE.search_fulltext(self.con, "OLD-MARKER-XYZ"),
                         "stale content served after revision")
        self.assertFalse(self.con.execute(
            "SELECT COUNT(*) FROM extracted_chunk WHERE artifact_id=?",
            (art["id"],)).fetchone()[0])
        # a fresh job was enqueued
        job = self.con.execute(
            "SELECT status FROM extract_job WHERE artifact_id=? "
            "ORDER BY created_at DESC LIMIT 1", (art["id"],)).fetchone()
        self.assertEqual(job[0], "pending")
        # after extraction, new marker is searchable
        E.drain_once(self.con, self.src, limit=5)
        self.assertTrue(SE.search_fulltext(self.con, "NEW-MARKER-XYZ"))

    def test_reprocess_same_revision_is_idempotent(self):
        """Re-running extraction on the same bytes yields identical chunks."""
        art = S.register_artifact(
            self.con, self.src, data=b"idempotent content here",
            filename="idem.txt", mime="text/plain")
        E.drain_once(self.con, self.src, limit=5)
        before = self._chunks(art["id"])
        # manually re-enqueue and re-run
        self.con.execute(
            "UPDATE extract_job SET status='pending' WHERE artifact_id=?",
            (art["id"],))
        self.con.commit()
        E.drain_once(self.con, self.src, limit=5)
        after = self._chunks(art["id"])
        self.assertEqual(
            [(c[0], c[1]) for c in before], [(c[0], c[1]) for c in after])

    def test_atomic_claim_prevents_double_processing(self):
        """Two workers cannot claim the same job (simulated)."""
        art = S.register_artifact(
            self.con, self.src, data=b"claim test",
            filename="claim.txt", mime="text/plain")
        job = self.con.execute(
            "SELECT * FROM extract_job WHERE artifact_id=?",
            (art["id"],)).fetchone()
        # simulate worker 1 claiming
        import time
        with self.con:
            cur = self.con.execute(
                "UPDATE extract_job SET status='processing', updated_at=? "
                "WHERE id=? AND status='pending'",
                (time.time(), job["id"]))
            self.assertEqual(cur.rowcount, 1)
        # simulate worker 2 trying to claim the same job
        with self.con:
            cur = self.con.execute(
                "UPDATE extract_job SET status='processing', updated_at=? "
                "WHERE id=? AND status='pending'",
                (time.time(), job["id"]))
            self.assertEqual(cur.rowcount, 0,
                             "second worker claimed an already-claimed job")

    def test_stale_job_reaping(self):
        """Jobs stuck in 'processing' are reset to pending by the reaper."""
        art = S.register_artifact(
            self.con, self.src, data=b"reap test",
            filename="reap.txt", mime="text/plain")
        import time
        old = time.time() - 7200
        self.con.execute(
            "UPDATE extract_job SET status='processing', updated_at=? "
            "WHERE artifact_id=?", (old, art["id"]))
        self.con.commit()
        n = E.reap_stale_jobs(self.con, stale_after=3600)
        self.assertEqual(n, 1)
        st = self.con.execute(
            "SELECT status FROM extract_job WHERE artifact_id=?",
            (art["id"],)).fetchone()[0]
        self.assertEqual(st, "pending")


if __name__ == "__main__":
    unittest.main()
