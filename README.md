# Golden Intelligence System (GIS) / Substrate

The knowledge substrate powering Abed's AI continuity: a content-addressed,
SQLite-backed knowledge graph with task tracking, handoffs, decisions, and
MCP integration for AI assistants.

**Repository:** https://github.com/akadaan310/gis-substrate
**Production:** https://files.abedkadaan.com (private deployment)
**Component repo:** https://github.com/akadaan310/gis-browser-sidecar

## System boundaries

- **GIS/Substrate** (this repo): knowledge store, API, MCP server, relay validation, web UI
- **Browser Sidecar** ([separate repo](https://github.com/akadaan310/gis-browser-sidecar)): ChatGPT → GIS capture page
- **Production deployment**: private, on koda-vm; this repo contains no production data or credentials

## Deployed vs published

| Location | Commit | Notes |
|----------|--------|-------|
| This repo (main) | `d9a0843` | Full source including relay MCP tools |
| Production `/home/azureuser/substrate/` | `0abd6f8e` + relay.py | UI capture page deployed; relay.py pending |

## Major modules

| Path | Responsibility |
|------|---------------|
| `substrate/app.py` | HTTP server, request routing, auth |
| `substrate/api.py` | REST API: workspaces, tasks, artifacts, handoffs, decisions, claims, edges |
| `substrate/model.py` | Domain logic: handoff lifecycle, task transitions |
| `substrate/db.py` | SQLite schema, migrations |
| `substrate/ui.py` | Server-rendered web UI (`/s/` routes) including `/s/capture/` |
| `substrate/relay.py` | Context-complete relay: `prepare_relay()`, `validate_relay()` |
| `substrate/search.py` | Full-text search (FTS5) |
| `substrate/store.py` | Content-addressed blob storage |
| `packages/gis-mcp/` | MCP server: 5 tools, stdio transport, write gating |

## Test suite

```bash
# Core (20) + API smoke (66) + Continuity (20)
python3 -m unittest tests.test_core tests.test_api_smoke tests.test_continuity

# Relay regression (5) + MCP relay (5)
python3 tests/test_relay.py
python3 tests/test_mcp_relay.py
```

## Authentication & writes

- Owner token via `Authorization: Bearer`, `?token=`, or `substrate_token` cookie
- Write operations require token; MCP writes additionally require `GIS_WRITE_ENABLED=1`
- Provisioning: set `GIS_BASE_URL` and `GIS_TOKEN` env vars; never commit secrets

## Entry points

- **MCP**: `packages/gis-mcp/gis_mcp/gis_mcp_server.py` (stdio)
- **Relay tools**: `gis_prepare_relay`, `gis_validate_relay` (read-only)
- **Sidecar**: `/s/capture/` route in `substrate/ui.py`
- **API**: `/api/v1/` routes in `substrate/api.py`

## Related systems

| System | Repository | Relationship |
|--------|-----------|--------------|
| PURL | `akadaan310/purl` | Programmable URLs (integration pending) |
| SE URL | `akadaan310/seurl` | Semantic URLs (integration pending) |
| MUSA | `akadaan310/MUSA` | Agent architecture (ingested as GIS records) |
| Browser Sidecar | `akadaan310/gis-browser-sidecar` | ChatGPT capture UI |

## Safe development instance

```bash
git clone https://github.com/akadaan310/gis-substrate
cd gis-substrate
python3 -m unittest tests.test_core  # no credentials needed for unit tests
# For API tests: set GIS_BASE_URL and GIS_TOKEN to your dev instance
```

## License

MIT — see LICENSE.
