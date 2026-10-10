# Connect ChatGPT to GIS via Secure MCP Tunnel

## What this does
Gives ChatGPT live read access to your GIS (bootstrap, search, records)
without exposing your server to the public internet.

## Prerequisites (your action required)
1. **OpenAI Platform account** with permission to create tunnels
2. **ChatGPT plan**: Pro or higher for read access; Business/Enterprise/Edu for write access
3. Developer mode enabled in your ChatGPT workspace

## Step 1: Create the tunnel (5 min, in browser)
1. Go to https://platform.openai.com → Tunnel settings
2. Create a new tunnel
3. Associate it with your ChatGPT workspace
4. Note the `tunnel_id` and download `tunnel-client`

## Step 2: Run tunnel-client (on koda-vm)
```bash
# One-time setup
export CONTROL_PLANE_API_KEY="<your-platform-api-key>"
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile gis \
  --tunnel-id <tunnel_id> \
  --mcp-command "/usr/local/bin/gis-mcp-server"

# Run (keep alive via systemd)
export GIS_BASE_URL="https://files.abedkadaan.com"
export GIS_TOKEN="<your-gis-token>"
tunnel-client run --profile gis
```

## Step 3: Add to ChatGPT (2 min, in browser)
1. Go to chatgpt.com → Plugins → Add custom MCP server
2. Select **Tunnel**, paste your `tunnel_id`
3. Name it "GIS" 
4. Create as plugin, then install it

## Step 4: Test
In a fresh chat, type `@GIS` and ask:
> "Bootstrap from workspace ws_y3a9agw0eqzj and report the latest handoff ID and next action."

Expected: ChatGPT calls `gis_bootstrap` and returns the live handoff.

## Security notes
- Your GIS token stays on the VM, never in ChatGPT
- Tunnel is outbound-only from your VM to OpenAI
- Read tools are marked readOnlyHint (no confirmation prompts)
- Write tools require GIS_WRITE_ENABLED=1 on the server (currently off)
- Write actions in ChatGPT require your confirmation by default

## Write access (later)
Requires ChatGPT Business/Enterprise/Edu plan. When ready:
1. Set GIS_WRITE_ENABLED=1 for the tunnel profile
2. Refresh the app's tool list in ChatGPT
3. Test with a harmless handoff save
