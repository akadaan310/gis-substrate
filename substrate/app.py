"""Substrate app skeleton: request contract, auth, routing, worker.

Mount contract for api.py / ui.py:
    def handle(con, data_dir, req) -> (status:int, content_type:str, body:bytes) | None
Return None when the path is not handled by that module.

Req fields: method, path, query (dict), headers (lower-cased dict), body (bytes),
             token (str|None), authed (bool), form (multipart fields, parsed lazily).
"""
import hmac
import json
import os
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs

from . import db as _db

MAX_BODY = 30 * 1024 * 1024


class Req:
    def __init__(self, method, path, query, headers, body, token, authed,
                 token_via_query=False):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.token = token
        self.authed = authed
        # True if the token arrived via ?token= (vs cookie/Bearer). The UI
        # only propagates ?token= in generated links when the session depends
        # on it, reducing token-in-URL exposure.
        self.token_via_query = token_via_query
        self._form = None

    @property
    def form(self):
        """Parsed multipart fields: list of (name, filename, content_type, bytes)."""
        if self._form is None:
            self._form = parse_multipart(self.headers.get("content-type", ""), self.body)
        return self._form

    def q(self, name, default=""):
        return self.query.get(name, default)


def load_secret(token_path):
    try:
        with open(token_path) as f:
            return f.read().strip()
    except OSError:
        return ""


def extract_token(query, headers):
    """Return (token, via_query). via_query is True only when the token came
    from the ?token= query parameter (vs Authorization header or cookie)."""
    tok = query.get("token", "")
    if tok:
        return tok, True
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), False
    cookie = headers.get("cookie", "")
    if cookie:
        try:
            c = SimpleCookie()
            c.load(cookie)
            if "substrate_token" in c:
                return c["substrate_token"].value, False
        except Exception:
            pass
    return "", False


def check_auth(secret, token):
    return bool(secret) and bool(token) and hmac.compare_digest(token, secret)


def parse_multipart(content_type, body):
    """Minimal multipart/form-data parser (stdlib, no cgi)."""
    fields = []
    if "multipart/form-data" not in content_type or not body:
        return fields
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip().strip('"')
    if not boundary:
        return fields
    sep = ("--" + boundary).encode()
    chunks = body.split(sep)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if not head:
            continue
        name, filename, ctype = "", "", "application/octet-stream"
        for line in head.decode("latin-1").split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                for seg in line.split(";"):
                    seg = seg.strip()
                    if seg.startswith("name="):
                        name = seg[5:].strip().strip('"')
                    elif seg.startswith("filename="):
                        filename = seg[9:].strip().strip('"')
            elif low.startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        fields.append((name, os.path.basename(filename), ctype, payload))
    return fields


# ---------------------------------------------------------------- responses

def json_resp(status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def err(status, code, message):
    return json_resp(status, {"error": {"code": code, "message": message}})


def html_resp(status, html):
    if isinstance(html, str):
        html = html.encode("utf-8")
    return status, "text/html; charset=utf-8", html


def page_args(req, default_limit=50, max_limit=500):
    try:
        limit = int(req.q("limit", str(default_limit)))
    except ValueError:
        limit = default_limit
    limit = max(1, min(limit, max_limit))
    cursor = req.q("cursor", "")
    return limit, cursor or None


# ---------------------------------------------------------------- app

class SubstrateApp:
    def __init__(self, data_dir, token_path):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.secret = load_secret(token_path)
        con = _db.connect(data_dir)
        _db.init_db(con)
        with con:
            con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('data_dir', ?)",
                        (data_dir,))
        con.close()
        self._worker_stop = threading.Event()
        self._worker = None

    def connect(self):
        return _db.connect(self.data_dir)

    def make_req(self, method, path, headers, body):
        u = urlparse(path)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        headers = {k.lower(): v for k, v in headers.items()}
        token, via_query = extract_token(query, headers)
        authed = check_auth(self.secret, token)
        return Req(method, u.path, query, headers, body or b"", token,
                   authed, token_via_query=via_query)

    def handle(self, method, path, headers, body):
        """Route to api/ui modules. Returns (status, ctype, body),
        (status, ctype, body, extra_headers), or None."""
        req = self.make_req(method, path, headers, body)
        if req.path == "/.well-known/purl" and method == "GET":
            # PURL instance manifest: public discovery, no auth required.
            # Describes operations; does not expose data.
            try:
                import os, json
                mf_path = os.path.join(os.path.dirname(__file__), "..",
                                       "docs", "GIS_PURL_MANIFEST.json")
                with open(mf_path) as f:
                    manifest = f.read()
                return (200, "application/json; charset=utf-8",
                        manifest.encode("utf-8"))
            except FileNotFoundError:
                return (404, "application/json; charset=utf-8",
                        b'{"error":"manifest not found"}')
        if req.path.startswith("/api/v1/"):
            try:
                from . import api
            except ImportError:
                return (501, "application/json; charset=utf-8",
                        b'{"error":{"code":"not_ready","message":"api module not loaded"}}')
            return api.handle(self.connect(), self.data_dir, req)
        if req.path.startswith("/s/"):
            try:
                from . import ui
            except ImportError:
                return (501, "text/plain; charset=utf-8",
                        b"UI module not loaded yet")
            return ui.handle(self.connect(), self.data_dir, req)
        return None

    # -- extraction worker -------------------------------------------------
    def start_worker(self, interval=10):
        from . import extract
        if self._worker and self._worker.is_alive():
            return
        stop = self._worker_stop

        def loop():
            while not stop.is_set():
                try:
                    con = _db.connect(self.data_dir)
                    try:
                        extract.drain_once(con, self.data_dir, limit=5)
                    finally:
                        con.close()
                except Exception:
                    pass  # worker never dies loudly; jobs record their own errors
                stop.wait(interval)

        self._worker = threading.Thread(target=loop, daemon=True, name="extract-worker")
        self._worker.start()

    def stop_worker(self):
        self._worker_stop.set()
