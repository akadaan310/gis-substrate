"""Regression test: relay must not reference undefined Options A/C.

Scenario: Source context defines Options A, B, C. Receiving session has NOT
received those definitions. A relay mentioning A and C without explanation
MUST fail validation.
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from substrate.relay import prepare_relay, validate_relay

# The actual options from the ChatGPT-GIS decision
DEFINITIONS = {
    "Option A": "Native ChatGPT MCP: direct MCP server connection via Secure Tunnel",
    "Option B": "GIS Browser Sidecar: authenticated capture page (/s/capture/)",
    "Option C": "GIS-native AI workspace: full AI workspace using model API",
}


def test_undefined_refs_fail():
    """Relay mentioning A and C without definitions must FAIL."""
    result = prepare_relay(
        task="Continue with Options A and C",
        recipient="fresh session",
        record_ids=[],
        definitions={},  # NO definitions provided
        instructions="Do not start Options A or C until this release is stable.",
        acceptance_criteria=["A and C are stable"],
        next_action="Await user direction on A and C",
        fetch_records_fn=lambda rid: None,
    )
    v = result["validation"]
    assert not v["valid"], "Should FAIL: A and C are undefined"
    failed_refs = [f["ref"] for f in v["failures"]]
    assert "Option A" in failed_refs, f"A should fail, got {failed_refs}"
    assert "Option C" in failed_refs, f"C should fail, got {failed_refs}"
    print("PASS: undefined Options A/C correctly fail validation")


def test_defined_refs_pass():
    """Relay with included definitions must PASS."""
    result = prepare_relay(
        task="Continue with Options A and C",
        recipient="fresh session",
        record_ids=[],
        definitions=DEFINITIONS,
        instructions="Option B is deployed. Do not start Options A or C until stable.",
        acceptance_criteria=["A and C remain unstarted"],
        next_action="Monitor B stability",
        fetch_records_fn=lambda rid: None,
    )
    v = result["validation"]
    assert v["valid"], f"Should PASS with definitions: {v['failures']}"
    states = {r["ref"]: r["state"] for r in v["references"]}
    assert states.get("Option A") == "included"
    assert states.get("Option B") == "included"
    assert states.get("Option C") == "included"
    print("PASS: defined options pass validation")


def test_accessible_refs_warn():
    """References to GIS records (not included) produce warnings, not failures."""
    fake_records = {
        "dec_swm6vz3dv2ur": {"title": "DEPLOYED: GIS Browser Sidecar"},
    }
    v = validate_relay(
        "See dec_swm6vz3dv2ur for deployment details.",
        {},
        fake_records,
    )
    assert v["valid"], "Accessible refs should not fail"
    assert len(v["warnings"]) == 1
    assert v["warnings"][0]["ref"] == "dec_swm6vz3dv2ur"
    print("PASS: accessible refs warn (not fail)")


def test_stale_ids_fail():
    """References to non-existent records must fail."""
    v = validate_relay(
        "See tsk_nonexistent123 for details.",
        {},
        {},
    )
    assert not v["valid"]
    assert any("tsk_nonexistent123" in f["ref"] for f in v["failures"])
    print("PASS: stale/missing IDs fail validation")


def test_missing_records_reported():
    """Requested but unfetchable records are reported, not invented."""
    result = prepare_relay(
        task="Test task",
        recipient="test",
        record_ids=["ho_missing999"],
        definitions={},
        instructions="Do the thing.",
        acceptance_criteria=[],
        next_action="Done",
        fetch_records_fn=lambda rid: None,  # nothing found
    )
    assert not result["validation"]["valid"]
    assert "ho_missing999" in result["relay"]["missing_records"]
    print("PASS: missing records reported, not invented")


if __name__ == "__main__":
    test_undefined_refs_fail()
    test_defined_refs_pass()
    test_accessible_refs_warn()
    test_stale_ids_fail()
    test_missing_records_reported()
    print("\nALL RELAY REGRESSION TESTS PASS")
