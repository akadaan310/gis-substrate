# Substrate benchmarks

Measured on the local dev VM (SQLite 3.37.2, CPython 3.10), synthetic data,
100k scale run with `TMPDIR` on a real disk (`/tmp` is a 512MB tmpfs — the
100k run fills it; that is an environment limit, not a product limit).

Raw numbers: `docs/BENCHMARKS.json`.

## Results

| scale | load | db | metadata page (50) | exact id | FTS common (25 hits) | graph depth-2 | context package |
|---|---|---|---|---|---|---|---|
| 1k | 3.3s (full pipeline) | 1.3MB | 0.3ms | 0.27ms | 2.3ms | 0.8ms (18 nodes) | 9.7ms (22 sources) |
| 10k | 59.9s (full pipeline) | 10MB | 0.2ms | 0.21ms | 2.8ms | 0.6ms (12 nodes) | 19.2ms (22 sources) |
| 100k | 77.7s (bulk path) | 525MB | 75.3ms | 11.9ms | 384ms | 0.7ms (12 nodes) | 953ms (50 sources) |

Notes:

- **Load**: full pipeline = artifact record + blob write + dedup + revision +
  initial extraction job + FTS index, per artifact. Bulk path used at 100k
  skips per-artifact blob writes for speed; FTS bulk-inserted.
- **metadata page** at 100k (75.3ms) is cold page-cache on a 525MB DB; the
  query plan uses `idx_artifact_created` and re-queries at 0.2ms warm
  (verified separately at 60k). A composite index on
  `(created_at DESC, id DESC)` would remove the "RIGHT PART OF ORDER BY"
  temp B-tree; judged not worth it for v1.
- **exact id** at 100k (11.9ms) is likewise cold-cache; warm it is
  sub-millisecond.
- **FTS common** at 100k (384ms): BM25 ranking over 100k documents with a
  high-frequency term. Bounded to 25 hits. Acceptable for interactive use;
  if it ever matters, FTS5 `rank` windows or trigram indexes are the next
  lever.
- **Graph traversal** stays sub-millisecond at all scales because
  `idx_edge_src` / `idx_edge_dst` serve both directions (multi-index OR plan).
- **Context package** at 100k (953ms, 50 sources) is dominated by the FTS
  ranking + per-source fetch of excerpts; still interactive.

## Query plans (representative, 100k)

- FTS: `SCAN search_fts VIRTUAL TABLE INDEX 0:M4` + temp B-tree for ORDER BY.
- Traversal: `MULTI-INDEX OR` → `SEARCH edge USING INDEX idx_edge_src` /
  `SEARCH edge USING INDEX idx_edge_dst` + temp B-tree for ordered output.

## Scaling expectations

- Metadata, exact lookup, and graph traversal: flat with index use.
- FTS: grows with term frequency; rare terms stay sub-millisecond.
- Load: linear per artifact; full pipeline ~6ms/artifact at 10k.
- DB: ~5.2KB/artifact at 100k (dominated by the 300-word synthetic FTS
  bodies; real artifacts will be smaller per row on average).
