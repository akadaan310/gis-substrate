# GIS MCP — Installation Guide

## One-command install

```bash
git clone <substrate-repo> && cd substrate/packages/gis-mcp && ./install.sh
```

## Configure credentials

```bash
export GIS_BASE_URL="https://files.abedkadaan.com"
export GIS_TOKEN="<your-token>"   # never commit this
```

Optional — enable writes:
```bash
export GIS_WRITE_ENABLED=1
```

## Claude Code

Add to `.mcp.json` (project) or `~/.claude.json` (user):

```json
{
  "mcpServers": {
    "gis": {
      "type": "stdio",
      "command": "gis-mcp-server",
      "env": {
        "GIS_BASE_URL": "https://files.abedkadaan.com",
        "GIS_TOKEN": "${GIS_TOKEN}"
      }
    }
  }
}
```

Restart Claude Code. Verify: ask "what GIS tools are available?" — you should see
`gis_bootstrap`, `gis_search`, `gis_get_record`, `gis_prepare_relay`,
`gis_validate_relay` (and write tools if enabled).

### Relay tools

- `gis_prepare_relay`: Build a context-complete relay from GIS records.
  Fetches referenced records, validates named terms are defined.
- `gis_validate_relay`: Check relay text for undefined references.
  Fails on unresolvable terms; warns on GIS-accessible (not included) refs.

Both are read-only. See `docs/RELAY_CONTRACT.md` for the full contract.

## Cursor

Settings → MCP → Add custom MCP server:

```json
{
  "gis": {
    "command": "gis-mcp-server",
    "env": {
      "GIS_BASE_URL": "https://files.abedkadaan.com",
      "GIS_TOKEN": "${GIS_TOKEN}"
    }
  }
}
```

## Health check

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | gis-mcp-server
```

Without `GIS_WRITE_ENABLED=1`, only 3 read tools appear.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `GIS_BASE_URL and GIS_TOKEN required` | Env not set | Export both vars |
| `HTTP 403` in tool results | Bad token | Verify token, no trailing whitespace |
| `base_url must use https://` | HTTP URL | Use https:// |
| Write tool rejected | `GIS_WRITE_ENABLED` not `1` | Export it deliberately |

## Uninstall

```bash
pip uninstall gis-mcp
# Then remove the "gis" entry from your MCP client config.
```
