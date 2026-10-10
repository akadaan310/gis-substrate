#!/bin/bash
# GIS MCP one-command installer.
# Usage: ./install.sh
# Requires: Python 3.10+, pip
set -e
echo "=== GIS MCP installer v0.1.0 ==="
# 1. Python version check
if ! python3 -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
  echo "ERROR: Python 3.10+ required. Found: $(python3 --version 2>&1)"
  exit 1
fi
echo "Python: $(python3 --version)"
# 2. Install package (no third-party deps)
pip install -e "$(dirname "$0")"
echo "Installed gis-mcp."
# 3. Configuration
echo ""
echo "Configuration: set these environment variables (never commit them):"
echo "  export GIS_BASE_URL=https://files.abedkadaan.com"
echo "  export GIS_TOKEN=<your-token>"
echo ""
echo "Optional (enables gis_update_task and gis_save_handoff):"
echo "  export GIS_WRITE_ENABLED=1"
echo ""
# 4. Validate (without printing token)
echo "=== validating ==="
if [ -z "$GIS_BASE_URL" ] || [ -z "$GIS_TOKEN" ]; then
  echo "SKIP: GIS_BASE_URL and GIS_TOKEN not set. Set them, then run:"
  echo "  gis-mcp-server --check"
  exit 0
fi
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | gis-mcp-server | head -1
echo "Server responds. Run a health check with your client or:"
echo "  echo '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}' | gis-mcp-server"
echo ""
echo "=== uninstall ==="
echo "  pip uninstall gis-mcp"
echo "  Remove the MCP server entry from your client's config."
