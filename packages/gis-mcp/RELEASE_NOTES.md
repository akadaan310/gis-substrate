# GIS Local MCP v0.1.0 — Release Notes

**Commit:** `bb68c14` (branch `gis-connectivity`)
**Date:** 2026-10-09

## Install

```bash
git clone <repo> && cd substrate/packages/gis-mcp && ./install.sh
export GIS_BASE_URL="https://files.abedkadaan.com"
export GIS_TOKEN="<token>"
```

(Note: `--break-system-packages` may be needed on Debian/Ubuntu.)

## What it does

Local stdio MCP server exposing 5 GIS tools. Read-only by default;
writes require `GIS_WRITE_ENABLED=1`.

## Test results: 7/7 PASS

- MCP init + tool discovery (3 read tools by default)
- Authenticated bootstrap against production (5 protocols retrieved)
- Write rejection when disabled
- 5 tools visible when enabled
- Bad token → safe 403, no crash
- HTTP URLs rejected

## Client support

- Claude Code: `.mcp.json` (tested format)
- Cursor: MCP settings (documented format)

## Known limitations

- Remote MCP (ChatGPT) requires OAuth infrastructure — not built
- ChatGPT mobile MCP support unconfirmed
- No Windows testing

## Uninstall

```bash
pip uninstall gis-mcp
# Remove "gis" from MCP client config
```

## Rollback

Delete the package directory and uninstall. No production changes were made;
removing the client config fully disables the integration.
