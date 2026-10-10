# ChatGPT Invocation Path

## What's proven
- MCP tools work via stdio (local execution verified)
- PURL manifest publicly accessible at `/.well-known/purl`
- Write operations work with `GIS_WRITE_ENABLED=1` + valid bearer token

## What's required for ChatGPT
ChatGPT needs ONE of:
1. **MCP connection** via ChatGPT's custom MCP app flow + Secure Tunnel
   - User creates tunnel at platform.openai.com
   - Runs `tunnel-client` pointing to `gis-mcp-server`
   - Adds as custom MCP server in ChatGPT
   - See `docs/CHATGPT_GIS_SETUP.md`

2. **Direct HTTP** (not available)
   - ChatGPT cannot make arbitrary authenticated HTTP POSTs
   - This path is BLOCKED by platform limitations

## Blocker
**User-side MCP connection not yet configured.** The server side is complete.
ChatGPT cannot invoke GIS until the tunnel/MCP app is set up.

## Smallest remaining action
User creates the Secure Tunnel and connects it in ChatGPT (15 min, documented).
