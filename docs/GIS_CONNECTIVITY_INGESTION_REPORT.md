# GIS Connectivity + Knowledge Ingestion — Report

**Date:** 2026-10-09 ~23:00 EDT
**Branch:** `gis-connectivity` (commits `f820f88`, `2255148`)
**Status:** Staged. **NOT deployed to production.**

---

## Workstream A — Connectivity

### Bridge audit
- `gis_bridge.py` was uncommitted — now committed with fixes.
- Fixed: `https://` enforcement on base_url; `include_payload` for handoffs.
- Credential handling: token via env/constructor only, Bearer header only,
  never logged/printed. Verified in all test output.

### MCP target comparison

| Target | Transport | Auth | Status |
|--------|-----------|------|--------|
| **Local MCP (Claude Code, Cursor)** | stdio | Env vars | ✅ Prototype works. `.mcp.json` config documented. |
| **Remote MCP (ChatGPT)** | HTTP/SSE + OAuth | OAuth/DCR | ⚠️ ChatGPT supports MCP (since Mar 2025), but a remote GIS MCP server would need OAuth infrastructure — significant new work. Not built. |
| **Manual bootstrap** | N/A (docs) | Bearer | ✅ Works today. Contract v1 documented. |

**Critical limitation:** ChatGPT mobile app MCP support is unconfirmed.
A saved memory entry does NOT invoke tools. Do not assume otherwise.

### Recommendation
**Local MCP first** (Claude Code/Cursor via stdio — prototype tested and working),
**manual bootstrap as universal fallback** (documented, works today).
Defer remote MCP until OAuth infrastructure is justified.

### Prototype test evidence
- `initialize` → correct protocol version
- `tools/list` → 5 tools
- `gis_bootstrap` via MCP → production context retrieved (5 protocols, 5 tasks)

## Workstream B — Repository Registry

### Inventory (from memory + GIS records)
Explicitly identified: purl, seurl, golden-surface, MUSA, NetGovComEduGovOrgEduGovComNet,
substrateIO, pearl-foundry (all github.com/akadaan310/*), fieldprogrammingtheory (local-only).

### Pilot entries (no duplicates — searched before creating)

| ID | Repo | Commit | Evidence |
|----|------|--------|----------|
| `tsk_73y0foqfrxtr` | substrate | `f820f88` | `git rev-parse HEAD` verified |
| `tsk_s8jh8ggrta6h` | MUSA | `d7971357` | `git rev-parse HEAD` verified |

Each has: canonical URL (or explicit null), local path, purpose, branch, commit,
last-inspected timestamp, evidence source, confidence level.

### Boundaries preserved
- No credentials stored. Public repos indexed from public content.
- fieldprogrammingtheory not touched (local-only research boundary).

## Acceptance Criteria

- [x] Clean session bootstraps GIS and retrieves next action (verified in prior milestone)
- [x] Local MCP prototype exposes 5 tools, auth tested (403 without token)
- [x] Manual workflow documented and usable
- [x] Registry has no duplicates (searched before create)
- [x] Pilot entries have verifiable commits and provenance
- [x] No secrets in records, logs, or output (verified)
- [x] Production unchanged (all writes were data-level registry entries, no code/config)

## Recommended Next Milestone

**Remote MCP feasibility study:** Evaluate OAuth/DCR infrastructure cost for a
hosted GIS MCP server. Alternatively, package the local MCP server for
one-command install (`pip install gis-mcp` + `.mcp.json` template).
