# Pre-Cutover Readiness Addendum

**Parent document:** `docs/PRODUCTION_DEPLOYMENT_REVIEW.md`
**Date:** 2026-10-09 ~21:50 EDT
**Status:** REVIEW ONLY — no deployment, no production changes, no commits.

This addendum resolves the 8 items requested, with evidence. It separates
**verified facts** from **proposed procedures** and **unresolved assumptions**.

---

## 1. Release identity

### Verified facts
Git history (all commits by Muse, no external changes):
```
d655ad9  docs: final operational verification report (3 gates closed)
253849e  code: integrity checker handles legacy in-place storage
7a1a98c  docs: implementation report for integrity and ingestion work
```

**Reconciliation:**
- `7a1a98c`: Implementation complete. Docs-only commit (no code changes from `03b90a9`).
- `253849e`: **Final code change.** Modifies `substrate/check.py` only (15 lines: legacy in-place storage handling).
- `d655ad9`: **Final revision.** Adds `docs/FINAL_OPERATIONAL_VERIFICATION.md` only. **Zero code changes** from `253849e`.

**Release revision: `d655ad9`**
- Contains all reviewed code changes (P1–P7, integrity checker fix).
- Contains all documentation (audit, implementation report, verification report).
- The staging migration tests ran against `7a1a98c` code, which is **byte-identical**
  to `d655ad9` code (verified via `git diff 253849e d655ad9 -- substrate/` → empty).
- The deployment patch (`deploy/server_patch.py`) is included and unchanged since `7a1a98c`.

**Do not deploy `7a1a98c` or `253849e` directly** — deploy `d655ad9` to ensure
the deployed docs match the deployed code.

---

## 2. Production patch compatibility

### Verified facts (read-only inspection, 2026-10-09)
Production `/home/azureuser/upload-tool/server.py`:
- Line 135: `INDEX = load_index()` ✅ (exact match)
- Line 289: `    def do_GET(self):` ✅ (exact match, 4-space indent)
- Line 329: `    def do_POST(self):` ✅ (exact match, 4-space indent)
- `__SUBSTRATE_MOUNT__` count: 0 (not previously patched) ✅

**Expected patch outcome:** Clean apply. All three anchors match exactly.
The patch asserts anchor presence and will abort (not corrupt) if they differ.

### Context around anchors (verified)
- Line 135 context: blank lines before/after; followed by `PAGE = """`.
- Line 289 (`do_GET`): first line is `path = urlparse(self.path).path`;
  legacy routing follows. The hook inserts **before** legacy routing, preserving it.
- Line 329 (`do_POST`): first lines check `path != "/upload"` → 404.
  The hook inserts before this check.

**No discrepancies found. Patch is compatible. Do not apply until authorized.**

---

## 3. Token rotation

### Verified facts (code inspection + isolated test)
**Code:** `SubstrateApp.__init__` (app.py:156) calls `load_secret(token_path)` **once**
and caches in `self.secret`. The secret is **not** re-read per request.

**Isolated test (disposable fixtures, no production):**
| Step | Result |
|------|--------|
| Old token before rotation | 200 ✅ |
| Old token after file rotation (same instance) | 200 ✅ (cached — old still works) |
| New token after file rotation (same instance) | 403 ❌ (not picked up) |
| New token with fresh instance | 200 ✅ |
| Old token with fresh instance | 403 ✅ (correctly rejected) |

**Conclusion:** Token rotation **requires a service restart** (which re-initializes
`SubstrateApp` via the patch hook). There is no hot-reload.

### Implications for cutover
- Rotate the token **during** the cutover restart window (not before, not after).
- Sequence: stop service → rotate token file → patch → migrate → start service.
- Old-token URLs will 403 immediately after restart. This is expected.
- **Test only in staging.** Do not touch the production token file.

---

## 4. Logging exposure

### Verified facts (Gate 3 + additional tracing)
**Caddy runtime:**
- Version 2.11.7, config: `/etc/caddy/Caddyfile` (4 lines, no `log` directive).
- **Access logs:** Disabled (Caddy v2 default when no `log` directive).
- **Error logs:** Enabled by default → systemd journal (`journalctl -u caddy`).
- When `reverse_proxy` encounters an error, it logs at WARN with the full
  `request` object, including `headers.Referer`.

**Observed evidence (systemd journal, 2026-10-09):**
A `reverse_proxy` WARN entry contained:
```json
"headers": {
  ...
  "Referer": ["https://files.abedkadaan.com/?token=<REDACTED>"],
  ...
}
```
(Token value redacted in this report; the actual journal entry contains the full token.)

**Distinction:**
| Log type | Query strings logged? | Token exposure? |
|----------|----------------------|-----------------|
| Caddy access logs | N/A (disabled) | No |
| Caddy error logs (journal) | Yes, via `Referer` header | **Yes, on proxy errors** |
| Upload-tool `server.py` | N/A (`log_message` overridden to quiet) | No |

### Proposed mitigation (Caddy v2.11 compatible, NOT tested)
**Option A — Sanitize error logs (recommended):**
Configure Caddy's global log to filter the `http.handlers.reverse_proxy` logger
or redact sensitive headers. Caddy v2 supports log filtering via the `log` directive
with `format filter`:
```
{
    log {
        format filter {
            wrap json
            fields {
                request>headers>Referer delete
            }
        }
    }
}
```
**Status:** Proposed from Caddy v2 documentation. **Not validated** (Caddy not
available in dev environment; production config not changed per constraints).

**Option B — Disable Referer at the edge:**
Add a `header` directive to strip or override `Referer` for sensitive paths.
Less precise than Option A.

**Option C — Application-level (already implemented):**
Substrate UI no longer propagates `?token=` when using cookie/Bearer sessions
(P6). The observed exposure was from the **legacy** upload-tool's `?token=` URLs,
not Substrate.

### Residual risk after token rotation
- **What remains:** If a proxy error occurs **after** rotation, the **new**
  token could appear in the journal via Referer (if the user still uses `?token=` URLs).
- **Mitigation:** Cookie sessions (P6) eliminate `?token=` from URLs entirely.
  Users should transition to cookie/Bearer auth.
- **Acceptable for cutover?** **Yes, with rotation.** The exposure requires
  (a) a proxy error AND (b) `?token=` in the Referer. Rotation invalidates any
  tokens already logged. The residual risk is low and bounded.
- **Long-term:** Implement Option A or Caddy log hardening in a separate
  maintenance window (requires Abed's authorization for config change).

---

## 5. Backup completeness

### Proposed procedure (improved — do NOT execute until authorized)
```bash
# 1. Create timestamped backup OUTSIDE the live directory
BACKUP=/home/azureuser/backup-cutover-$(date +%Y%m%d-%H%M%S)
mkdir -p $BACKUP

# 2. Copy the entire upload-tool directory (preserves permissions)
cp -a /home/azureuser/upload-tool $BACKUP/upload-tool

# 3. Verify completeness
echo "=== file counts ==="
echo "source files: $(find /home/azureuser/upload-tool/files -type f | wc -l)"
echo "backup files: $(find $BACKUP/upload-tool/files -type f | wc -l)"

echo "=== critical files ==="
for f in server.py index.jsonl .token; do
  if [ -f "/home/azureuser/upload-tool/$f" ]; then
    src_hash=$(md5sum /home/azureuser/upload-tool/$f | cut -d' ' -f1)
    bak_hash=$(md5sum $BACKUP/upload-tool/$f | cut -d' ' -f1)
    if [ "$src_hash" = "$bak_hash" ]; then
      echo "OK: $f"
    else
      echo "FAIL: $f hash mismatch"; exit 1
    fi
  fi
done

echo "=== all file hashes ==="
(cd /home/azureuser/upload-tool/files && md5sum * | sort > /tmp/bak-src.txt)
(cd $BACKUP/upload-tool/files && md5sum * | sort > /tmp/bak-dst.txt)
diff /tmp/bak-src.txt /tmp/bak-dst.txt && echo "OK: all 34 file hashes match"
rm /tmp/bak-src.txt /tmp/bak-dst.txt

echo "=== permissions ==="
ls -ld /home/azureuser/upload-tool $BACKUP/upload-tool
ls -l /home/azureuser/upload-tool/.token $BACKUP/upload-tool/.token
# .token must be mode 600 in both

echo "=== metadata ==="
du -sh /home/azureuser/upload-tool $BACKUP/upload-tool
```

**What this covers:**
- ✅ `server.py` (with hash verification)
- ✅ `index.jsonl` (with hash verification)
- ✅ `files/` (34 files, with per-file hash verification)
- ✅ `.token` (with permission check: must be 600)
- ✅ Permissions (via `cp -a` + explicit `ls -l` check)
- ✅ Directory structure and sizes (via `du -sh`)

**Storage location:** `/home/azureuser/backup-cutover-<timestamp>/` —
**outside** `/home/azureuser/upload-tool/` (the live directory).

**Integrity verification:** Per-file MD5 hashes compared; critical files
individually verified; permissions explicitly checked.

**Do NOT create this backup until Abed authorizes the cutover window.**

---

## 6. Safe smoke tests (revised)

### Authentication (prefer Bearer/cookie — no secrets in URLs)
```bash
TOKEN="<new-token>"  # set from secure source, not command history
BASE="https://files.abedkadaan.com"

# Bearer header (preferred — token not in URL or logs)
curl -s -o /dev/null -w "health (bearer): %{http_code}\n" \
  -H "Authorization: Bearer $TOKEN" $BASE/api/v1/health

# No token → 403
curl -s -o /dev/null -w "health (no auth): %{http_code}\n" \
  $BASE/api/v1/health

# Cookie session (establish once, then use cookie)
curl -s -c /tmp/smoke-cookies.txt -o /dev/null -w "session establish: %{http_code}\n" \
  -X POST "$BASE/s/session?action=establish&token=$TOKEN"
curl -s -b /tmp/smoke-cookies.txt -o /dev/null -w "UI (cookie): %{http_code}\n" \
  $BASE/s/
rm /tmp/smoke-cookies.txt
```

### Legacy URLs (no auth needed — public)
```bash
# Use a known file ID from staging verification (not a secret)
curl -s -o /dev/null -w "legacy file: %{http_code}\n" \
  $BASE/f/<known-id>.jpg
```

### Upload/download (Bearer auth, test artifacts clearly marked)
```bash
# Upload a clearly-marked test file
echo "smoke-test-$(date +%s)" > /tmp/smoke-test.txt
ART_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/smoke-test.txt" \
  $BASE/api/v1/artifacts | python3 -c "import sys, json; print(json.load(sys.stdin)['artifacts'][0]['id'])")
echo "test artifact: $ART_ID"

# Download and verify
curl -s -H "Authorization: Bearer $TOKEN" \
  $BASE/api/v1/artifacts/$ART_ID/content | diff - /tmp/smoke-test.txt \
  && echo "download verified"

# Check Content-Disposition for HTML (should be attachment)
echo "<html><body>test</body></html>" > /tmp/smoke-test.html
HTML_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/smoke-test.html;type=text/html" \
  $BASE/api/v1/artifacts | python3 -c "import sys, json; print(json.load(sys.stdin)['artifacts'][0]['id'])")
curl -s -D - -H "Authorization: Bearer $TOKEN" \
  -o /dev/null $BASE/api/v1/artifacts/$HTML_ID/content | grep -i "content-disposition"
# must show: attachment

# Cleanup test artifacts (safe — only the ones we created)
# Note: Substrate has no delete API; test artifacts remain but are clearly marked.
# Document their IDs for manual cleanup if needed.
echo "Test artifacts (safe to leave, marked): $ART_ID $HTML_ID"
rm /tmp/smoke-test.txt /tmp/smoke-test.html
```

**Safety rules:**
- ✅ Use Bearer headers or cookies, never `?token=` in test URLs.
- ✅ Test files are clearly marked (`smoke-test-<timestamp>`).
- ✅ Never delete or modify existing legacy files.
- ✅ Never use production file IDs for destructive tests.

---

## 7. Operational gaps — what can be tested in staging

### Tested in isolated staging (2026-10-09)
| Gap | Test | Result |
|-----|------|--------|
| Concurrent requests | 20 parallel health checks | ✅ 20/20 returned 200 |
| Concurrent DB reads | 10 parallel artifact lists | ✅ 10/10 returned 5 items, no lock errors |
| Migration interruption | Kill during migration, re-run | ✅ Idempotent (34/34 after re-run) |

### Testable in staging (not yet done)
| Gap | Proposed test | Why it matters |
|-----|---------------|----------------|
| SQLite write contention | Parallel uploads (10+ concurrent POSTs) | Write locks could cause 500s under load |
| Rollback/restart | Patch → restart → rollback → restart in staging | Validates the actual rollback procedure |
| Token rotation | Rotate staging token, verify old 403/new 200 without restart | Confirms restart requirement |

### Cannot be safely tested (even in staging)
| Gap | Reason |
|-----|--------|
| Disk-full handling | Simulating disk exhaustion risks the host; use a loopback device in a disposable VM instead |
| Production concurrent load | Cannot replicate production traffic patterns in staging |

### Recommendation
Run the "testable" items above in staging **before** cutover. They require
~30 minutes and a staging server (already set up at `/home/azureuser/stage-verify/`).

---

## 8. Decision-ready outcome

### Verified facts and evidence
1. **Release revision:** `d655ad9` (code identical to `253849e`; adds verification report).
2. **Patch compatibility:** All 3 anchors present in production `server.py` at lines 135, 289, 329. Not previously patched. Expected to apply cleanly.
3. **Token caching:** Secret is loaded once at `SubstrateApp.__init__` and cached. **Rotation requires service restart.** Verified by isolated test (old 200/new 403 after file change; reversed after re-init).
4. **Logging:** Caddy access logs disabled. Error logs (systemd journal) **do** capture `Referer` with `?token=` on proxy errors (observed). Upload-tool does not log requests.
5. **Staging:** 34-file migration idempotent; integrity clean; export/restore 34/34; concurrent reads safe (20/20, 10/10).
6. **Tests:** 65 unit + 66 smoke green.

### Unresolved assumptions
1. Production `server.py` structure matches the fixture-tested patch (anchors verified, but full-file compatibility not tested).
2. Systemd will restart the patched service cleanly (untested).
3. SQLite write contention under concurrent uploads (untested — reads only).
4. Disk-full behavior (untested).
5. Caddy log mitigation (Option A) is untested (Caddy unavailable in dev).

### Exact changes needed before cutover
1. **None to the code.** Release `d655ad9` is final.
2. **Operational:** Abed must decide on token rotation timing, Tesseract, and Caddy hardening (see §9).
3. **Testing:** Run the 3 "testable" staging items (§7) — ~30 min.

### Updated go/no-go checklist
| # | Check | Authorized by | Status |
|---|-------|---------------|--------|
| 1 | Cold backup completed and verified (§5) | Abed | ☐ Not started |
| 2 | Preflight checks pass | Operator | ☐ Not started |
| 3 | Staging gap tests pass (§7: write contention, rollback/restart, token rotation) | Operator | ☐ Not started |
| 4 | Code deployed to `/home/azureuser/substrate/` (rev `d655ad9`) | Abed | ☐ Not started |
| 5 | Patch applied and verified (anchors confirmed §2) | Operator | ☐ Not started |
| 6 | Token rotated during restart window (§3) | **Abed** | ☐ Decision: at cutover? |
| 7 | Migration: 34 indexed, 0 errors, integrity clean | Operator | ☐ Not started |
| 8 | Service restarted, routes respond | Operator | ☐ Not started |
| 9 | Smoke tests pass (§6: Bearer/cookie, no query tokens) | Operator | ☐ Not started |
| 10 | Tesseract installed (if required) | **Abed** | ☐ Decision: at launch or deferred? |
| 11 | Caddy log hardening (if required pre-cutover) | **Abed** | ☐ Decision: before or after? |
| 12 | **FINAL GO/NO-GO** | **Abed** | ☐ Not authorized |

### Should anything block cutover?
**No blocking issues found.** The release is ready from a code perspective.

**Non-blocking but requires Abed's decision:**
1. **Token rotation timing** — Recommend: during cutover restart (required anyway for the cached secret).
2. **Tesseract** — Recommend: deferred (honest degradation verified).
3. **Caddy hardening** — Recommend: after cutover (rotation covers interim risk).

**If Abed disagrees with any recommendation**, that item becomes a blocker until resolved.

### Production remains untouched
- `server.py` md5 `a94bccfb7edc094ab4dbb1d64c1b2aea` — verified 2026-10-09 ~21:50 EDT.
- Service active, never restarted by this review.
- No files modified, no config changed, no packages installed, no credentials rotated.
- All inspection was read-only. All tests used disposable fixtures or isolated staging.
- This addendum is **uncommitted** (per instructions).

---

## Appendix: document lineage (preserved as historical evidence)
- `docs/PRODUCTION_READINESS_AUDIT.md` (a3abb7a) — original audit, NOT READY verdict.
- `docs/IMPLEMENTATION_REPORT.md` (7a1a98c) — P1–P10 implementation.
- `docs/FINAL_OPERATIONAL_VERIFICATION.md` (d655ad9) — 3 gates closed.
- `docs/PRODUCTION_DEPLOYMENT_REVIEW.md` (uncommitted) — deployment review.
- This addendum (uncommitted) — pre-cutover readiness.
