"""Boundary tests for request-body limits (P1C).

- HTTP mount patch: rejects oversized / malformed Content-Length before
  reading the body (413, no memory buffering).
- API artifact upload: per-file MAX_ARTIFACT_BYTES enforced before any
  artifact/revision/blob is written (no partial state).
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from substrate import db as D, store as S
from substrate.app import SubstrateApp, MAX_BODY
from substrate.api import MAX_ARTIFACT_BYTES


class _FakeSocket:
    """Minimal stand-in exercising the patched hook's length logic."""
    def __init__(self, headers):
        self.headers = headers


def hook_body_limit(headers):
    """Replicates the length gate from deploy/server_patch.py HOOK."""
    raw = headers.get("Content-Length", "0") or "0"
    try:
        length = int(str(raw).strip())
    except (ValueError, TypeError):
        length = -1
    if length < 0 or length > MAX_BODY:
        return 413
    return 200


class RequestLimitTest(unittest.TestCase):
    def test_hook_rejects_oversized(self):
        self.assertEqual(
            hook_body_limit({"Content-Length": str(MAX_BODY + 1)}), 413)

    def test_hook_accepts_at_limit(self):
        self.assertEqual(
            hook_body_limit({"Content-Length": str(MAX_BODY)}), 200)

    def test_hook_accepts_small(self):
        self.assertEqual(hook_body_limit({"Content-Length": "1024"}), 200)
        self.assertEqual(hook_body_limit({}), 200)

    def test_hook_rejects_malformed(self):
        for bad in ("abc", "12x", "  ", "-5", "99999999999999999999"):
            with self.subTest(value=bad):
                self.assertEqual(hook_body_limit({"Content-Length": bad}), 413)

    def test_hook_treats_empty_as_missing(self):
        # empty header value falls back to "0" (no body) — safe, not an error
        self.assertEqual(hook_body_limit({"Content-Length": ""}), 200)

    def test_patch_hook_source_matches(self):
        """The deployed hook must contain the same gate (no drift)."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "deploy", "server_patch.py")).read()
        self.assertIn("_MAX_BODY = 30 * 1024 * 1024", src)
        self.assertIn("request body too large", src)
        self.assertIn("close_connection = True", src)


class ArtifactUploadLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t-limits-")
        self.token = os.path.join(self.tmp, "tok")
        open(self.token, "w").write("secret")
        self.data_dir = os.path.join(self.tmp, "data")
        self.app = SubstrateApp(self.data_dir, self.token)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload(self, payload, filename="big.bin"):
        boundary = "BOUNDARY123"
        body = (
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
             % (boundary, filename)).encode() + payload +
            ("\r\n--%s--\r\n" % boundary).encode())
        res = self.app.handle(
            "POST", "/api/v1/artifacts?token=secret",
            {"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            body)
        return res[0], res[1], res[2]  # ignore optional 4th (headers)

    def _counts(self):
        con = D.connect(self.data_dir)
        try:
            return {
                "artifact": con.execute("SELECT COUNT(*) FROM artifact").fetchone()[0],
                "revision": con.execute(
                    "SELECT COUNT(*) FROM artifact_revision").fetchone()[0],
                "blob": con.execute("SELECT COUNT(*) FROM blob").fetchone()[0],
            }
        finally:
            con.close()

    def test_oversized_file_rejected_without_partial_state(self):
        before = self._counts()
        code, _ctype, body = self._upload(b"x" * (MAX_ARTIFACT_BYTES + 1))
        self.assertEqual(code, 413, body[:200])
        self.assertEqual(self._counts(), before,
                         "rejected upload left partial state")

    def test_at_limit_accepted(self):
        code, _ctype, _body = self._upload(b"y" * MAX_ARTIFACT_BYTES,
                                           filename="at-limit.bin")
        self.assertEqual(code, 201)

    def test_below_limit_accepted(self):
        code, _ctype, _body = self._upload(b"z" * 1024, filename="small.bin")
        self.assertEqual(code, 201)

    def test_request_limit_documented_distinction(self):
        # HTTP request cap vs per-artifact cap are different constants with
        # different purposes; the request cap must cover the artifact cap.
        self.assertGreater(MAX_BODY, MAX_ARTIFACT_BYTES)


if __name__ == "__main__":
    unittest.main()
