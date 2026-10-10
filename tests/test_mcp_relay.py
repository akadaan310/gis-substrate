"""Tests for relay MCP tools: registration, validation, truncation, A/C regression."""

import json
import os
import subprocess
import sys

SERVER = os.path.join(os.path.dirname(__file__), "..", "packages", "gis-mcp",
                      "gis_mcp", "gis_mcp_server.py")
ENV = {"PATH": "/usr/bin:/bin", "GIS_BASE_URL": "https://x.example",
       "GIS_TOKEN": "y"}


def mcp_call(msgs):
    inp = "\n".join(json.dumps(m) for m in msgs)
    r = subprocess.run([sys.executable, SERVER], input=inp,
                       capture_output=True, text=True, env=ENV, timeout=15)
    out = {}
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        d = json.loads(line)
        if "id" in d:
            out[d["id"]] = d
    return out


def test_tools_registered():
    out = mcp_call([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])
    names = [t["name"] for t in out[1]["result"]["tools"]]
    assert "gis_prepare_relay" in names, f"missing: {names}"
    assert "gis_validate_relay" in names, f"missing: {names}"
    # Both must be read-only
    for t in out[1]["result"]["tools"]:
        if t["name"] in ("gis_prepare_relay", "gis_validate_relay"):
            assert t.get("annotations", {}).get("readOnlyHint") is True, \
                f"{t['name']} must be readOnlyHint"
    print("PASS: tools registered as read-only")


def test_validate_undefined_fails():
    out = mcp_call([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "gis_validate_relay",
                    "arguments": {"relay_text": "Proceed with Options A and C.",
                                  "definitions": {}}}},
    ])
    res = json.loads(out[1]["result"]["content"][0]["text"])
    assert res["valid"] is False
    refs = [f["ref"] for f in res["failures"]]
    assert "Option A" in refs and "Option C" in refs
    print("PASS: undefined A/C fails via MCP")


def test_validate_defined_passes():
    out = mcp_call([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "gis_validate_relay",
                    "arguments": {
                        "relay_text": "Proceed with Options A and C.",
                        "definitions": {
                            "Option A": "Native MCP",
                            "Option C": "AI workspace",
                            "GIS": "Golden Intelligence System",
                        }}}},
    ])
    res = json.loads(out[1]["result"]["content"][0]["text"])
    assert res["valid"] is True, f"failures: {res['failures']}"
    print("PASS: defined refs pass via MCP")


def test_prepare_relay_basic():
    out = mcp_call([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "gis_prepare_relay",
                    "arguments": {
                        "task": "Test task",
                        "recipient": "test session",
                        "record_ids": [],
                        "definitions": {"GIS": "Golden Intelligence System"},
                        "instructions": "Do the test task.",
                        "acceptance_criteria": ["done"],
                        "next_action": "Report",
                    }}},
    ])
    res = json.loads(out[1]["result"]["content"][0]["text"])
    assert "relay" in res and "validation" in res
    assert res["validation"]["valid"] is True
    print("PASS: prepare_relay via MCP")


def test_truncation_handling():
    # Very long relay text should not crash
    long_text = "Proceed with Options A and C. " * 1000
    out = mcp_call([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "gis_validate_relay",
                    "arguments": {"relay_text": long_text,
                                  "definitions": {}}}},
    ])
    res = json.loads(out[1]["result"]["content"][0]["text"])
    assert res["valid"] is False  # still catches undefined refs
    print("PASS: long text handled")


if __name__ == "__main__":
    test_tools_registered()
    test_validate_undefined_fails()
    test_validate_defined_passes()
    test_prepare_relay_basic()
    test_truncation_handling()
    print("\nALL MCP RELAY TESTS PASS")
