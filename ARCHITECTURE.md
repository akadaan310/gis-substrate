# Architecture

## Overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  ChatGPT    │────▶│ Browser      │────▶│ GIS API     │
│  (web)      │ copy│ Sidecar      │ POST│ /api/v1/    │
│             │◀────│ /s/capture/  │◀────│ handoffs    │
└─────────────┘ paste└──────────────┘     └──────┬──────┘
                                                │
┌─────────────┐     ┌──────────────┐             │
│  Claude     │────▶│ MCP Server   │─────────────┘
│  Code etc.  │stdio│ (5 tools)    │   read/write
└─────────────┘     └──────────────┘   (gated)
```

## Persistence

- **SQLite** (`substrate.db`): relational records (tasks, handoffs, decisions, claims, edges)
- **Content-addressed blobs** (`blobs/`): artifact content by SHA-256
- **FTS5**: full-text search index

## Request flow

1. `app.py` receives HTTP, checks auth, routes to `api.py` or `ui.py`
2. `api.py` validates input, calls `model.py` domain logic
3. `model.py` enforces state transitions (e.g., handoff lifecycle)
4. `db.py` persists; `store.py` handles blobs
5. Audit log records every mutation

## MCP integration

`packages/gis-mcp/gis_mcp/`:
- `gis_mcp_server.py`: stdio JSON-RPC, tool registration, `readOnlyHint` annotations
- `gis_bridge.py`: HTTP client wrapping the REST API; `GIS_WRITE_ENABLED` gates mutations

Tools: `gis_bootstrap` (read), `gis_search` (read), `gis_get_record` (read),
`gis_prepare_relay` (read), `gis_validate_relay` (read),
`gis_update_task` (write-gated), `gis_save_handoff` (write-gated).

## Relay validation

`substrate/relay.py`:
- `prepare_relay()`: assembles task + records + definitions + validation
- `validate_relay()`: extracts named references, classifies as included /
  accessible / unknown; fails on unresolvable terms

## UI

`substrate/ui.py`: server-rendered HTML. Key routes:
- `/s/` — home, `/s/workspaces`, `/s/handoffs`, `/s/capture/` (sidecar)

## What is NOT in this repo

- Production database or GIS records
- Credentials, tokens, `.env` files
- Uploaded user files
- The production Caddy/systemd configuration (see deployment docs)
