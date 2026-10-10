# Deployment Plan: GIS Browser Sidecar

## Proposed commit
`8f69d802` on branch `gis-connectivity`

## Target
Production: `/home/azureuser/substrate/` on koda-vm
(via existing deploy mechanism)

## Changes in this release
- `substrate/ui.py`: New `/s/capture/` page (form + preview + approve)
- `docs/SIDECAR_ARCHITECTURE.md`: Architecture decision
- `docs/CHATGPT_GIS_SETUP.md`: Tunnel setup guide
- `packages/gis-mcp/`: readOnlyHint annotations

## Pre-deployment backup
```bash
# On koda-vm, before deploying:
cp -r /home/azureuser/substrate /home/azureuser/backup-sidecar-$(date +%Y%m%d-%H%M)
cp /home/azureuser/substrate/substrate.db /home/azureuser/backup-sidecar-*/substrate.db
```

## Deployment steps
1. Verify backup exists and is readable
2. Deploy via existing mechanism (same as continuity cutover)
3. Restart service
4. Smoke test: GET /s/capture/ → 200 (authenticated)
5. Smoke test: GET /s/capture/ → 403 (unauthenticated)
6. Verify existing routes unaffected: /s/, /api/v1/handoffs, /api/v1/tasks

## Rollback
```bash
# Restore from backup:
systemctl stop substrate  # or equivalent
rm -rf /home/azureuser/substrate
cp -r /home/azureuser/backup-sidecar-YYYYMMDD-HHMM /home/azureuser/substrate
systemctl start substrate
# Verify: GET /s/ → 200
```

## Risk assessment
- **Low risk**: Additive change only (new route + NAV entry)
- No schema changes, no API changes, no auth changes
- Existing routes untouched (verified by 106 passing tests)
- Rollback is file restore (no migrations to reverse)

## NOT included
- No GitHub publication (awaiting destination approval)
- No production deployment (awaiting explicit approval)
