# GIS Session Bootstrap Contract v1

**Status:** Design proposal (not yet implemented)
**Date:** 2026-10-09

## Purpose

A small, versioned procedure for a new AI session to establish working
context from Substrate (GIS) without loading the entire knowledge store
and without relying on pasted transcripts.

## Contract

### Inputs
- `workspace_id` (required): e.g. `ws_y3a9agw0eqzj`
- `task_or_project` (optional): a task ID or project name to focus retrieval
- `auth`: Bearer token via `Authorization` header (never in URL, prompt, or logs)

### Procedure (v1)

1. **Health check.** `GET /api/v1/health` → 200 confirms service availability.
   If unreachable, report "GIS unavailable" and do not fabricate context.

2. **Retrieve context package.** `GET /api/v1/workspaces/{id}/context`
   - Bounded (default 50/section, hard-capped). Check `truncated` flags.
   - If truncated, note which sections are incomplete.

3. **Identify applicable records.**
   - Protocols (`kind="protocol"`): governing procedures for this workspace.
   - Active tasks: `status` in (`open`, `in_progress`, `blocked`), via `next_actions`.
   - Decisions: prior decisions with rationale.
   - If `task_or_project` given, fetch it directly via `GET /api/v1/tasks/{id}`
     and follow its `derives_from` edges to the governing protocol.

4. **Classify information.**
   - **Verified facts:** record IDs, titles, statuses, timestamps, edge relationships —
     anything returned by the API.
   - **Reported claims:** body text of records (authored by a prior session; may be stale).
   - **Hypotheses:** anything inferred by the current session (must be labeled as such).
   - **Missing:** explicitly note what was not found (e.g., "no decisions recorded").

5. **State the retrieval.**
   - Report: workspace ID, counts retrieved, truncation status.
   - Report: what remains unknown or unverified.

6. **Bound the load.**
   - Do not call `traverse` or full-text search across the entire store
     unless the context package is insufficient.
   - Prefer the bounded context package; escalate only with reason.

### Idempotency

- Steps 1–6 are **read-only**. Repeated bootstrap calls MUST NOT create,
  modify, or delete records.
- Writes (task updates, handoffs) require explicit intent and use the
  API's idempotency keys where supported (`Idempotency-Key` header on POST).

### Handoff Contract (companion)

When saving a handoff via `POST /api/v1/handoffs`:
- Required: `objective` (what this handoff covers)
- Recommended: `workspace_id`, `payload_text` (structured summary),
  `scope` (completed/pending), `expected` (next action), `meta` (evidence refs)
- Use `Idempotency-Key` header to prevent duplicate handoffs on retry.
- Never overwrite an existing handoff; create a new one.

## Versioning

- Contract version is `v1`. Breaking changes require `v2` and a migration note.
- The `meta.contract_version` field on handoffs records which version was used.
