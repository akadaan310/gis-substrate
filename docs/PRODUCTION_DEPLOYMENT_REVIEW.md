# Production Deployment Review — Substrate

**Status:** REVIEW DOCUMENT — not a deployment authorization.
**Prepared:** 2026-10-09 ~21:45 EDT
**Based on:** Original audit `a3abb7a`, implementation `7a1a98c`,
operational verification `d655ad9`.
**Production state at review time:**
- `server.py` md5: `a94bccfb7edc094ab4dbb1d64c1b2aea` (unpatched, no Substrate)
- Archive: 34 files in `/home/azureuser/upload-tool/files/`
- Service `upload-tool`: active
- No Substrate code, database, or configuration in production.

**What this document is:** A pre-deployment review proposing the cutover
sequence, rollback plan, and go/no-go criteria. Every section separates
**verified facts** (tested evidence) from **proposed procedures** (untested
in production) and **open decisions** (requiring Abed's authorization).

**What this document is not:** It does not authorize deployment. No step
herein has been executed against production.

---

## 1. Preflight checks and cold backup

### Verified facts
- Production `server.py` is unpatched (md5 confirmed 2026-10-09).
- Production archive has 34 files; staging migration of an identical 34-file
  copy succeeded with 0 errors and passed integrity checks.
- The deployment patch is idempotent and keeps a `.pre-substrate` backup
  (tested on a disposable fixture — **not** on the production `server.py`).
- Rollback via `.pre-substrate` restore was verified byte-identical on a
  fixture — **not** on production.

### Required preflight checks (proposed — run immediately before cutover)
1. Confirm production `server.py` md5 matches `a94bccfb...` (no drift).
2. Confirm `upload-tool` service is active and serving (spot-check a legacy `/f/` URL).
3. Confirm 34 files in `files/` and `index.jsonl` readable.
4. Confirm no `.pre-substrate` file exists (would indicate a prior partial patch).
5. Confirm staging verification revision (`d655ad9`) matches the code to be deployed.
6. Confirm disk space: at least 2× the archive size free (for DB + blob copies + export).

### Required cold backup (proposed)
Before any cutover step, take a complete cold backup:
```bash
BACKUP=/home/azureuser/backup-substrate-cutover-$(date +%Y%m%d-%H%M%S)
mkdir -p $BACKUP
cp -a /home/azureuser/upload-tool $BACKUP/upload-tool
# verify
diff -r /home/azureuser/upload-tool/files $BACKUP/upload-tool/files && echo "backup verified"
md5sum /home/azureuser/upload-tool/server.py
md5sum $BACKUP/upload-tool/server.py  # must match
```
**Do not proceed** if the backup verification fails.

---

## 2. Staged cutover sequence with validation checkpoints

### Proposed sequence
Each stage has an explicit checkpoint. **Stop at any failed checkpoint.**

**Stage 0 — Freeze (checkpoint: no writes during cutover)**
- Announce the maintenance window. No uploads during cutover.
- Verify no active uploads in progress (check service logs if available).

**Stage 1 — Deploy code (checkpoint: files in place, untouched config)**
1. Copy substrate code to `/home/azureuser/substrate/` (from verified revision `d655ad9`).
2. Verify: `ls /home/azureuser/substrate/substrate/app.py` exists.
3. **Do not** modify any production configuration yet.

**Stage 2 — Patch server.py (checkpoint: patch applied cleanly)**
1. Run `python3 /home/azureuser/substrate/deploy/server_patch.py /home/azureuser/upload-tool/server.py`.
2. Verify: output says "patched"; `.pre-substrate` backup exists.
3. Verify: `python3 -c "import ast; ast.parse(open('/home/azureuser/upload-tool/server.py').read())"` passes.
4. Verify: `grep -c "__SUBSTRATE_MOUNT__" server.py` shows the marker (exactly 2: init + hook).
5. **Checkpoint:** If any verification fails, restore `.pre-substrate` immediately and abort.

**Stage 3 — Migrate (checkpoint: 34 artifacts, 0 errors, integrity clean)**
1. Set environment: `SUBSTRATE_DIR=/home/azureuser/substrate`, `SUBSTRATE_DATA_DIR=/home/azureuser/substrate` (or separate data dir).
2. Run migration: `python3 -m substrate.migrate --legacy-dir /home/azureuser/upload-tool/files --index /home/azureuser/upload-tool/index.jsonl --data-dir <DATA_DIR>`.
3. Verify: output `indexed=34 errors=0`.
4. Run integrity check: `python3 -m substrate.check --data-dir <DATA_DIR>` → "all checks passed".
5. **Checkpoint:** If errors > 0 or integrity fails, do NOT restart the service. Investigate.

**Stage 4 — Restart service (checkpoint: both old and new routes respond)**
1. Restart `upload-tool`: `sudo systemctl restart upload-tool`.
2. Verify service is active: `systemctl is-active upload-tool`.
3. Verify legacy route: `curl -s -o /dev/null -w "%{http_code}" https://files.abedkadaan.com/f/<known-id>` → 200.
4. Verify Substrate health: `curl -s https://files.abedkadaan.com/api/v1/health?token=<token>` → 200.
5. Verify Substrate UI: `curl -s -o /dev/null -w "%{http_code}" "https://files.abedkadaan.com/s/?token=<token>"` → 200.
6. **Checkpoint:** If legacy routes fail, rollback immediately (Stage R).

**Stage 5 — Post-cutover validation (see §7 for full smoke tests)**
- Run the complete smoke test suite before declaring the cutover complete.

### Untested assumptions (explicit)
- The patch has **not** been applied to the actual production `server.py`
  (only to fixtures). Anchor strings (`INDEX = load_index()`, `do_GET`,
  `do_POST`) are assumed present but not verified against the live file.
- Service restart behavior with the patched file is untested.
- Concurrent legacy + Substrate traffic is untested.

---

## 3. Rollback procedure

### Rollback triggers (any one triggers immediate rollback)
1. Legacy `/f/` URLs return non-200 after restart.
2. Service fails to start after patch.
3. Migration reports errors > 0.
4. Integrity check fails post-migration.
5. Substrate routes return 500 on health check.
6. Any data loss detected (file count mismatch, hash mismatch).

### Proposed rollback procedure
```bash
# 1. Stop the service
sudo systemctl stop upload-tool

# 2. Restore the pre-patch server.py
cp /home/azureuser/upload-tool/server.py.pre-substrate \
   /home/azureuser/upload-tool/server.py

# 3. Verify restoration
md5sum /home/azureuser/upload-tool/server.py
# must match a94bccfb7edc094ab4dbb1d64c1b2aea

# 4. Restart the service
sudo systemctl start upload-tool

# 5. Verify legacy routes
curl -s -o /dev/null -w "%{http_code}" https://files.abedkadaan.com/f/<known-id>
# must be 200
```

### Legacy recoverability — evidence vs. proposal
- **Verified (fixture):** Restoring `.pre-substrate` yields byte-identical
  `server.py` that parses cleanly. The patch is purely additive; no legacy
  code paths are modified.
- **NOT verified:** Rollback on the actual production server, or service
  restart after rollback. The procedure above is **proposed**, not tested.
- **Substrate data:** If rollback occurs after migration, the Substrate DB
  (`substrate.db`) and any copied blobs remain on disk but are inert — the
  unpatched `server.py` does not reference them. They can be deleted or kept
  for forensic analysis. Legacy files are never modified by migration
  (verified: read-only source, in-place indexing).

### Rollback verification (proposed)
After rollback, confirm:
1. `server.py` md5 matches pre-cutover value.
2. Service active and legacy URLs return 200.
3. No Substrate routes respond (404 or connection refused on `/s/`, `/api/v1/`).

---

## 4. Owner-token rotation and session management

### Current state (verified facts)
- Production uses a single owner token at `/home/azureuser/upload-tool/.token` (mode 600).
- Substrate accepts the token via `?token=`, `Authorization: Bearer`, or `substrate_token` cookie.
- Caddy error logs can capture `?token=` via the `Referer` header (Gate 3 finding).
- The new code supports cookie sessions (`POST /s/session?action=establish` sets `HttpOnly; SameSite=Lax`).

### Proposed rotation procedure
1. **After** successful cutover and smoke tests, generate a new token:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /home/azureuser/upload-tool/.token.new
   chmod 600 /home/azureuser/upload-tool/.token.new
   mv /home/azureuser/upload-tool/.token.new /home/azureuser/upload-tool/.token
   ```
2. **Do not** restart the service for rotation — the Substrate app reads the
   token file on each request (verify this behavior in staging first).
   - **Untested assumption:** Token file re-read frequency. If the app caches
     the token at startup, a restart is required. **Verify before relying.**
3. Establish a cookie session via the new endpoint; confirm subsequent
   navigation works without `?token=`.
4. **Retire old query-token URLs:** After rotation, any bookmarked `?token=`
   URLs with the old token will return 403. This is expected and desired.
   Notify Abed before rotation so he updates his bookmarks.

### Open decision (requires Abed's authorization)
- **When to rotate:** Immediately post-cutover, or defer to a later window?
  Recommendation: rotate during the cutover window while the service is
  already being restarted, to minimize the window where the old token (which
  may be in Caddy error logs) remains valid.

---

## 5. Caddy error-log / Referer exposure

### Verified facts (Gate 3)
- Caddy v2.11.7, no `log` directive → **no access logs**. Normal requests
  with `?token=` are not routinely logged.
- When a `reverse_proxy` error occurs, Caddy logs the full request to the
  systemd journal, including all headers. A real example was observed with
  `Referer: https://files.abedkadaan.com/?token=<redacted>`.
- The upload-tool `server.py` does not log requests (`log_message` overridden).

### Risk
Tokens can persist in the systemd journal via error-triggered Referer logging.
The journal persists across reboots and may be included in diagnostics.

### Recommended mitigation
1. **Before cutover:** No config change (per constraints).
2. **At cutover:** Rotate the owner token (see §4). This invalidates any
   tokens already in the journal.
3. **Post-cutover (separate change, requires authorization):** Add a Caddy
   `log` directive with a format that excludes query strings, or configure
   `reverse_proxy` error handling to redact sensitive headers. Example
   (proposed, not tested):
   ```
   files.abedkadaan.com {
       log {
           format json
           # custom format excluding uri_query
       }
       reverse_proxy 127.0.0.1:8123
   }
   ```

### Decision required (Abed)
**Must the Caddy log mitigation be resolved before cutover?**
- **Recommendation: No.** The exposure is limited to error conditions (not
  every request), and token rotation at cutover invalidates any logged tokens.
  The P6 cookie-session support further reduces `?token=` usage going forward.
- **Counter-consideration:** If the threat model includes journal access by
  unauthorized parties, harden Caddy logging before cutover.
- **Abed's decision needed:** Proceed with rotation-only, or harden Caddy first?

---

## 6. Tesseract requirement

### Verified facts
- Tesseract 5.3.4 is installed in the **dev environment only**.
- **Not installed in production.**
- Without Tesseract: PDF image-only pages and image OCR return honest
  `unavailable` status (verified). No fake text is produced. Embedded-text
  PDFs, DOCX, text, and ZIP extraction work without Tesseract.

### Decision required (Abed)
**Is OCR required at launch?**

**Option A — Required at launch:**
- During the deployment window (before Stage 4 restart):
  ```bash
  sudo apt-get update && sudo apt-get install -y tesseract-ocr tesseract-ocr-eng
  tesseract --version  # must show 5.x
  ```
- Verify: upload a test image to staging, confirm OCR job completes.
- Risk: `apt-get` may be slow (observed 20+ min on dev VM). Allow time.

**Option B — Deferred:**
- Launch without Tesseract. Image/PDF-OCR jobs will show `unavailable`
  with a clear message. Install later during a maintenance window.
- No data loss: original files are preserved; extraction can be retried
  after Tesseract is installed.

**Recommendation:** Option B (deferred) unless Abed has image-heavy documents
requiring immediate OCR. The system degrades honestly, and the 34-file archive
can be assessed for OCR need post-launch.

---

## 7. Post-cutover smoke tests

Run these **before** declaring cutover complete. All use the **new** token if rotated.

### Authentication
- [ ] `GET /api/v1/health?token=<token>` → 200
- [ ] `GET /api/v1/health` (no token) → 403
- [ ] `GET /api/v1/health` with `Authorization: Bearer <token>` → 200
- [ ] `GET /s/?token=<token>` → 200 HTML
- [ ] `GET /s/` (no token) → 403

### Legacy file URLs
- [ ] `GET /f/<known-image-id>` → 200, correct Content-Type, bytes match staging hash
- [ ] `GET /f/<known-doc-id>` → 200
- [ ] Legacy upload still works (if applicable): `POST /` with test file → 200

### Upload/download (Substrate)
- [ ] `POST /api/v1/artifacts?token=<token>` (multipart, small text file) → 201
- [ ] `GET /api/v1/artifacts/<id>/content?token=<token>` → 200, bytes match upload
- [ ] `GET /api/v1/artifacts/<id>/content?token=<token>` for HTML file → `Content-Disposition: attachment`

### Extraction
- [ ] Uploaded text file: extract job → `done`, chunks present
- [ ] Check `GET /api/v1/artifacts/<id>` shows extraction status

### Search
- [ ] `GET /api/v1/search?q=<term>&token=<token>` returns the uploaded file
- [ ] Search result includes `provenance` with extraction info

### Export/restore
- [ ] `POST /api/v1/export?token=<token>` (or CLI) → bundle created
- [ ] Verify bundle structure (if CLI available)

### Integrity checker
- [ ] `python3 -m substrate.check --data-dir <DATA_DIR>` → "all checks passed"

### Cookie session
- [ ] `POST /s/session?action=establish&token=<token>` → 200, `Set-Cookie` header present
- [ ] `GET /s/` with cookie (no `?token=`) → 200

---

## 8. Monitoring, recovery, and outstanding risks

### Monitoring (proposed — not implemented)
- Service liveness: `systemctl is-active upload-tool` (existing).
- Substrate health: periodic `GET /api/v1/health` (new).
- Extraction queue depth: `SELECT COUNT(*) FROM extract_job WHERE status='pending'` (new).
- Disk space: alert at 80% full (new).
- Integrity: weekly `python3 -m substrate.check` via cron (new).

### Recovery expectations
| Scenario | Recovery | Evidence |
|----------|----------|----------|
| Service crash | systemd restart (existing) | Untested with patched file |
| Patched file corrupt | Rollback via `.pre-substrate` | Tested on fixture only |
| DB corruption | Restore from export bundle | Export/restore tested; no tested DB-corruption scenario |
| Blob loss (non-legacy) | Restore from export bundle | Bundle verification tested |
| Blob loss (legacy) | Source files are the blobs; re-migrate | Migration idempotency tested |
| Disk full | **Unknown** — error paths exist but untested | **Untested** |

### Outstanding risks (untested)
1. **Concurrent load:** SQLite WAL with worker + request threads not stress-tested.
   Risk: lock contention under heavy concurrent uploads.
2. **Disk-full behavior:** What happens when the disk fills during migration,
   blob write, or extraction? Error paths exist but were not exercised.
3. **Token file re-read:** Unverified whether the app re-reads `.token` without restart.
4. **Patch anchor drift:** If production `server.py` differs from the assumed
   structure, the patch will fail (safe — it asserts anchors) but cutover stalls.

---

## 9. Go/no-go checklist

| # | Check | Authorized by | Status |
|---|-------|---------------|--------|
| 1 | Cold backup completed and verified | Abed | �square Not started |
| 2 | Preflight checks pass (md5, service, files, no prior patch) | Operator | ☐ Not started |
| 3 | Code deployed to `/home/azureuser/substrate/` (rev `d655ad9`) | Abed | ☐ Not started |
| 4 | Patch applied and verified (marker, backup, syntax) | Operator | ☐ Not started |
| 5 | Migration: 34 indexed, 0 errors, integrity clean | Operator | ☐ Not started |
| 6 | Service restarted, legacy + Substrate routes respond | Operator | ☐ Not started |
| 7 | Smoke tests pass (§7) | Operator | ☐ Not started |
| 8 | Token rotation (if approved) | **Abed** | ☐ Decision needed |
| 9 | Tesseract install (if required at launch) | **Abed** | ☐ Decision needed |
| 10 | Caddy log hardening (if required pre-cutover) | **Abed** | ☐ Decision needed |
| 11 | **FINAL GO/NO-GO** | **Abed** | ☐ Not authorized |

### Authorization rules
- **Abed must authorize:** Cold backup (irreversible if skipped), token rotation
  (invalidates existing URLs), Tesseract install (production package change),
  Caddy config change (production config), and the final go/no-go.
- **Operator may execute:** Code copy, patch application, migration, service
  restart, smoke tests — **only** after Abed authorizes the cutover window.
- **Rollback:** Operator may execute rollback unilaterally if any trigger (§3)
  fires during cutover. Abed must be notified immediately.

---

## Decisions requiring Abed's authorization

1. **Cutover window:** When? (Propose a low-traffic time; Abed sets it.)
2. **Token rotation:** At cutover or deferred? (Recommendation: at cutover.)
3. **Tesseract:** Required at launch or deferred? (Recommendation: deferred unless image-heavy.)
4. **Caddy hardening:** Before cutover or after? (Recommendation: after, with rotation covering the interim.)
5. **Final go/no-go:** After reviewing this document and the verification reports.

---

## Appendix: verified facts vs. proposals

**Verified by testing:**
- Patch idempotency and byte-identical rollback (fixture).
- Migration idempotency, 0 errors on 34 files (staging).
- Export/restore completeness and structure verification (staging + unit tests).
- Request-body limits (unit tests).
- OCR pipeline end-to-end (dev, Tesseract 5.3.4).
- Integrity checker on 34-file staging (all checks passed).
- 65 unit + 66 smoke tests green.
- Caddy has no access logs; error logs can include Referer with token.

**Proposed but not tested in production:**
- The entire cutover sequence (§2).
- The rollback procedure (§3).
- Service restart with patched file.
- Token rotation procedure.
- Tesseract installation in production.
- Caddy log format changes.

**Untested assumptions:**
- Production `server.py` contains the expected patch anchors.
- The app re-reads the token file without restart.
- Systemd will restart the patched service cleanly.
- Concurrent load will not cause SQLite lock issues.
- Disk-full scenarios will fail gracefully.
