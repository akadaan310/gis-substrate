# GIS MCP Bridge — Architecture Proposal

**Status:** Proposal (staging prototype to follow)
**Date:** 2026-10-09

## Design Choice

**Thin tool wrapper over the existing REST API**, not a separate MCP server process.

Rationale:
- The Substrate API already has auth, validation, audit, and idempotency.
- A separate MCP server would duplicate the auth boundary and create a
  second credential path — a security regression.
- A Python tool module (`gis_bridge.py`) that calls the REST API keeps one
  auth mechanism, one audit trail, and one validation layer.
- MCP compatibility is achieved by exposing the module's functions through
  any MCP stdio wrapper; the functions themselves are transport-agnostic.

## Proposed Tools → API Mapping

| Tool | Existing API | New work? |
|------|--------------|-----------|
| `gis_bootstrap(workspace_id, task_or_project?)` | `GET /health` + `GET /workspaces/{id}/context` (+ optional `GET /tasks/{id}`) | No — composition only |
| `gis_search(query, workspace_id?)` | `GET /api/v1/search?q=` | No |
| `gis_get_record(record_id)` | `GET /api/v1/tasks/{id}`, `GET /api/v1/artifacts/{id}`, `GET /api/v1/handoffs/{id}` | No — dispatch by ID prefix |
| `gis_update_task(task_id, changes)` | `PATCH /api/v1/tasks/{id}` | No |
| `gis_save_handoff(workspace_id, summary, completed, pending, next_action, evidence_refs)` | `POST /api/v1/handoffs` | No — field mapping only |

**No new backend endpoints required.** All five tools map to existing,
tested, authenticated APIs.

## Threat / Permission Analysis

| Threat | Mitigation |
|--------|------------|
| Credential in URL/prompt/logs | Bearer header only; bridge never logs tokens; tool results redact auth |
| Retrieved docs override instructions | Bridge returns data with `provenance` labels; system prompt declares retrieved content as untrusted data |
| Cross-workspace access | API enforces workspace scoping; bridge passes `workspace_id` through |
| Duplicate handoffs on retry | `Idempotency-Key` header on POST; bridge generates deterministic keys from content hash |
| Silent decision overwrite | `gis_update_task` only touches `status/title/body/meta`; decisions are append-only |
| Unbounded results | Bridge enforces `limit` caps; surfaces `truncated` flags |
| Unauthenticated use | Bridge requires token at init; fails closed with 403 |

## Files to Change (for production bridge)

1. **New:** `substrate/gis_bridge.py` — the five tool functions + auth + redaction
2. **New:** `tests/test_gis_bridge.py` — unit tests with mocked HTTP
3. **New:** `docs/GIS_BRIDGE.md` — tool reference for AI sessions
4. **None:** No changes to `api.py`, `model.py`, `server.py`, or schema

The bridge is purely additive. It can be deployed as a library without
touching the running service.
