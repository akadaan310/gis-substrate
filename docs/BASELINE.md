# Baseline Report — existing file system (koda-vm, inspected 2026-10-09)

Source of truth: `/home/azureuser/upload-tool/server.py` (408 lines, v1.1), read in full on the VM.
No inference from the UI alone.

## 1. Architecture and runtime

- Single-process CPython 3.10, stdlib-only (`http.server.ThreadingHTTPServer`, `cgi` for multipart).
- systemd unit `upload-tool.service`: `Type=simple`, `User=azureuser`,
  `ExecStart=/usr/bin/python3 /home/azureuser/upload-tool/server.py`, `Restart=always`.
- Listens on `0.0.0.0:8123` (PORT env override). Caddy v2.11.7 (`caddy.service`)
  reverse-proxies `files.abedkadaan.com` → `127.0.0.1:8123`, auto Let's Encrypt.
  Azure NSG rules `files-http-80` (1016), `files-https-443` (1017).
- No git repo, no tests, no backups documented, no metrics/logging beyond systemd.

## 2. Storage

- `files/` — flat directory, 41 MB at inspection (~15 objects). Filenames are
  `<uuid4hex><ext>`, e.g. `10b9466593844d1da78cf12db6d1d999.jpg`.
- `index.jsonl` — append-only JSON lines: `{id, name, size, time, type}`.
  `load_index()` backfills any on-disk file missing from the index and then
  **rewrites the whole file** (racy under the threaded server, low risk in practice).
- Uploads are written to `<name>.part` then `os.replace()`-renamed (atomic), chmod 644.
- `.token` (mode 600) holds the single shared secret. `server.py.bak1` (v1.0) kept beside it.

## 3. Links and identifiers

- Public: `GET /f/<32hex>.<ext>` — no auth, `Cache-Control: public, max-age=31536000, immutable`.
  Filename regex restricts to an allow-list of extensions.
- Token-gated (`?token=` compared with `hmac.compare_digest`): `/` (upload page + gallery),
  `/list` (JSON history), `/upload` (multipart POST).
- Public base URL is derived from the `Host` header (or `PUBLIC_BASE` env), so links
  generated behind Caddy are correct (`https://files.abedkadaan.com/f/...`).
- **Link model, stated plainly:** a `/f/` link is public to anyone who holds it. It is not
  private, signed, expiring, or revocable. There is no per-file ACL.

## 4. AuthN/AuthZ

- One shared bearer secret in the query string. Also accepted: nothing else.
- Risks: token appears in proxy/server logs and browser history; single secret, no rotation
  procedure, no per-user identity, no audit log.

## 5. Limits

- 25 MB per file, max 10 files per request, total body cap `10*25MB + 10MB`.
- Extension allow-list (~40 types: images, video, audio, docs, fonts, archives).
- No rate limiting, no quotas, no expiry/deletion policy.

## 6. Confirmed capabilities (preserve these)

- Upload → immediate public link; multi-file; gallery history with thumbnails for images.
- Existing `/f/` URLs keep working; files stay byte-identical where they are.
- Token gate on management routes.

## 7. Risks / gaps (why the substrate is needed)

- No database: no search, no metadata queries, no relationships, no provenance.
- `cgi` module is deprecated (removed in Python 3.13) — upload parsing will break on upgrade.
- No integrity verification (no hashes), no deduplication, no versioning.
- No extraction (PDF text, OCR, transcripts), no thumbnails except browser-side `<img>`.
- No access control granularity; no audit trail; no export/restore story.
- Single-threaded-index rewrite race; no observability.

## 8. Migration strategy (additive, non-destructive)

1. Leave `server.py`, `files/`, `index.jsonl`, and all `/f/` URLs exactly as they are.
2. Build the substrate as a new package mounted in the same process under `/s/` + `/api/v1/`.
3. Index legacy files **in place**: hash each file, create artifact records with
   `visibility='public-link'`, map `legacy_file.legacy_id → artifact_id`.
   No file is moved, renamed, or deleted. The old `/f/` route keeps serving them.
4. New substrate uploads go to a content-addressed blob store (`blobs/xx/yy/<sha256>`);
   identical bytes are stored once; distinct provenance records are never merged.
5. Cutover to production only at an explicit authorization checkpoint, after a dry-run
   migration against a full copy of the data and a localhost-bound staging instance.
