# Deployment plan — substrate on koda-vm

## What changes in production (and what does not)

CHANGES (all additive, all reversible):
1. New directory `/home/azureuser/substrate/` holding the `substrate` package,
   `substrate.db` (SQLite), and `blobs/` (content-addressed store).
2. `server.py` gains an additive patch (see `deploy/server_patch.py`): requests whose
   path starts with `/s/` or `/api/v1/` are delegated to the substrate app; every
   other path behaves exactly as today. The patch shares the existing `.token`
   secret — no new credential.
3. `systemd` unit `upload-tool` is restarted once to load the patch.

DOES NOT CHANGE:
- `files/`, `index.jsonl`, and every existing `/f/<id>` URL (byte-identical, same route).
- The `/`, `/list`, `/upload` routes and their auth.
- Caddy config, NSG rules, firewall, DNS. No new ports, no new public services.

## Environment overrides (staging / testing only)

The generated patch reads, in order: `SUBSTRATE_DIR` (code location, default
`/home/azureuser/substrate`), `SUBSTRATE_DATA_DIR` (DB + blobs, defaults to
`SUBSTRATE_DIR` — the production layout keeps code and data together),
`SUBSTRATE_TOKEN_FILE` (default `/home/azureuser/upload-tool/.token`).
Production needs none of these set. Staging used all three to isolate code,
data, and token from production.

## Pre-cutover verification (done before asking for approval)

1. Full test suite passes locally (`tests/`).
2. Acceptance criteria A–L pass on an isolated instance (see `docs/ACCEPTANCE.md`).
3. Benchmarks recorded (`docs/BENCHMARKS.md`).
4. Migration dry-run on the VM against a **copy** of `/home/azureuser/upload-tool`:
   - `rsync -a /home/azureuser/upload-tool/ /home/azureuser/stage-upload-copy/`
   - run `substrate/migrate.py --legacy-dir stage-upload-copy/files
     --index stage-upload-copy/index.jsonl --data-dir /home/azureuser/stage-substrate/`
   - verify: every legacy id resolves, every `/f/` URL still served by old code path,
     hashes match, row counts reconcile with index.jsonl.
5. Staging instance on the VM bound to `127.0.0.1:8124` (not exposed) serving the
   migrated copy; exercised via `curl` through SSH.

## Cutover steps (require explicit user approval — see below)

1. `cp -r` the reviewed `substrate/` package to `/home/azureuser/substrate/`.
2. Apply `deploy/server_patch.py` to `server.py` (keeps a `.pre-substrate` backup).
3. Syntax-check + import-check the patched server.
4. Run the real migration (in-place indexing; moves nothing, deletes nothing):
   `python3 -m substrate.migrate --legacy-dir /home/azureuser/upload-tool/files ...`
5. `systemctl restart upload-tool`; verify `/`, `/list`, `/f/<known-id>` still work
   and `/s/?token=` + `/api/v1/health` answer.
6. Rollback: restore `server.py.pre-substrate`, restart. The substrate DB/blobs are
   inert files; nothing in the old path depends on them.

## Approval checkpoint (do not proceed without the user's explicit go-ahead)

- [ ] Restarting the `upload-tool` systemd service (brief downtime of files.abedkadaan.com, seconds).
- [ ] Writing new files under `/home/azureuser/substrate/` on the VM.
- [ ] Running the migration indexer against the live `files/` directory
      (read-only w.r.t. existing files; writes only new DB + blobs).

## Backups

- The substrate DB + blobs are plain files: back up `/home/azureuser/substrate/`
  (excluding nothing; blobs are content-addressed and stable).
- `GET /api/v1/export` produces a portable bundle any time; restore is tested
  (see `docs/ACCEPTANCE.md` criterion J).

## Token rotation

The substrate reuses the upload tool's `.token`. To rotate: write the new secret to
`/home/azureuser/upload-tool/.token` (mode 600) and restart the service. Both the
old UI and the substrate pick it up on start. There is no per-user identity; the
secret is a single owner bearer token — see `docs/SECURITY.md` for the honest
limitations.
