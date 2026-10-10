"""
GIS Relay: context-complete handoff preparation and validation.

Motivating failure: A relay referenced "Options A and C" without defining them.
The originating session knew the meanings; the recipient did not.

This module ensures a receiving agent never has to guess what an unexplained
reference means.

Context states (must be distinguished):
- included: actually present in the relay payload
- accessible: exists in GIS, recipient can fetch via tools (with explicit ID)
- retrieved: evidence the recipient actually fetched it
- unknown: no evidence either way

A record existing in GIS is NOT proof it was delivered to a recipient.
"""

import re

# Patterns that indicate a named reference requiring definition
REF_PATTERNS = [
    re.compile(r'\b[Oo]ptions?\s+([A-Z])\b'),          # Option A, Options A/C
    re.compile(r'\b([A-Z]{2,})\b'),                     # ALL-CAPS terms (T04, P02)
    re.compile(r'\b(tsk|clm|dec|ho|art)_[a-z0-9]+\b'), # GIS record IDs
]


def extract_references(text):
    """Find all named references in relay text that may need definitions."""
    refs = set()
    text = text or ""
    # Handle "Option(s) A", "Options A and C", "Options A, B, or C"
    for m in re.finditer(r'\b[Oo]ptions?\s+([A-Z](?:\s*(?:,|and|or)\s*[A-Z])*)', text):
        letters = re.findall(r'[A-Z]', m.group(1))
        for letter in letters:
            refs.add(f"Option {letter}")
    for pat in REF_PATTERNS[1:]:  # skip the Option pattern (handled above)
        for m in pat.finditer(text):
            refs.add(m.group(0))
    return sorted(refs)


def classify_context(ref, included_defs, gis_records):
    """
    Classify a reference into one of four context states.
    Returns (state, detail).
    """
    # 1. Included: defined directly in the relay
    if ref in included_defs:
        return ("included", included_defs[ref])
    # 2. Accessible: exists in GIS with explicit retrievable ID
    if ref in gis_records:
        rec = gis_records[ref]
        return ("accessible", {"id": ref, "title": rec.get("title", ""),
                               "fetch_via": "gis_get_record"})
    # 3/4. Retrieved vs unknown requires delivery evidence (not available here)
    return ("unknown", {"reason": "no definition in relay, no GIS record found"})


def validate_relay(relay_text, included_definitions, gis_records):
    """
    Validate that all named references in a relay are resolvable.

    Args:
        relay_text: the instruction text for the recipient
        included_definitions: dict of term -> definition included in relay
        gis_records: dict of record_id -> {title, ...} for accessible records

    Returns:
        {
            "valid": bool,
            "references": [{"ref": str, "state": str, "detail": dict}],
            "failures": [{"ref": str, "reason": str}],
            "warnings": [{"ref": str, "reason": str}],
        }
    """
    refs = extract_references(relay_text)
    results = []
    failures = []
    warnings = []

    for ref in refs:
        state, detail = classify_context(ref, included_definitions, gis_records)
        results.append({"ref": ref, "state": state, "detail": detail})
        if state == "unknown":
            failures.append({
                "ref": ref,
                "reason": f"Reference '{ref}' is not defined in the relay "
                          f"and no GIS record was provided. The recipient "
                          f"cannot resolve this.",
            })
        elif state == "accessible":
            warnings.append({
                "ref": ref,
                "reason": f"Reference '{ref}' is accessible via GIS "
                          f"(record {detail['id']}) but not included. "
                          f"Recipient must fetch it.",
            })

    return {
        "valid": len(failures) == 0,
        "references": results,
        "failures": failures,
        "warnings": warnings,
    }


def prepare_relay(task, recipient, record_ids, definitions, instructions,
                  acceptance_criteria, next_action, fetch_records_fn=None):
    """
    Prepare a self-contained relay.

    Args:
        task: task description
        recipient: intended recipient description
        record_ids: GIS record IDs the relay depends on
        definitions: dict of term -> definition to include
        instructions: explicit instructions text
        acceptance_criteria: list of criteria
        next_action: concrete next step
        fetch_records_fn: optional fn(id) -> record or None (for validation)

    Returns:
        {"relay": dict, "validation": dict}
    """
    gis_records = {}
    missing = []
    if fetch_records_fn:
        for rid in record_ids:
            rec = fetch_records_fn(rid)
            if rec:
                gis_records[rid] = rec
            else:
                missing.append(rid)

    relay = {
        "task": task,
        "recipient": recipient,
        "instructions": instructions,
        "definitions": definitions,
        "record_refs": record_ids,
        "acceptance_criteria": acceptance_criteria,
        "next_action": next_action,
        "missing_records": missing,
    }

    # Validate: check all references in instructions + task
    full_text = f"{task}\n{instructions}\n{next_action}"
    validation = validate_relay(full_text, definitions, gis_records)
    if missing:
        validation["failures"].extend([
            {"ref": rid, "reason": f"Record '{rid}' was requested but not found."}
            for rid in missing
        ])
        validation["valid"] = False

    relay["validation"] = validation
    return {"relay": relay, "validation": validation}
