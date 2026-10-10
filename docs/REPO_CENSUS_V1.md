# Master Repository Census
**Date:** 2026-10-10
**Method:** Local git inspection + GIS search

## Repositories in scope

| Repo | Local commit | Branch | GIS artifacts | GIS claims | Coverage |
|------|--------------|--------|---------------|------------|----------|
| substrate | 9f130e1 | gis-connectivity | 8 | ~15 | Core docs ingested |
| MUSA | d7971357 | master | 22 | ~45 | Genesis, strata, formal, master |
| purl | 3df4452a | (local) | 0 | 0 | Not ingested |
| seurl | 620ff955 | (local) | 0 | 0 | Not ingested |
| golden-surface | b1137187 | (local) | 0 | 0 | Not ingested |
| substrateIO | 7ace119a | (local) | 0 | 0 | Not ingested |
| NetGovComEduGovOrgEduGovComNet | 9fcf2e1d | (local) | 0 | 0 | Not ingested |
| pearl-foundry | f4fad26 | main (remote) | 0 | 0 | Not ingested |
| fieldprogrammingtheory | f443bb5 | (local-only) | 0 | 0 | Excluded (research boundary) |

## Ingested (complete)
- Substrate: 8 core docs (DESIGN, DEPLOY, SECURITY, ACCEPTANCE, bootstrap contract, bridge proposal, MCP README, threat model)
- MUSA: 3 luna-agent + 3 genesis + 8 strata + 2 master + 6 formal = 22 docs

## Partially covered
- None — pilot repos are either fully ingested (bounded set) or not started

## Missing (not ingested)
- purl, seurl, golden-surface, substrateIO, NetGovComEduGovOrgEduGovComNet: 0 docs each
- MUSA: theory/, protocols/ subdirectories beyond the 6 formal docs; strata subdirectories

## Inaccessible
- fieldprogrammingtheory: local-only, no remote; excluded per research boundary directive

## Estimated remaining batches
1. **Batch A (MUSA deep):** theory/ + protocols/ remaining files (~10 docs) — 1 batch
2. **Batch B (purl):** README + core docs (~5 docs) — 1 batch  
3. **Batch C (seurl):** README + NAI-CI research (~5 docs) — 1 batch
4. **Batch D (substrateIO, golden-surface, NetGov...):** inventory first, then bounded ingestion — 2 batches

Total estimate: ~5 batches to complete the census scope. No mass ingestion; each batch follows the manifest → ingest → benchmark pattern.
