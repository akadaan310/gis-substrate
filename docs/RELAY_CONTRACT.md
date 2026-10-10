# Relay Contract v1

## Purpose
Ensure a receiving agent never has to guess what an unexplained reference means.

## Tools

### gis_prepare_relay
Builds a self-contained relay from a task, recipient, GIS record IDs,
definitions, instructions, acceptance criteria, and next action.

**Inputs:**
- `task`: what needs doing
- `recipient`: who it's for (description)
- `record_ids`: GIS record IDs the relay depends on
- `definitions`: dict of term → definition (included in relay)
- `instructions`: explicit instructions
- `acceptance_criteria`: list of strings
- `next_action`: concrete next step

**Outputs:**
- `relay`: the prepared relay with validation results
- `validation`: `{valid, references, failures, warnings}`

**Behavior:**
- Fetches each `record_id` via existing GIS retrieval
- Missing records are listed in `missing_records`, not invented
- Validation runs automatically on the assembled relay

### gis_validate_relay
Checks relay text for undefined references.

**Inputs:**
- `relay_text`: the text to validate
- `definitions`: dict of term → definition (optional)
- `record_ids`: GIS record IDs that are accessible (optional)

**Outputs:**
- `valid`: boolean
- `references`: list of `{ref, state, detail}` where state is one of:
  - `included`: defined in the relay
  - `accessible`: in GIS with explicit ID (recipient must fetch)
  - `unknown`: no definition, no record
- `failures`: unresolvable references (must fix)
- `warnings`: accessible-but-not-included refs (recipient must fetch)

## Context states
A record existing in GIS is NOT proof it was delivered. The four states
(included / accessible / retrieved / unknown) must be distinguished.
"Retrieved" requires delivery evidence not yet tracked.

## Security
- Both tools are read-only (`readOnlyHint: true`)
- No writes, no write-gate changes
- Uses existing GIS authentication
- Never invents missing context
