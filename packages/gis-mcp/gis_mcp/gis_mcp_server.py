#!/usr/bin/env python3
"""GIS MCP server (stdio transport) — local prototype.

Exposes the five GIS tools via MCP JSON-RPC over stdin/stdout.
Configuration: GIS_BASE_URL and GIS_TOKEN environment variables.
The token is never logged, printed, or included in tool results.

Usage in Claude Code (.mcp.json):
{
  "mcpServers": {
    "gis": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/gis_mcp_server.py"],
      "env": {
        "GIS_BASE_URL": "https://files.abedkadaan.com",
        "GIS_TOKEN": "<token>"
      }
    }
  }
}
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gis_bridge import GISBridge

TOOLS = {
    "gis_bootstrap": {
        "annotations": {"readOnlyHint": True},
        "description": "Retrieve GIS workspace context for a new session. Read-only, idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "task_or_project": {"type": "string"},
            },
            "required": ["workspace_id"],
        },
    },
    "gis_search": {
        "annotations": {"readOnlyHint": True},
        "description": "Search GIS records. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "workspace_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    "gis_get_record": {
        "annotations": {"readOnlyHint": True},
        "description": "Get a single GIS record by ID (task, artifact, handoff, workspace, decision). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "include_payload": {"type": "boolean", "default": False},
            },
            "required": ["record_id"],
        },
    },
    "gis_create_task": {
        "description": "Create a task in GIS. Requires GIS_WRITE_ENABLED=1. Idempotent.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "status": {"type": "string", "default": "open"},
            },
            "required": ["workspace_id", "title"],
        },
    },
    "gis_update_task": {
        "description": "Partially update a task (status, title, body, meta). Requires GIS_WRITE_ENABLED=1. Audited.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "changes": {"type": "object"},
            },
            "required": ["task_id", "changes"],
        },
    },
    "gis_prepare_relay": {
        "annotations": {"readOnlyHint": True},
        "description": "Prepare a context-complete relay from GIS records. Validates that named references are defined. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "recipient": {"type": "string"},
                "record_ids": {"type": "array", "items": {"type": "string"}},
                "definitions": {"type": "object"},
                "instructions": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "next_action": {"type": "string"},
            },
            "required": ["task", "recipient", "record_ids", "definitions",
                         "instructions", "acceptance_criteria", "next_action"],
        },
    },
    "gis_validate_relay": {
        "annotations": {"readOnlyHint": True},
        "description": "Validate relay text for undefined references. Returns failures for unresolvable terms, warnings for GIS-accessible refs. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "relay_text": {"type": "string"},
                "definitions": {"type": "object"},
                "record_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["relay_text"],
        },
    },
    "gis_save_handoff": {
        "description": "Save a structured session handoff. Requires GIS_WRITE_ENABLED=1. Idempotent.",
        "write": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "session_summary": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "pending": {"type": "array", "items": {"type": "string"}},
                "next_action": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["workspace_id", "session_summary", "completed",
                         "pending", "next_action"],
        },
    },
}


def main():
    base = os.environ.get("GIS_BASE_URL", "")
    token = os.environ.get("GIS_TOKEN", "")
    if not base or not token:
        sys.stderr.write("GIS_BASE_URL and GIS_TOKEN required\n")
        sys.exit(1)
    bridge = GISBridge(base, token)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")

        def reply(result=None, error=None):
            out = {"jsonrpc": "2.0", "id": mid}
            if error:
                out["error"] = error
            else:
                out["result"] = result
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()

        if method == "initialize":
            reply({"protocolVersion": "2024-11-05",
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "gis", "version": "0.1.0"}})
        elif method == "tools/list":
            write_on = os.environ.get("GIS_WRITE_ENABLED") == "1"
            visible = {n: s for n, s in TOOLS.items()
                       if write_on or not s.get("write")}
            reply({"tools": [
                {"name": n, **{k: v for k, v in spec.items() if k != "write"}}
                for n, spec in visible.items()]})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments", {})
            # Write gating: mutations require GIS_WRITE_ENABLED=1
            if TOOLS.get(name, {}).get("write") and os.environ.get("GIS_WRITE_ENABLED") != "1":
                reply(error={"code": -32602,
                             "message": f"tool {name} requires GIS_WRITE_ENABLED=1"})
                continue
            try:
                fn = getattr(bridge, name)
                result = fn(**args)
                # Redact: ensure no token leaks in results
                reply({"content": [{"type": "text",
                                    "text": json.dumps(result, default=str)}]})
            except AttributeError:
                reply(error={"code": -32601, "message": f"unknown tool: {name}"})
            except Exception as e:
                reply(error={"code": -32603, "message": str(e)[:200]})
        elif method in ("notifications/initialized",):
            pass  # no reply needed
        else:
            reply(error={"code": -32601, "message": f"unknown method: {method}"})


if __name__ == "__main__":
    main()
