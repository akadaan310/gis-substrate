"""Applies the additive substrate mount patch to a copy of server.py.

Idempotent: running twice does not duplicate the patch. Keeps a .pre-substrate backup.
Usage: python3 server_patch.py /home/azureuser/upload-tool/server.py
"""
import os
import sys

MARKER = "# __SUBSTRATE_MOUNT__"
PATCH = '''
# __SUBSTRATE_MOUNT__ (additive: substrate UI+API under /s/ and /api/v1/)
_SUBSTRATE = None
def _substrate_app():
    global _SUBSTRATE
    if _SUBSTRATE is None:
        import sys as _sys, os as _os
        _SUBSTRATE_DIR = _os.environ.get("SUBSTRATE_DIR", "/home/azureuser/substrate")
        _DATA_DIR = _os.environ.get("SUBSTRATE_DATA_DIR", _SUBSTRATE_DIR)
        _TOKEN_FILE = _os.environ.get("SUBSTRATE_TOKEN_FILE",
                                      "/home/azureuser/upload-tool/.token")
        _sys.path.insert(0, _SUBSTRATE_DIR)
        from substrate.app import SubstrateApp
        _SUBSTRATE = SubstrateApp(_DATA_DIR, _TOKEN_FILE)
        _SUBSTRATE.start_worker()
    return _SUBSTRATE
'''

HOOK = '''
        # __SUBSTRATE_MOUNT__ hook: delegate /s/ and /api/v1/ before legacy routes
        if self.path == "/s" or self.path.startswith(("/s/", "/api/v1/")):
            try:
                app = _substrate_app()
                # Enforce request-body limit BEFORE reading: never buffer an
                # unbounded body into memory. Malformed/negative/absurd
                # Content-Length values are rejected without reading.
                _raw_len = self.headers.get("Content-Length", "0") or "0"
                try:
                    length = int(str(_raw_len).strip())
                except (ValueError, TypeError):
                    length = -1
                _MAX_BODY = 30 * 1024 * 1024
                if length < 0 or length > _MAX_BODY:
                    self.send_response(413)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Connection", "close")
                    _msg = b"request body too large or invalid Content-Length"
                    self.send_header("Content-Length", str(len(_msg)))
                    self.end_headers()
                    self.wfile.write(_msg)
                    self.close_connection = True
                    return
                body = self.rfile.read(length) if length > 0 else b""
                res = app.handle(self.command, self.path, dict(self.headers), body)
            except Exception as ex:  # never break legacy routes
                # Generic message to the caller; details go to stderr only.
                import sys as _sys, traceback as _tb
                _tb.print_exc(file=_sys.stderr)
                res = (500, "text/plain; charset=utf-8",
                       b"substrate error")
            if res is not None:
                # res is (code, ctype, body) or (code, ctype, body, headers)
                if len(res) == 4:
                    code, ctype, rbody, _xhdrs = res
                else:
                    code, ctype, rbody = res
                    _xhdrs = {}
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(rbody)))
                if isinstance(_xhdrs, dict):
                    for _hk, _hv in _xhdrs.items():
                        self.send_header(_hk, _hv)
                elif isinstance(_xhdrs, str) and _xhdrs:
                    # raw "Set-Cookie: ..." header line
                    _hn, _, _hvv = _xhdrs.partition(":")
                    if _hn.strip():
                        self.send_header(_hn.strip(), _hvv.strip())
                self.end_headers()
                self.wfile.write(rbody)
                return
'''


def _add_patch_method(src):
    """Add a do_PATCH method that delegates to the substrate mount.
    Used for already-patched files that predate PATCH support."""
    patch_method = '''
    def do_PATCH(self):
        # __SUBSTRATE_MOUNT__ hook: delegate /api/v1/ PATCH before legacy routes
        # (legacy server has no PATCH routes; 501 for non-substrate paths)
        if self.path.startswith("/api/v1/"):
            try:
                app = _substrate_app()
                _raw_len = self.headers.get("Content-Length", "0") or "0"
                try:
                    length = int(str(_raw_len).strip())
                except (ValueError, TypeError):
                    length = -1
                _MAX_BODY = 30 * 1024 * 1024
                if length < 0 or length > _MAX_BODY:
                    self.send_response(413)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                body = self.rfile.read(length) if length > 0 else b""
                res = app.handle("PATCH", self.path, dict(self.headers), body)
                if res is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    return
                code, ctype, rbody = res[0], res[1], res[2]
                xhdrs = res[3] if len(res) > 3 else None
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(rbody)))
                if isinstance(xhdrs, dict):
                    for _hk, _hv in xhdrs.items():
                        self.send_header(_hk, _hv)
                self.end_headers()
                self.wfile.write(rbody)
                return
            except Exception:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                return
        self.send_response(501)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
'''
    # Insert at end of Handler class (before "if __name__").
    anchor = 'if __name__ == "__main__":'
    if anchor in src:
        src = src.replace(anchor, patch_method + "\n" + anchor, 1)
    else:
        raise AssertionError("cannot find __main__ anchor for do_PATCH")
    return src


def main():
    target = sys.argv[1]
    with open(target) as f:
        src = f.read()
    if MARKER in src:
        # Already patched. Check if do_PATCH hook is present (added in
        # continuity milestone). If not, add it without re-patching.
        if "def do_PATCH(self):" not in src:
            src = _add_patch_method(src)
            with open(target, "w") as f:
                f.write(src)
            import ast
            ast.parse(src)
            print("added do_PATCH hook to %s" % target)
            return 0
        print("patch already applied")
        return 0
    backup = target + ".pre-substrate"
    with open(backup, "w") as f:
        f.write(src)
    # 1. module-level init after the INDEX = load_index() line
    anchor = "INDEX = load_index()\n"
    assert anchor in src, "anchor not found"
    src = src.replace(anchor, anchor + PATCH, 1)
    # 2. hook at the top of do_GET and do_POST
    for method in ("    def do_GET(self):\n", "    def do_POST(self):\n"):
        assert method in src, "method anchor missing: %r" % method
        src = src.replace(method, method + HOOK, 1)
    # 3. add do_PATCH method (does not exist in legacy server)
    src = _add_patch_method(src)
    with open(target, "w") as f:
        f.write(src)
    import ast
    ast.parse(src)
    print("patched %s (backup %s)" % (target, backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
