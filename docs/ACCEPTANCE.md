# Acceptance criteria — execution record

Each criterion is exercised end-to-end against an isolated instance
(`tests/test_acceptance.py` drives a live `SubstrateApp` on a temp data dir).
Status is PASS only when the behavior was observed, not when code merely exists.

## A. Artifact provenance
Upload a test screenshot → register artifact → link session --references--> artifact
via edge + event kind=upload → GET event → neighbors(out) → artifact → neighbors(in)
→ event. Assert both directions resolve and provenance fields are present.

## B. Multi-file collection
Create collection → add 3 artifacts + 1 nested collection → open
`/s/collections/{id}` (and `/api/v1/collections/{id}/manifest`) → entries listed
in order → retrieve one member's bytes via `/api/v1/artifacts/{id}/content` →
bytes match original upload.

## C. Session continuity
Create session + events + decision → create checkpoint with body referencing
session/event/decision ids → new process: load checkpoint only, then fetch the
referenced records by id → assert the reconstructed state matches without
listing the whole archive.

## D. Relationship navigation
Chain: session --contains--> event --introduces--> artifact(screenshot)
--documents--> decision --authorizes--> task --produces--> artifact(code)
--validates--> (test result event). Traverse from session with max_depth=4 and
assert the full chain is reachable with explainable paths.

## E. Bounded search
Seed 5,000 synthetic artifacts + events; run `search_all` with limit=25; assert
`len(results) <= 25`, response contains no full-archive serialization, and
`total_considered` is reported.

## F. Context package
Build package for a test question with budget_tokens=1500; assert every source
has doc_type/doc_id/locator/provenance/why_included, token_estimate <= budget
(+label `token_estimate_is_estimate: true`), uncertainties + missing_evidence
present, and a second call can fetch an additional artifact by id.

## G. Handoff integrity
Create handoff with payload_text → GET payload → bytes verbatim equal input,
sha256 matches `payload_hash`; walk statuses prepared→ready→authorized→sent→
acknowledged→executing→completed; assert illegal jump prepared→completed is
rejected; assert a second revision creates a new artifact (original bytes unchanged).

## H. Revision history
Upload doc → add revision → assert rev 1 bytes still retrievable via its blob,
current artifact serves rev 2 bytes, revision list shows both with change notes.

## I. Access control
Private artifact: GET content without token → 403 and response contains no
filename/size/mime metadata. Share link for a collection: token opens only that
collection; tampered token → 404; revoked → 404; expired → 404. Raw share token
absent from DB (only hash stored).

## J. Export and restoration
`write_bundle` → `verify_bundle` (all hashes ok) → `restore_bundle` into empty
dir → assert ids preserved, bytes identical, FTS finds a known phrase, edge
count matches.

## K. Backward compatibility
Apply `deploy/server_patch.py` to a copy of production `server.py`; assert legacy
routes (`/`, `/list`, `/upload`, `/f/<id>`) behave identically: same status codes,
same bytes for a known file, token gate intact. Migration dry-run indexes every
legacy id; every legacy id resolves via `legacy_file`.

## L. Operational integrity
Enqueue extraction for a corrupt image (valid extension, invalid bytes) → job
fails with `last_error` set, artifact bytes intact and retrievable → retry
endpoint resets to pending → queue depth visible on `/s/settings`, failure
observable without corrupting the source.

## Results

2026-10-09 ~20:00 EDT, local: `python3 -m unittest tests.test_acceptance
tests.test_core` → **32 tests, 32 passed** (18 core incl. migration +
no-path-leak, 12 acceptance A–L, plus 2 new regression tests).

2026-10-09 ~20:30–21:15 EDT, koda-vm staging (patched copy of production
`server.py` on 127.0.0.1:8124, migrated copy of production data):

- Migration: 28/28 legacy files indexed in place, 0 errors; 28 artifacts,
  28 revisions, 28 legacy mappings, 27 blobs (1 dedup hit); production
  files byte-identical before/after (md5sum -c clean); production
  `server.py` never patched, `upload-tool` service never restarted.
- Legacy routes: `/f/<id>` 200 with bytes identical to production file;
  `/` with token 200, without 403; unknown id 404.
- Substrate: `/api/v1/health` 200 (schema 1); `/s/` 200; artifacts list,
  search, workspace/session/event writes, share create → page (200,
  no-auth) → revoke → 404, context package with retrieval audit — all 200.
- Regression found + fixed in staging: context provenance leaked host
  filesystem paths → now emits `legacy_id=` (test: `TestNoPathLeak`).
- Full-bundle export/verify/restore on the 28-object archive: verify
  format_ok + 27/27 blobs; all tables restored with identical counts;
  27/27 restored blobs byte-verified; restored bytes == source bytes;
  restored FTS operational.
