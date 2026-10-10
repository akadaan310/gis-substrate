# Production Readiness Audit — Substrate

**Audited revision:** `15d85140f56a5be2e3090183e0bbe0b2983565a9` (tree clean, 6 commits)
**Audit date:** 2026-10-09 ~20:50–21:40 EDT
**Auditor:** Muse (implementation owner; audit conducted as a separate pass, no code changed)
**Scope:** the Substrate implementation at `~/workspace/substrate/` — modules, deployment
patch, tests, docs, and the VM staging setup. Production was inspected read-only;
nothing in production was modified, restarted, or migrated during this audit.

---

## 1. Verdict

**NOT READY — blocking issues found.** Two defects in export/restore cause silent
data loss / fidelity degradation. Both are fixable without architecture changes,
and the fixes are small (recommended below, not implemented per audit rules).
Once fixed and re-verified, the remaining items are non-blocking.

No evidence was found of data corruption in the migration path, authentication
bypass, or archive mutation. The core storage model (content-addressed blobs,
immutable revisions, idempotent migration) verified sound.

---

## 2. Evidence matrix

| # | Finding | Risk | Status | Evidence | Consequence | Required action | Blocks deploy |
|---|---------|------|--------|----------|-------------|-----------------|---------------|
| 1 | Unscoped export silently drops artifacts | **HIGH** | Confirmed defect | Fixture test: 2 artifacts (one in a collection, one edge-linked only) → unscoped `write_bundle` exported 1 artifact but **all** edges; restored DB contains an edge pointing at a non-existent artifact. Root cause: `build_export` collects `art_ids` from collection members, then `if art_ids: dump WHERE id IN art_ids` — unreferenced artifacts are dropped while `edge` is dumped unconditionally in unscoped mode. Staging 28/28 passed only because no collections existed (empty `art_ids` → dump-all branch). | Silent data loss on backup; dangling edges on restore; `verify_bundle` does not catch it (it only checks blob hashes for exported rows). | Smallest fix: in unscoped mode (`workspace_id is None`), always dump the full `artifact` table; alternatively, expand `art_ids` to include every artifact referenced by any exported edge. Add a restore-time referential check (every edge endpoint exists). | **YES** |
| 2 | Restore drops extracted text with no re-extraction | **HIGH** | Confirmed defect | `build_export` sets `extract_job=[]`, `extracted_chunk=[]`; `restore_bundle` rebuilds FTS from filenames/titles/event text only ("re-extract later" comment) but creates no jobs, so nothing ever re-extracts. Fixture: pre-restore FTS had chunk content; post-restore FTS has metadata only. | Restored instance silently loses all searchable extracted content (document text, code, transcripts). Docs claim "FTS rebuilt" — true but misleading. | Smallest fix: on restore, enqueue `pending` extract jobs for every restored artifact (or export the chunks). Document the actual behavior until fixed. | **YES** |
| 3 | No request-body size enforcement on substrate paths | **MEDIUM** | Confirmed gap | `MAX_BODY = 30MB` defined in `app.py` but never referenced anywhere; the mount patch does `self.rfile.read(length)` for the full `Content-Length`; `POST /api/v1/artifacts` has no per-file cap (legacy path caps at 25MB/file). | Owner (or anyone with the token) can exhaust server memory with a single large upload. Threat is limited to token holders, but it is a crash vector. | Enforce `MAX_BODY` in the patch hook (413 on exceed) and/or cap artifact upload size in `_art_upload`. | No, but fix before/with cutover |
| 4 | Owner token in URL query params lands in logs | **MEDIUM** | Confirmed design risk | `extract_token` accepts `?token=`; UI propagates it in every nav link and form action. Any HTTP access log (server, Caddy) records the token in cleartext. | Token exposure via log files, log shipping, or shoulder-surfing URLs. The token is the entire auth model. | Prefer cookie/Bearer in UI; at minimum document log handling. Long-term: rotate the token post-cutover. | No |
| 5 | 500 handler can leak exception text to unauthenticated callers | **LOW** | Confirmed risk | Patch hook: `except Exception as ex: res = (500, ... ("substrate error: %s" % ex))`. The hook runs before auth; `app.handle` raising (e.g., DB error, missing token file) exposes `str(ex)`, which may contain filesystem paths or SQL. | Minor information disclosure (paths, not secrets). | Return a generic 500 to unauthenticated callers; log the detail server-side. | No |
| 6 | Content-Disposition not settable through mount contract | **LOW** | Confirmed limitation (documented in code) | `_art_content` notes the contract has no header channel; non-safe types (HTML/SVG) are served inline with their content type. | Self-XSS at worst (owner-only route, token required). No cross-user impact under the single-token model. | Document; consider a `?download=1` convention or header channel later. | No |
| 7 | No DB↔blob consistency check command | **LOW** | Confirmed gap | No function walks `blob` rows vs `blobs/` files vs `artifact.blob_sha256` references. `verify_bundle` only covers exports. | Silent bit-rot or partial deletes go undetected. | Add a `substrate.check` / `migrate --check` command. | No |
| 8 | Staging is stale relative to production | **INFO** | Confirmed | Production `files/` now has 34 files; staging DB has 28 (copied earlier). 6 new uploads since. | Staging verification covered the copy-time state, not current production. Migration is idempotent, so re-running at cutover picks up the delta — but the delta itself was never migration-tested. | Re-run migration on a fresh copy (or accept: idempotency proven, delta is 6 ordinary files). | No |
| 9 | CSRF via `substrate_token` cookie | **LOW** | Verified minimal | Server never *sets* the cookie; it only reads one if the user's browser has it. CSRF requires the owner to have manually set the cookie and then visit a malicious site. Primary auth (`?token=`, Bearer) is not auto-sent cross-origin. | Low practical risk. | Document; consider `SameSite` guidance if cookie use is encouraged. | No |

### Verified sound (no action required)

- **AuthN:** `hmac.compare_digest` token check; global 403 gates on all `/api/v1/*`
  and all `/s/*` except public-by-token share pages; denied attempts audited
  without secrets. Share tokens: `secrets.token_urlsafe(32)`, SHA-256 hash
  stored, raw token only in the creation response, never in audit logs;
  expiry + revocation enforced and verified (200 → revoke → 404).
- **No path traversal:** blob paths derive from 64-hex SHAs; `source_ref` is only
  set by migration (system), never from API input; API artifact views drop
  `source_ref`; context provenance uses `legacy_id=` (regression-tested).
- **Input validation:** visibility/enum allow-lists, 400s on bad input, 404s on
  unknown ids, pagination caps (50/500), export refuses >200k events unscoped.
- **ZIP handling:** entry listing only, never extracts → no zip-bomb.
  Images: PIL default `MAX_IMAGE_PIXELS` applies; corrupt images fail the job,
  artifact bytes untouched (verified).
- **Escaping:** UI escapes user-controlled and extracted content (`E()`); FTS
  `<b>` markers preserved by design, rest escaped; injection test in suite.
- **Inferred vs explicit:** `traverse` returns `inferred` + `provenance` per edge;
  UI badges inferred edges; verified programmatically.
- **Migration:** idempotent (re-run: 0 new rows), transactional per file,
  `--dry-run` works, partial-migration recovery verified (4/10 → 10/10),
  byte-identical (md5sum clean), legacy URLs unaffected.
- **Dedup:** identical bytes → one blob row, distinct artifact records
  (provenance never merged) — verified.
- **Revisions:** immutable, parent-chained, old bytes preserved — verified.
- **Failed extraction:** job `failed` with `last_error`, artifact bytes intact,
  retry resets to `pending` (no data mutation) — verified.
- **Malformed bundles:** garbage manifest → error; missing blob → detected,
  restore refused; tampered blob → hash mismatch detected, restore refused;
  non-empty restore target → refused; wrong format marker → `format_ok=False`.
- **Rollback:** patch is idempotent (marker-guarded), keeps `.pre-substrate`
  backup; rollback restores byte-identical original (verified on fixture).
- **Tests:** 32/32 pass in isolation (no production/staging paths referenced;
  single comment mention only).

---

## 3. Reproduction record

All commands run against disposable fixtures or read-only production inspection.
No application code, tests, or config were changed (only this report is new).

**Baseline**
- `git log --oneline` → HEAD `15d8514`; `git status --short` → clean.
- VM: `md5sum /home/azureuser/upload-tool/server.py` →
  `a94bccfb7edc094ab4dbb1d64c1b2aea` (unchanged pre/post audit);
  no `*.pre-substrate` in production; `systemctl is-active upload-tool` → active;
  production `files/` count 34 (staging copy had 28).

**Tests**
- `python3 -m unittest tests.test_core tests.test_acceptance` → **32 ran, 32 OK**
  (1.5s). Isolation checked: no test references production/staging paths.

**Archive integrity (disposable fixtures, `/tmp/audit-*`)**
- Dedup: 2 identical 100KB uploads → 1 blob row, 2 artifact records, bytes identical.
- Revisions: v1/v2 bytes preserved, parent chain intact.
- Failed extraction (corrupt JPEG): job `failed` + error recorded, bytes intact.
- Retry: status reset to `pending`, audited, no data mutation.

**Recovery (disposable fixtures)**
- Rich fixture (workspace, session, 2 events, 2 artifacts + revision, 2 edges,
  decision, task, claim+evidence, collection+member, extracted text):
  `write_bundle` → `verify_bundle` (format_ok, 2/2 blobs) → `restore_bundle`
  to empty dir → **artifact 2→1 MISMATCH** (finding #1); edges 2→2 (dangling);
  blobs 2/2 byte-verified; ids stable for exported rows; FTS metadata-only
  (finding #2).
- Malformed bundles: garbage JSON → `JSONDecodeError`; missing blob →
  `blobs_missing=1`, restore refused; tampered blob → `blobs_bad=1`, restore
  refused; non-empty target → refused; wrong format → `format_ok=False`.
- Migration: partial (4/10) → full run → 10/10, 0 errors; re-run idempotent;
  `--dry-run` reports correctly.

**Rollback (disposable fixture)**
- `deploy/server_patch.py` on a minimal anchor-compatible `server.py`:
  patched, marker-guarded re-run prints "patch already applied",
  `.pre-substrate` backup kept, restoring it yields byte-identical original
  that parses clean.

**Security (code inspection + targeted probes)**
- Auth gates, token comparison, share lifecycle, audit logging: inspected
  (`app.py`, `api.py`, `ui.py`, `model.py`); share create→page→revoke→404
  exercised earlier on staging.
- `MAX_BODY` grep: defined once, referenced nowhere → finding #3.
- Inferred/explicit: programmatic traverse check → flags correct.

**Not run during this audit** (per instructions): resource-intensive benchmarks;
  any production migration; any service restart.

---

## 4. Security findings

**Confirmed issues:** #3 (no body-size enforcement), #4 (token-in-URL logging),
#5 (verbose 500 to unauthenticated callers), #6 (inline content disposition —
documented limitation). None is an authentication bypass or data-exposure
vulnerability under the single-owner-token threat model; #3 is the most
operationally serious (memory exhaustion).

**Unverified risks (need investigation, not confirmed):**
- Whether Caddy access logs currently record `?token=` URLs (depends on log
  format configuration — not inspected; no config changes permitted).
- Behavior under concurrent large uploads (worker thread + request threads
  share SQLite; WAL mode assumed — not load-tested).
- Disk-full behavior during migration or blob writes (error paths exist but
  were not exercised against a full disk).

---

## 5. Data-integrity and recovery findings

**Proven:**
- Migration is byte-identical, idempotent, transactional, and recoverable
  from interruption; legacy files and URLs untouched.
- Dedup never merges provenance; revisions are immutable and parent-chained.
- Failed extraction cannot damage artifacts; retry is safe.
- Bundle integrity checking works (missing/tampered blobs detected, restores
  refused appropriately, non-empty targets refused).
- Patch rollback restores byte-identical `server.py`.

**Not proven / disproven:**
- **Disproven:** unscoped export completeness (finding #1) and restore fidelity
  of extracted content (finding #2).
- **Unproven:** service-level disaster recovery (only bundle restore tested —
  no runbook for DB corruption, disk failure, or point-in-time recovery);
  extraction worker behavior at production data volumes; end-to-end behavior
  through the Caddy reverse proxy (staging was tested direct to the app port).

---

## 6. Deployment prerequisites (ordered, for a future separately-authorized decision)

1. Fix finding #1 (unscoped export artifact coverage) and add a restore-time
   referential check; re-run the rich-fixture export/restore test to PASS.
2. Fix finding #2 (restore extracted text or enqueue re-extraction); re-verify
   FTS parity pre/post restore.
3. Enforce request-body limits (#3) in the patch hook and/or `_art_upload`.
4. Re-run the full 32-test suite; re-run migration against a **fresh** copy of
   production data (staging is stale: 28 vs 34 files).
5. Rotate the owner token post-cutover (mitigates #4 for any tokens already in logs).
6. Take a cold backup of `/home/azureuser/upload-tool/` (files + index.jsonl +
   server.py) before cutover.
7. Cutover during a low-traffic window: copy code → patch (verify marker +
   backup) → syntax check → migrate → restart → smoke-test legacy routes +
   `/s/` + `/api/v1/health`.
8. Keep the `.pre-substrate` backup until the first successful backup cycle
   completes on the new system.

---

## 7. Deferred work (safe to postpone)

- `Content-Disposition: attachment` for non-safe types (needs header channel
  in the mount contract) — finding #6.
- Generic 500s for unauthenticated callers — finding #5.
- `DB↔blob` consistency check command — finding #7.
- CSRF `SameSite` guidance — finding #9.
- Vector/semantic retrieval (explicitly deferred in design; insertion point exists).
- PDF/OCR/transcription backends (dependency-explicit by design).
- Per-user identity / rate limiting (single owner token is the documented model).
- Pagination cursors on long UI lists (currently LIMIT-based).

---

## 8. Stop conditions (must prevent a future deployment)

1. Finding #1 or #2 unfixed (backup/restore data loss or fidelity loss).
2. Full test suite not green on the exact revision being deployed.
3. Migration not re-verified against current production data (staging staleness).
4. No cold backup of `/home/azureuser/upload-tool/` taken immediately before cutover.
5. Any production modification made outside the authorized cutover procedure.

---

## Appendix: audit boundaries honored

- No production file, config, credential, or archive was modified, moved,
  renamed, rewritten, or deleted. `server.py` md5 identical before/after.
- No production service restart or reload; no live migration; no DNS/TLS/port/
  firewall/systemd/proxy changes; no packages installed.
- No application fixes implemented (two defects documented with smallest-fix
  recommendations only).
- No share links created for real private material; all destructive tests used
  disposable fixtures under `/tmp/audit-*` (cleaned up).
- No credentials, tokens, or private content appear in this report.
- Repository change: this file only (`docs/PRODUCTION_READINESS_AUDIT.md`).
