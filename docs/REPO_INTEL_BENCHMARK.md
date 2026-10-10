# GIS Repository Intelligence — Retrieval Benchmark

6 questions. Answer using ONLY the GIS MCP tools (gis_bootstrap, gis_search, gis_get_record).
Do NOT use prior conversation knowledge. Cite the record ID for every factual claim.

## Questions

1. How does GIS retrieve and restore session continuity?
   Expected: bootstrap contract (read-only, idempotent), workspace context endpoint,
   protocols/tasks/decisions retrieval. Source: art_0iqtmisaie1t, clm_ojtoik7jwx7t.

2. Which API endpoint retrieves workspace context, and what are its authentication requirements?
   Expected: GET /api/v1/workspaces/{id}/context, Bearer token.
   Source: clm_807hrubcjj6z, clm_xszwnc4a2i1d.

3. Which behaviors are verified by tests versus merely described in documentation?
   Expected: 20 continuity tests pass; 16/16 acceptance checks; legacy /f/ byte-identical.
   Source: clm_kdej8ixvxn16, art_vvw1a49mbep0.

4. What are the known production operational risks?
   Expected: token cached at init (rotation needs restart); no new ports/systemd.
   Source: clm_xszwnc4a2i1d, clm_zbqahtjejf79.

5. Which repository commit supports the Substrate architectural claims?
   Expected: cdd39ae42e74ee11d1b4a508c7e69e36642c6b5b (gis-connectivity branch).
   Source: tsk_73y0foqfrxtr, claim meta.

6. What remains incomplete in the connectivity and ingestion workflow?
   Expected: remote MCP (ChatGPT) not built; ChatGPT mobile unconfirmed.
   Acceptable: "not in ingested records" if truly absent.

## Scoring

For each: correct (1), partial (0.5), wrong/missing (0), fabricated ( -1 ).
Report per-question score and total. Flag any invented details.
