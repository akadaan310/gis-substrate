# Final handoff — persistent session & knowledge substrate

Built 2026-10-09 per the project directive. Local repo: `~/workspace/substrate/`
(git). Staging on koda-vm: `/home/azureuser/substrate-staging/` (code) +
`/home/azureuser/stage-substrate/` (migrated DB from a copy of production data).

## 1. Original system actually found

Single-process CPython 3.10 stdlib upload tool (`/home/azureuser/upload-tool/server.py`,
408 lines, v1.1): `files/` (41 MB, 24 objects) + `index.jsonl`; public bearer-URL
`/f/<32hex>.<ext>` links; token-gated `/`, `/list`, `/upload` (25 MB/file, 10 files);
systemd `upload-tool` → Caddy `files.abedkadaan.com` → 127.0.0.1:8123. No DB, no
search, no versioning, no audit, no backups, no tests. Full baseline: `docs/BASELINE.md`.

## 2. Final architecture and why

Same process, same port, same TLS path — the substrate mounts additively at `/s/`
(UI) + `/api/v1/` (JSON) via a ~15-line patch to `server.py` (`deploy/server_patch.py`,
idempotent, keeps `.pre-substrate` backup). Rationale: smallest architecture that
delivers the product (no new services/ports/firewall/DNS changes, no new paid deps).
SQLite + FTS5 (present in SQLite 3.37.2) is the canonical store; content-addressed
`blobs/ab/cd/<sha256>` gives stable artifact identity + byte dedup without merging
provenance; legacy files are indexed **in place** (never moved/renamed/deleted) and
old `/f/` URLs keep working byte-identically. Vector search deferred (documented,
insertion point in `search.py`); stdlib-only + PIL (already on VM).

## 3. Canonical data model

`workspace → session → event` (seq-ordered, time_quality explicit); `artifact`
(identity ≠ filename ≠ URL) + `artifact_revision` + `blob`; `edge` (typed,
directed, explicit|inferred with inferred flagged in UI/API); `decision`
(supersede-preserving); `task` (kind task|question); `claim` + `evidence`
(stance vocabulary); `collection` + ordered/nestable members (no file copying);
`share_link` (hash-stored token, expiry, revocation, access-logged);
`checkpoint` (immutable, references canonical ids); `handoff` (verbatim payload
as immutable artifact, 9-state lifecycle with legal transitions); `extract_job`
+ `extracted_chunk` + FTS5 `search_fts`; `audit_log`; `legacy_file` map.
Full DDL: `substrate/db.py`. Design: `docs/DESIGN.md`.

## 4. Retrieval and context assembly

Layered: A exact (id/filename/sha-prefix/legacy id), B structured SQL filters,
C FTS5 BM25 (injection-safe MATCH builder for SQLite < 3.38), D semantic
(deferred, explicit), E bounded graph traversal (depth ≤ 4, per-depth budget,
explainable paths, inferred flagged), F context packages (token-budget-aware,
estimates labeled as estimates, uncertainties + missing evidence + omitted
reasons), G retrieval audit (query/filters/selected ids logged; content never).
`substrate/search.py`, `substrate/context.py`.

## 5. Collection and navigation experience

Server-rendered UI (`substrate/ui.py`, existing dark identity): home, workspaces,
sessions, timeline, artifacts (+ gallery with keyboard nav), artifact detail
(provenance/edges/revisions/extracted text), collections (navigable, manifest),
graph explorer, unified search, context builder, checkpoints, handoffs (status
pipeline + verbatim payload + hash verify), settings (storage/index health/job
queue/audit/link-model docs). Share viewer `/s/share/<token>` is scope-limited.

## 6. Session, checkpoint, handoff workflows

Events append with seq + content hash; summaries are separate events (originals
never rewritten). Checkpoints reference canonical ids and are immutable.
Handoffs store verbatim payloads as artifacts with sha256; illegal state jumps
rejected; payload revisions are new artifacts.

## 7. Security and access-control behavior

Owner token (query | Bearer header | cookie) gates `/s/*` and `/api/v1/*`.
`private` artifacts: 403 without token, no metadata leak (tested). `public-link`
and legacy `/f/` URLs are bearer URLs — documented as such, never claimed
private (`docs/SECURITY.md`). Share links: scoped, expiring, revocable,
access-logged, tokens hash-stored. Extracted text treated as untrusted
(escaped, never executed). No secrets in logs/DB.

## 8. Migration and backward-compatibility results

Dry-run + real migration on a full copy of production data: 28/28 files indexed,
0 errors; every legacy id resolves; all bytes verified readable in place;
production `files/` untouched (md5sum -c clean against pre-staging checksums).
Patched staging server (127.0.0.1:8124, token-isolated from production):
legacy `/f/` bytes identical to production, `/` with token 200 / without 403;
`/s/` and `/api/v1/` serve the substrate (artifacts, search, writes, share
lifecycle, context packages). `substrate/migrate.py` is idempotent (tested).
Staging also caught and fixed one regression: context provenance leaked host
filesystem paths → now emits stable `legacy_id=` (regression test added).

## 9. Files created/changed

`substrate/{db,store,model,extract,search,context,api,ui,app,export,migrate}.py`;
`deploy/server_patch.py`; `docs/{BASELINE,DESIGN,SECURITY,DEPLOY,ACCEPTANCE,REPORT}.md`;
`docs/BENCHMARKS.json`; `tests/{test_core,test_acceptance,bench}.py`.
Production: nothing changed yet (cutover is the approval item below).

## 10. Migrations

One schema (`SCHEMA_VERSION=1`, `substrate/db.py` DDL, applied by `init_db`).
Legacy indexer: `substrate/migrate.py` — executed against the staging copy only.

## 11. Test commands

- `python3 -m unittest tests.test_core tests.test_acceptance` → 32 passed
  (2026-10-09 ~21:00 EDT).
- `python3 tests/bench.py` → see `docs/BENCHMARKS.json` / `docs/BENCHMARKS.md`.
- Staging: patched `stage-upload-copy/server.py` on 127.0.0.1:8124 against a
  migrated copy of production data; exercised legacy + substrate routes,
  share lifecycle, context packages, and full-bundle export/verify/restore.
  Production `upload-tool` untouched throughout.

## 12. Benchmark data

See `docs/BENCHMARKS.json` (measured, not extrapolated) and
`docs/BENCHMARKS.md` for the table and scaling notes. Headline: metadata,
exact-id, and graph-traversal queries stay sub-millisecond to sub-20ms at
10k; at 100k (525MB DB) FTS common-term ranking is 384ms and a 50-source
context package ~950ms — interactive, with cold page-cache the only
regression vs warm (verified via query plans).

## 13. Acceptance criteria

A–L all PASS on an isolated instance (`tests/test_acceptance.py`, 12 tests).
Details: `docs/ACCEPTANCE.md`.

## 14. Known limitations and technical debt

1. No vector/semantic search (insertion point ready; needs an embedding backend).
2. No PDF text extraction without poppler (`unavailable` recorded honestly); no OCR
   (no tesseract); no transcription (no backend) — all dependency-explicit.
3. Single shared owner token, no per-user identity; token-in-URL legacy kept for
   compatibility (Bearer/cookie also accepted).
4. No rate limiting; no encryption at rest beyond VM disk; backups are file copies.
5. `cgi`-based multipart in legacy `server.py` (deprecated upstream; substrate's
   own parser doesn't use it).
6. Graph depth capped at 4; context token counts are estimates.

## 15. Backup and restoration status

`export.write_bundle` → `verify_bundle` (hash-checked) → `restore_bundle` into an
empty dir: tested twice — acceptance J (synthetic) and a full-bundle run on the
28-object staging archive (verify format_ok + 27/27 blobs; all tables restored
with identical counts; 27/27 restored blobs byte-verified; restored bytes ==
source bytes; restored FTS operational). Ids preserved, FTS rebuilt.
Production backup = file copy of `/home/azureuser/substrate/` (when deployed).

## 16. Working tree / commit

Local git repo `~/workspace/substrate/`, tree clean, commits: see `git log`.
No code pushed anywhere (per directive). Production untouched.

## 17. Actions requiring explicit user approval

- [ ] Copy `substrate/` to `/home/azureuser/substrate/` on koda-vm.
- [ ] Apply `deploy/server_patch.py` to production `server.py` (backup kept).
- [ ] Restart the `upload-tool` systemd service (seconds of downtime).
- [ ] Run `substrate/migrate.py` against live `/home/azureuser/upload-tool/files`
      (read-only to existing files; writes new DB + blobs only).

## 18. Most valuable next engineering improvement

Per-artifact encryption + per-link capabilities are premature. The highest-value
next step is **PDF/OCR extraction backends** (install poppler-utils + tesseract on
the VM, or a small sidecar): the pipeline, job table, retry UX, and FTS sync are
all built — only the binaries are missing, and research PDFs/screenshots are the
bulk of the archive's dark matter.
