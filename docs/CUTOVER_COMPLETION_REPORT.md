# Substrate Production Cutover — Completion Report

**Date:** 2026-10-09 ~22:00 EDT (2026-10-10 ~02:00 UTC)
**Authorization:** Abed's explicit cutover directive
**Final status:** **LIVE AND VERIFIED**

---

## 1. Deployed commit and timestamp

- **Release:** `d655ad9` (code) + `4acc3ac` (cookie-session hotfix, 2 lines in `ui.py`)
- **Deployment:** 2026-10-09 ~21:55–22:05 EDT
- **Code location:** `/home/azureuser/substrate/` (verified byte-identical to `d655ad9` for all key files)
- **Data location:** `/home/azureuser/substrate-data/` (separate from code)
- **Patch:** `deploy/server_patch.py` applied to `/home/azureuser/upload-tool/server.py`
  - `.pre-substrate` backup: `a94bccfb7edc094ab4dbb1d64c1b2aea` (byte-identical to original)
  - 3 `__SUBSTRATE_MOUNT__` markers, Python syntax valid, hooks before legacy routes

## 2. Backup location and verification

- **Location:** `/home/azureuser/backup-cutover-20261009-2200/upload-tool/`
- **Verification:**
  - 43 files/dirs in recursive manifest (including hidden), lists identical
  - All file MD5 hashes match between source and backup
  - `.token` mode 600 in both
  - Sizes match (46M)
- **Token value never printed** in any log or report.

## 3. Staging results and production preflight

**Staging (all passed):**
- Release identity: `d655ad9` confirmed
- Concurrent writes: 10/10 simultaneous uploads, contents and hashes verified, 44 artifacts, integrity clean
- Patch → restart → smoke → rollback → restart: all verified on exact production copy
- Token rotation: cached secret confirmed; restart required; old 403/new 200 after restart
- 65 unit + 66 smoke tests green on exact release

**Production preflight:**
- Service: active, healthy
- `server.py` MD5: `a94bccfb7edc094ab4dbb1d64c1b2aea` (matched expected)
- Archive: 35 files (one new upload since staging verification)
- All 3 patch anchors present, not previously patched
- Disk: 64G free

## 4. Migration and integrity

- **Migration:** `indexed=35 errors=0 total_ids=35` (exit 0)
- **Integrity check:** 35 revisions, 0 orphan blobs, 0 orphan rows, 0 dangling legacy refs
- **Legacy files:** untouched (35 files, hashes unchanged)

## 5. Service and smoke-test results

| Test | Result |
|------|--------|
| Service active after restart | ✅ active |
| Legacy `/f/` URL bytes match | ✅ MD5 identical |
| Substrate health (Bearer) | ✅ 200 |
| Substrate UI (Bearer) | ✅ 200 |
| Unauthenticated `/api/v1/health` | ✅ 403 |
| Cookie session establish | ✅ 200 + Set-Cookie (after hotfix) |
| UI via cookie (no token) | ✅ 200 |
| Upload test artifact | ✅ `art_kbp4on0u63iv` |
| Download test artifact | ✅ content matches |
| Search | ✅ 4 hits for test marker |
| Wrong token | ✅ 403 |
| Export integrity (unit tests) | ✅ 5/5 pass |
| Integrity checker | ✅ clean |

## 6. Token rotation

- **Rotated:** Yes, during cutover window (secure random, 43 chars, mode 600)
- **Restart required:** Yes (confirmed — secret cached at init)
- **Old token:** Rejected after restart (403 verified with wrong token)
- **New token:** Works via Bearer and cookie
- **Value:** Never exposed in output, logs, or reports

## 7. Test artifacts created

- `art_kbp4on0u63iv` (`prod-smoke.txt`, 51 bytes) — clearly marked, safe to leave
- No legacy files modified or deleted

## 8. Remaining risks and deviations

**Deviations from plan:**
1. **Archive grew 34 → 35 files** during cutover (live upload). Migration handled it (35/35).
2. **Cookie-session bug found in production:** `SimpleCookie` import missing + `_shell()` tuple mishandling caused 500 on `/s/session?action=establish`. Fixed with 2-line change (`4acc3ac`), deployed, verified. This was a P6 feature that was never tested end-to-end before cutover.
3. **No Caddy changes** (per directive — deferred).
4. **No Tesseract install** (per directive — deferred; honest `unavailable` status preserved).

**Remaining risks:**
- Concurrent write contention under heavy load (tested with 10 parallel uploads in staging; production traffic patterns unknown)
- Disk-full behavior (untested)
- Caddy error-log Referer exposure persists (mitigated by rotation + cookie sessions; hardening deferred per directive)

## 9. Final status

**LIVE AND VERIFIED**

Substrate is live at `https://files.abedkadaan.com/s/` and `/api/v1/`.
All 35 legacy files preserved and serving. Rollback backup verified and ready
at `/home/azureuser/upload-tool/server.py.pre-substrate` if ever needed.
