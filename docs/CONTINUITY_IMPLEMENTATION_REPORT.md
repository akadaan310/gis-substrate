# Continuity Milestone — Implementation Report

**Branch:** `continuity` (commit `48db663`)
**Status:** Implemented, tested in staging. **NOT deployed to production.**
**Date:** 2026-10-09 ~22:15 EDT

---

## Design Decision: Protocol Representation

**Decision:** Protocols are tasks with `kind="protocol"`, not a separate table.

**Rationale:**
- Protocols share the complete task infrastructure: workspace scoping,
  status lifecycle (`open` → `in_progress` → `done`), `updated_at` revision
  tracking, FTS indexing, audit trail, and edge relationships.
- A separate `protocol` table would duplicate: workspace FK, status
  management, FTS triggers, audit hooks, and edge compatibility — for no
  additional integrity guarantees.
- Structured checklist steps live in `meta_json["steps"]` as
  `[{text, done}]`. Schema stays stable; structure is in the JSON.
- The DB schema has `kind TEXT` with no CHECK constraint — this is a
  **code-only change, no migration required**.

**Trade-off acknowledged:** Protocols and tasks share the `tsk_` ID prefix
and the `task` table. A future need for protocol-specific fields (e.g.,
versioning, approval workflows) may warrant a separate table. The current
design does not preclude this.

---

## Data Model Changes

| Change | Location | Migration? |
|--------|----------|------------|
| `TASK_KINDS = ("task", "question", "protocol")` | `model.py` | No |
| `create_task()` accepts `kind="protocol"` | `model.py` | No |
| `update_task()` — partial update with FTS reindex | `model.py` | No |
| `set_task_status()` delegates to `update_task()` | `model.py` | No |

**Retention/immutability semantics (documented, not changed):**
- No delete API exists for artifacts, tasks, or protocols. This is intentional.
- Records are immutable except via PATCH (tasks) — every change updates
  `updated_at` and is audit-logged.
- Test records are labeled (`TEST-`, `ACCEPTANCE-` prefixes) and left intact.

---

## Endpoint Contracts

### New: `POST /api/v1/tasks` with `kind="protocol"`
- Accepts `kind` in `("task", "question", "protocol")`
- `meta.steps` optionally carries structured checklist
- Returns 201 with task record

### New: `GET /api/v1/tasks/<id>`
- Validates ID format (`tsk_` prefix, alphanumeric) → 400 if malformed
- Returns 404 if not found
- Returns task + `edges_out` + `edges_in` (provenance)
- Requires auth → 403 if missing/invalid

### Extended: `PATCH /api/v1/tasks/<id>`
- Partial update: `status`, `title`, `body`, `meta` (at least one required)
- Status validated against kind (`task`/`protocol` → TASK_STATUS, `question` → QUESTION_STATUS)
- Invalid status → 400; empty patch → 400; malformed ID → 400; not found → 404
- Updates FTS index if title/body change
- Audit-logged via `task.patch` action
- **Requires `do_PATCH` in server.py** (see deployment notes)

### New: `GET /api/v1/workspaces/<id>/context`
Read-only context package for continuity:
```json
{
  "context": {
    "workspace": {...},
    "protocols": [...],
    "tasks": [...],
    "questions": [...],
    "decisions": [...],
    "edges": [...],
    "next_actions": [{"id", "kind", "title", "status", "updated_at"}],
    "counts": {"protocols": N, "tasks": N, ...},
    "truncated": {"protocols": false, ...},
    "limits": {"per_section": 50, "next_actions": 10, "edges_per_task": 50}
  }
}
```
- `next_actions`: open/in_progress/blocked tasks+protocols, most-recent first, max 10
- Truncation is explicit — `truncated` flags indicate when limits were hit
- All sections bounded; no silent data loss

---

## Deployment Implications

### `do_PATCH` support
- Production `server.py` currently has no `do_PATCH` handler (only GET/POST hooks).
- `deploy/server_patch.py` updated to add `do_PATCH`:
  - **Fresh patch:** adds the method during initial patch
  - **Already-patched:** detects missing `do_PATCH` and adds only that method (idempotent)
- **Production cutover for continuity:** re-run the updated patch script on
  production `server.py`. It will detect the existing patch and add only `do_PATCH`.
  Then restart the service. Rollback: `.pre-substrate` backup still valid.

### Migration
- **No database migration required.** The `kind` column is TEXT with no constraints.
- Existing records unaffected. New `kind="protocol"` values are additive.

### Backward compatibility
- All existing endpoints unchanged in behavior.
- `set_task_status()` preserves its signature (delegates to `update_task`).
- New routes are additive; no existing routes modified.

---

## Test Evidence

**Unit tests:** 20 new tests in `tests/test_continuity.py` — all pass.
- Model: protocol creation, invalid kind rejection, partial updates, FTS reindex, nonexistent handling
- API: protocol creation, invalid kind, single GET (valid/invalid/not-found/unauth), PATCH (status/title/body/meta, invalid status, empty, unauth), context package, cross-workspace isolation

**Full suite:** 85 tests pass (65 existing + 20 new), 1 skip (tesseract).

**Staging end-to-end (isolated, port 18927):**
- `do_PATCH` hook added to production-copy server.py via updated patch script ✅
- HTTP PATCH through staging server: `in_progress` status set ✅
- Full continuity acceptance: protocol → tasks → decision → edges → status change → context package → all links verified ✅
- Context package retrieved and validated ✅

---

## Unresolved Design Decisions

1. **Protocol-specific status values?** Currently protocols use TASK_STATUS.
   If protocols need states like "deprecated" or "draft", add PROTOCOL_STATUS.
   Not needed for the current milestone.

2. **Delete API?** Intentionally not added. If retention policy requires
   deletion, design a soft-delete with audit trail (not hard delete).

3. **Context package `next_actions` ordering:** Currently most-recent-first.
   Could be priority-weighted if tasks gain priority fields.

---

## Production Cutover Checklist (for separate authorization)

- [ ] Review this report and the `continuity` branch diff
- [ ] Re-run updated `deploy/server_patch.py` on production (adds `do_PATCH` only)
- [ ] Verify patch output: "added do_PATCH hook"
- [ ] Restart `upload-tool` service
- [ ] Smoke test: PATCH a test task, GET context package
- [ ] Rollback available via `.pre-substrate` if needed
