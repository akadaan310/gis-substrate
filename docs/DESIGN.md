# Substrate Design — persistent session & knowledge substrate

## 0. Decisions (with reasons)

| # | Decision | Reason |
|---|----------|--------|
| 1 | Stay stdlib-only Python (+PIL, already on VM), same `http.server` process | Smallest architecture; zero new services, zero new ports, zero new paid deps. Reuses the hardened Caddy TLS path. |
| 2 | SQLite + FTS5 as the canonical store, one file `substrate.db` | Single-file backup story, WAL-capable, FTS5 + JSON1 present in SQLite 3.37.2. Relational edge table is sufficient for first implementation; no graph DB (operational cost unjustified — §3 of directive allows this). |
| 3 | Content-addressed blob store `blobs/ab/cd/<sha256>` | Stable artifact identity independent of URL/filename; byte-level dedup without merging provenance/ACL records. |
| 4 | Legacy files indexed **in place**, never moved | Non-destructive migration; old `/f/` URLs byte-identical. |
| 5 | Vector search **deferred** | Needs an embedding service/model; FTS5 + structured + graph retrieval covers the product need. Documented as technical debt with an insertion point (`search.py::semantic_candidates`, currently returns `[]` with reason). |
| 6 | Server-rendered HTML UI, no JS framework | Matches existing dark visual identity; works without build step; keyboard nav via small vanilla JS. |
| 7 | Same shared-secret auth, extended: `?token=` ∪ `Authorization: Bearer` ∪ cookie | Backward compatible; header/cookie avoid token-in-URL for API use. Rotation procedure documented. |
| 8 | Extraction as in-process worker thread + `extract_job` table | No new queue service; jobs survive restart (pending jobs re-run); failure never touches the original blob. Missing toolchains (pdftotext, tesseract) are recorded as `unavailable` — never faked. |
| 9 | Mount at `/s/` (UI) + `/api/v1/` (JSON) inside the existing server process | No new public service, no firewall change, no Caddy change. ~15-line additive patch to `server.py`. |

## 1. Identity

Every object: `id TEXT PRIMARY KEY` like `art_9f3k2m8q1x4z` (prefix = type, 12 chars from
`secrets.token_urlsafe` filtered to `[a-z0-9]`). Stable, independent of titles/URLs/filenames.
Timestamps: REAL unix epoch + `time_quality` in (`exact`,`imported`,`estimated`,`unknown`).
Never invent timestamps: unknown → NULL + `time_quality='unknown'`.

## 2. Schema (see `substrate/db.py` for DDL — this is the contract)

- `workspace(id, title, description, created_at, meta_json)`
- `session(id, workspace_id, title, started_at, ended_at, time_quality, source, source_id, meta_json)`
  - `source`: `chat` | `claude-code` | `manual` | ...; `source_id`: original id (e.g. chat_id)
- `event(id, session_id, seq, ts, time_quality, actor, role, kind, text, content_hash, meta_json)`
  - `kind`: `message` | `instruction` | `tool_result` | `upload` | `decision_ref` | `checkpoint_ref` | `handoff_ref` | `note`
  - `content_hash`: sha256 of canonical text when available; summaries are separate events with `kind='note'` and an edge `summarizes` → original (never rewrite originals)
- `artifact(id, blob_sha256, filename, mime, size, created_at, visibility, source, source_ref, meta_json)`
  - `visibility`: `public-link` | `private`; `source`: `upload` | `legacy` | `generated` | `import`
- `blob(sha256 PRIMARY KEY, size, stored_at)` — refcount derived by query, not stored
- `artifact_revision(id, artifact_id, rev_no, blob_sha256, created_at, change_note, parent_rev)`
- `edge(id, src_type, src_id, dst_type, dst_id, rel, created_at, provenance, inferred, note)`
  - `provenance`: `explicit` | `inferred`; `inferred` BOOLEAN — inferred edges are labeled in UI/API, never presented as confirmed
  - indexes: `(src_type, src_id, rel)`, `(dst_type, dst_id, rel)`, `(rel)`
- `decision(id, workspace_id, session_id, title, body, status, decided_at, time_quality, supersedes_id, meta_json)` — status: `proposed|active|superseded|reverted`. Supersede links old→new; old row is never edited.
- `task(id, workspace_id, kind, title, body, status, created_at, updated_at, meta_json)` — `kind`: `task|question`; task status: `open|in_progress|blocked|done|cancelled`; question status: `open|answered|unresolved` (a question stays `open`/`unresolved` until an explicit resolution event)
- `claim(id, workspace_id, title, body, confidence, verification_status, created_at, meta_json)` — `verification_status`: `unverified|supported|partially_supported|contested|refuted`
- `evidence(id, claim_id, artifact_id, event_id, locator, stance, note, created_at)` — `stance`: `supports|refutes|reproduces|derives_from|cites|contradicts|qualifies|context`; `locator`: free text (`p.12`, `L40-60`, `00:13:02`)
- `collection(id, title, description, visibility, created_at, updated_at, meta_json)`, `collection_member(id, collection_id, artifact_id, child_collection_id, position, note, added_at)` — ordered, nestable, no file copying
- `share_link(id, token_hash, scope_type, scope_id, created_at, expires_at, revoked_at, note)` — token shown once at creation; only the hash is stored; access logged
- `checkpoint(id, workspace_id, session_id, title, created_at, schema_version, body_json)` — body references canonical ids; immutable once written
- `handoff(id, workspace_id, from_session_id, to_env, objective, payload_artifact_id, payload_hash, status, scope_json, expected_json, created_at, updated_at, meta_json)` — status: `prepared|ready|authorized|sent|acknowledged|executing|completed|failed|abandoned`; payload stored as an immutable artifact; revisions are new artifacts, never edits
- `extract_job(id, artifact_id, kind, status, attempts, last_error, created_at, updated_at)` — `pending|running|done|failed|unavailable`
- `extracted_chunk(artifact_id, chunk_no, page_no, text)` + `search_fts` (FTS5: `doc_type, doc_id, title, body`) synced on write by application code (deterministic, testable)
- `audit_log(id, ts, actor, action, scope_type, scope_id, detail_json)` — auth failures, share accesses, mutations, retrievals (query text logged, not content)
- `legacy_file(legacy_id PRIMARY KEY, artifact_id)` — old `/f/<id>` → artifact
- `kv(key, value)` — `schema_version`, settings

Controlled vocabularies for `edge.rel` (extendable): `contains, references, introduces, documents, produces, authorizes, changes, validates, supports, refutes, derives_from, cites, contradicts, qualifies, summarizes, supersedes, resumes, blocks, depends_on, investigates, resolves, requires_verification, member_of, captures, acknowledges, embeds, renders`.

## 3. Retrieval architecture (layers A–G)

- **A exact**: id / filename / sha256 / legacy id / source_id lookups — single indexed queries.
- **B structured**: SQL filters (workspace, session, actor, time range, type, visibility, tags, source). All list endpoints paginated (`limit` default 50, max 500, cursor = `(created_at, id)`).
- **C full-text**: FTS5 `search_fts` with BM25 ranking; results carry `doc_type, doc_id, title, snippet, rank` and always resolve to the source artifact + locator.
- **D semantic**: insertion point only; returns empty with explicit reason until an embedding backend is configured.
- **E relationship-aware**: bounded BFS from seed ids over `edge`, depth default 2 / max 4, per-depth node budget 200, `rel` filter allow-list; returns nodes + the path taken (explainable); inferred edges flagged.
- **F context package** (`POST /api/v1/context`): `{question, budget_tokens, seed}` → `{sources:[{doc_type, doc_id, title, excerpt, locator, provenance, why_included}], relationships, uncertainties, missing_evidence, additional_index, omitted:{count, reasons}, token_estimate, token_estimate_is_estimate:true, retrieval_id}`. Budget enforced by greedy BM25+recency order with excerpt truncation. Never invents token counts.
- **G retrieval audit**: every context/search-with-`audit=1` call logs `{query, filters, ts, selected_ids, rationale, algorithm:'fts5-bm25+graph-v1', limitations}` to `audit_log`. Content not logged.

## 4. Extraction pipeline

Worker thread drains `extract_job` (pending → running → done|failed|unavailable). Extractors:
`text/*,.md,.csv,.json,.xml,.log` + code extensions → utf-8 chunks (~120 lines, page_no=line range);
`.zip` → entry listing; images (PIL) → dims/format/EXIF + 320px thumbnail as derived artifact
(edge `derives_from`, never overwrites original); `.pdf` → `pdftotext` if present else `unavailable`;
OCR/transcription → `unavailable` unless tesseract/whisper present (not installed — recorded honestly).
Failure sets `last_error`, keeps original, retryable via API.

## 5. API (`/api/v1`, versioned, JSON, token auth)

`GET /health`, workspaces CRUD, sessions CRUD + `POST /sessions/:id/events`,
artifacts `POST /artifacts` (multipart, visibility flag) + `GET /artifacts/:id` (+ `/content`, `/revisions`, `/edges`),
edges CRUD, decisions/tasks/claims/evidence CRUD, collections CRUD + members,
checkpoints CRUD, handoffs CRUD + status transitions, `GET /search`, `GET /traverse`,
`POST /context`, `GET /export`, share links create/revoke, extract retry.
All mutations validate input, are idempotent where retryable (`Idempotency-Key` on POST create),
and return bounded pages. Errors: `{error:{code, message}}` with proper HTTP status.

## 6. UI (`/s/`, token-gated; share viewer `/s/share/<token>` public-by-token)

Shared dark shell (existing identity: `#0f1115`/`#171a21`/`#7fb0ff`). Pages:
home (recent workspaces/sessions/checkpoints), workspaces, workspace detail,
sessions, session detail (event timeline), artifacts (+ gallery w/ keyboard nav),
artifact detail (metadata, provenance, edges, revisions, extracted text),
collections, collection viewer (navigable, per-entry open, manifest export),
graph (seed → neighborhood, depth + rel filter), search (unified),
context builder (form → package view), checkpoints, handoffs (status pipeline + verbatim payload + hash verify),
settings (storage, index health, job queue, audit tail, link-model documentation).
Provenance and inferred-vs-explicit labels visible everywhere.

## 7. Security model (see docs/SECURITY.md)

- Owner token: full access. `public-link` artifacts: readable by URL guess-resistance only (documented, not claimed private). `private`: token required everywhere including thumbnails/extracted text/search snippets. Share links: scoped, optional expiry, revocable, access-logged.
- Server-side authz on every route; no private metadata on public endpoints; path traversal impossible (content-addressed paths + legacy regex); uploads never executed; `Content-Disposition: attachment` for non-safe types; secrets never logged; retention = manual (documented).
- Extracted text and transcripts treated as untrusted content (displayed escaped, never executed); retrieval never turns source text into instructions.

## 8. Migration & compatibility

- Phase 1 (this build): additive only. Legacy index in place. Old `/f/` route untouched.
- Export format: versioned JSON manifest + blobs (partitioned by workspace); restore into empty DB verifies hashes; tested in isolation (see docs/DEPLOY.md).
- Production cutover checklist lives in docs/DEPLOY.md and requires explicit user approval.
