# GIS MCP — Permission Matrix & Threat Model v0.1.0

## Permission Matrix

| Tool | Read/Write | Default | Gated by |
|------|------------|---------|----------|
| `gis_bootstrap` | Read | ✅ Enabled | — |
| `gis_search` | Read | ✅ Enabled | — |
| `gis_get_record` | Read | ✅ Enabled | — |
| `gis_update_task` | Write | ❌ Disabled | `GIS_WRITE_ENABLED=1` |
| `gis_save_handoff` | Write | ❌ Disabled | `GIS_WRITE_ENABLED=1` |

Write tools are hidden from `tools/list` when disabled.

## What the bridge CANNOT do

- Arbitrary shell execution — no such tool exists
- Arbitrary HTTP requests — only the configured GIS base URL
- Unrestricted DB access — only the five GIS API endpoints
- Credential exfiltration — token never in results, logs, or URLs

## Threat Model

| Threat | Likelihood | Impact | Mitigation |
|--------|------------|--------|------------|
| Token in shell history | Medium | High | Docs say use env vars; installer warns |
| Malicious prompt exfiltrates token | Low | High | Bridge never returns token in results |
| Model overwrites decisions | Low | Medium | Decisions are append-only; no update tool |
| SSRF via base_url | Low | Medium | `https://` enforced; no user-supplied URLs in tools |
| Duplicate handoffs | Low | Low | Deterministic idempotency keys |
| Retrieved data as instructions | Medium | Medium | Docs label retrieved content as untrusted data |

## Credential Rules

1. Token via `GIS_TOKEN` env var only. Never in files, args, URLs, or logs.
2. `GIS_WRITE_ENABLED=1` is a deliberate, per-session opt-in.
3. The bridge validates `https://` — no plaintext HTTP.
