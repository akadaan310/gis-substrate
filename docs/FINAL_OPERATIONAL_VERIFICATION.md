# Final Operational Verification — Substrate

**Starting revision:** `7a1a98c` (implementation directive complete)
**Final revision:** `253849e` (integrity checker legacy-storage fix)
**Verification date:** 2026-10-09 ~21:20–22:00 EDT
**Original audit:** `a3abb7a` — preserved unchanged as historical evidence.
**Scope:** Close the three outstanding operational gates. No production
deployment, no production changes, no new feature work.

---

## Gate 1: OCR verification — PASS

**Environment:** Dev VM (this machine), isolated from production. Tesseract
installed via `apt-get` (production untouched — no packages installed there).

**Installation:** `tesseract-ocr` 5.3.4 (leptonica-1.82.0) + English language
data. Initial `apt-get update` stalled on slow mirrors; succeeded on retry.

**Deterministic test:**
- Fixture: image-only PDF (`/tmp/scan-test.pdf`), 1 page, **0 characters** of
  embedded text (verified via `pdftotext`).
- Expected text: `SCANNED DOCUMENT marker-scanned-xyz-123` (rendered with PIL).

**Results:**
| Check | Result |
|-------|--------|
| Extraction completes | ✅ job status `done` |
| Chunk created | ✅ 1 chunk, `page_no=p1` |
| Backend recorded | ✅ `tesseract 5.3.4` |
| Method recorded | ✅ `ocr` (distinguished from `embedded-text`) |
| Text recognized | ✅ `SCANNED DOCUMENT marker-scanned-xyz-123` (exact) |
| FTS indexed | ✅ searchable |
| Search for `scanned-xyz-123` | ✅ 1 hit, provenance `tesseract 5.3.4/ocr` |

**Unit test:** `test_image_ocr` now PASSES (was skipped before Tesseract install).

---

## Gate 2: Fresh staging migration and recovery — PASS

**Procedure (read-only source):**
1. Created `/home/azureuser/stage-verify/` (isolated, no servers running).
2. `cp -a` of `/home/azureuser/upload-tool/files/` → 34 files copied.
3. File lists identical; **all 34 MD5 hashes identical** (no content exposed).
4. Deployed substrate code at `7a1a98c` via `git archive` (no production code touched).

**Migration:**
| Check | Result |
|-------|--------|
| Dry run | ✅ `would index 34 legacy files` |
| Real migration | ✅ `indexed=34 errors=0 total_ids=34` |
| Idempotency (re-run) | ✅ `indexed=34 errors=0` (0 new, 0 duplicates) |
| Legacy compatibility | ✅ all 34 artifacts `source=legacy`, `source_ref` intact |

**Integrity check** (`python3 -m substrate.check`):
- **Result: all checks passed** (after fixing the checker to handle legacy
  in-place storage — blob rows exist for hashing but files stay in place).
- 34 artifacts, 34 revisions, 33 blobs (1 duplicate), 34 legacy mappings.
- 0 missing files, 0 hash mismatches, 0 dangling references, 0 stale chunks.

**Export/restore regression:**
| Check | Result |
|-------|--------|
| `verify_bundle` | ✅ `format_ok=True structure_ok=True blobs_ok=33` |
| `restore_bundle` | ✅ OK to separate destination |
| Artifact counts | ✅ 34 → 34 |
| Revision counts | ✅ 34 → 34 |
| Blob counts | ✅ 33 → 33 |
| Legacy mappings | ✅ 34 → 34 |

**Differences from prior 28-file staging:**
- Old staging: 28 files, migrated 2026-10-09 ~20:00 EDT.
- Fresh staging: 34 files (6 new uploads since), migrated 2026-10-09 ~21:30 EDT.
- The 6 new files are ordinary uploads (images/documents); migration handled
  them identically (0 errors). No behavioral differences observed.

---

## Gate 3: Caddy query-token logging — CONFIRMED RISK

**Method:** Read-only inspection of `/etc/caddy/Caddyfile` and systemd journal.
No config changes, no restarts, no log modifications. No tokens printed in this report.

**Findings:**

| Question | Answer |
|----------|--------|
| Are Caddy access logs enabled? | **No.** The Caddyfile has no `log` directive; Caddy v2.11.7 does not write access logs by default. Normal requests with `?token=` are **not** routinely logged. |
| Can tokens appear in logs? | **Yes, via error logs.** When a `reverse_proxy` error occurs, Caddy logs the full request to the systemd journal, including headers. Observed a real example where the `Referer` header contained `https://files.abedkadaan.com/?token=<redacted>`. |
| Does the upload-tool log requests? | **No.** `server.py` overrides `log_message` to be quiet (line 402). |

**Risk assessment:** Tokens are not logged on every request, but they **can**
be captured in the systemd journal when (a) a proxy error occurs and (b) the
browser sends a `Referer` header containing `?token=`. The journal persists
across reboots and may be included in support bundles.

**Mitigations already implemented (in 7a1a98c):**
- UI no longer propagates `?token=` in links when the session uses cookie/Bearer auth.
- `POST /s/session?action=establish` sets `HttpOnly; SameSite=Lax` cookie.
- Recommendation: rotate the owner token post-cutover (documented in deployment prerequisites).

**Smallest safe way to fully resolve:** Add a Caddy `log` directive with a
custom format that excludes query strings, OR front the token auth with a
header-only scheme. Both require config changes — out of scope for this
verification (no production changes permitted).

---

## Gate 4: Regression verification — PASS

**Final revision:** `253849e` (tree clean)

**Test commands and results:**
```bash
$ python3 -m unittest tests.test_core tests.test_acceptance \
    tests.test_export_integrity tests.test_request_limits \
    tests.test_extraction tests.test_check
Ran 65 tests in 4.9s — OK (skipped=1)

$ python3 tests/test_api_smoke.py
66 passed, 0 failed
```

| Suite | Tests | Pass | Skip | Fail |
|-------|-------|------|------|------|
| test_core | 18 | 18 | 0 | 0 |
| test_acceptance | 14 | 14 | 0 | 0 |
| test_export_integrity | 5 | 5 | 0 | 0 |
| test_request_limits | 10 | 10 | 0 | 0 |
| test_extraction | 9 | 8 | 1* | 0 |
| test_check | 9 | 9 | 0 | 0 |
| test_api_smoke.py | 66 checks | 66 | 0 | 0 |

*Skip: `test_image_without_tesseract_reports_honestly` — correctly skips when
Tesseract IS installed (tests the missing-dependency path).

**Original defects rechecked:**
- Unscoped export completeness: ✅ 5/5 integrity tests pass.
- Extracted text preservation: ✅ chunks exported, FTS rebuilt, searchable.
- Request-body limits: ✅ 10/10 limit tests pass (hook gate, malformed lengths, no partial state).

**New commits during this follow-up:**
- `253849e` — integrity checker handles legacy in-place storage (1 file: `substrate/check.py`).

**New files during this follow-up:**
- None in the repo (staging dirs on VM are outside the repo).

---

## Remaining risks

1. **Token exposure via Caddy error logs** (Gate 3): Confirmed. Mitigated by
   P6 changes but not eliminated. Recommend token rotation post-cutover.
2. **No concurrent-load testing**: SQLite WAL mode with worker + request
   threads not stress-tested.
3. **Disk-full behavior**: Error paths exist but not exercised against a full disk.
4. **Tesseract in production**: Not installed there (correctly). If OCR is
   needed in production, it must be installed during the deployment window.

---

## Production confirmation

- `server.py` md5 `a94bccfb7edc094ab4dbb1d64c1b2aea` — identical before/after.
- No patch applied in production; service never restarted.
- No files modified, moved, or deleted in production.
- No packages installed in production.
- Staging operations used isolated paths (`/home/azureuser/stage-verify/`);
  migration/restore never pointed at production.

---

## Recommendation

**Substrate is ready for a separate production-deployment review.**

All three operational gates are closed:
- ✅ OCR verified end-to-end (Tesseract 5.3.4).
- ✅ Fresh 34-file staging migrated, verified, and restored cleanly.
- ✅ Caddy logging behavior determined (tokens can appear in error logs via Referer; mitigations in place).

The two original audit blockers are fixed and regression-tested. The test
suite is green (65 + 66 checks). The remaining risks are operational and
documented above; none blocks a deployment review.

**A successful test suite does not by itself authorize deployment.** The
deployment review should cover: cutover procedure, rollback plan, token
rotation, Tesseract installation in production (if OCR needed), Caddy log
format hardening, and monitoring/alerting.
