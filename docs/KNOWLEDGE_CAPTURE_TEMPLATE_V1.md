# Knowledge-Capture Record Template v1
## (SUBSTRATE-T04 deliverable, per P02)

### Purpose
Standard fields for preserving a discovery so a clean session can recover it:
what was found, where it came from, how we know, what was decided, what's uncertain,
and what to do next.

### Record types and fields

#### 1. SOURCE (GIS: artifact)
| Field | Required | Notes |
|-------|----------|-------|
| repository | yes | repo name + canonical URL or local path |
| commit | yes | immutable SHA at inspection time |
| path | yes | document path within repo |
| section | no | heading, section, or line range |
| content_hash | yes | sha256 of ingested content |
| classification | no | axiom, definition, protocol, tested, etc. |

#### 2. CLAIM (GIS: claim)
| Field | Required | Notes |
|-------|----------|-------|
| statement | yes | precise, faithful to source |
| semantic | yes | axiom, definition, derivation, invariant, normative, tested, interpretation, hypothesis, unresolved |
| verification_status | yes | supported, unverified, contested (per API vocabulary) |
| source_artifact | yes | artifact ID |
| section | no | where in the source |
| quote_vs_paraphrase | no | mark exact quotes distinctly |

#### 3. EVIDENCE (GIS: claim body, artifact, or task meta)
| Field | Required | Notes |
|-------|----------|-------|
| type | yes | test_output, source_excerpt, read_back, observation |
| content | yes | actual evidence, not summary of evidence |
| timestamp | yes | when obtained |

#### 4. DECISION (GIS: decision)
| Field | Required | Notes |
|-------|----------|-------|
| decision | yes | what was decided |
| rationale | yes | why, with evidence refs |
| alternatives | yes | what was considered and rejected, why |
| scope_excluded | no | what was deliberately not done |
| acceptance_criteria | no | how to verify |
| rollback | no | how to undo |

#### 5. UNCERTAINTY (GIS: claim with semantic=unresolved, or decision meta)
| Field | Required | Notes |
|-------|----------|-------|
| gap | yes | what is not known |
| blocking | no | does it block action? |
| tripwire | no | what future event would resolve it |

#### 6. RELATIONSHIPS (GIS: edge)
Use typed edges: `derives_from`, `documents`, `implements`, `supports`,
`supersedes`, `references`. Never create an edge the source doesn't support.

#### 7. NEXT ACTION (GIS: handoff expected_json.next_action + task)
| Field | Required | Notes |
|-------|----------|-------|
| action | yes | concrete, single step |
| owner_task | no | task ID it belongs to |

### Rules
1. Do not require every field. Mark inapplicable fields optional, never invented.
2. Keep observations separate from conclusions. Label interpretations.
3. Never upgrade a reported result to a verified fact without new evidence.
4. Preserve source identifiers verbatim (commit SHAs, record IDs, paths).
5. One claim per proposition. Do not collapse distinct statements.
