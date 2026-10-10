# Security & access-control behavior

## Link model — stated plainly

| Mechanism | Who can open | Auth | Expiry | Revocable | Logged |
|---|---|---|---|---|---|
| Legacy `/f/<32hex>.<ext>` | anyone holding the URL | none | no | no | no |
| Substrate `public-link` artifact (`/s/content/...` via share or direct) | anyone holding the URL | none | no | no | no |
| `private` artifact | owner token only | token | n/a | n/a (token-gated) | yes (mutations) |
| Collection share link `/s/share/<token>` | anyone holding the token | token-as-URL | optional | yes | yes (every access) |
| Owner UI `/s/*` and `/api/v1/*` | owner token only | token | n/a | via rotation | yes (mutations, retrievals) |

A link is not private merely because its identifier is unguessable. `public-link`
artifacts and legacy `/f/` URLs are bearer URLs: possession equals access. This is the
pre-existing behavior of the upload tool, preserved and documented here — not upgraded
by assertion. Nothing that was private is made public by the migration: legacy files
were already bearer-URL public, and are recorded as `visibility='public-link'`.

## Enforcement

- Authorization is checked server-side on every route, before any metadata is read.
- Private artifacts: 403 without the owner token — including content bytes,
  thumbnails, extracted text, FTS snippets, and collection membership listings.
- Share links are scoped: a collection link exposes only that collection's entries;
  an artifact link exposes only that artifact. Scope is checked on every request.
- Share tokens are stored as sha256 hashes; the raw token is shown once at creation.
- Expired or revoked links return 404 (indistinguishable from unknown, to avoid
  oracle probing).

## Uploads and content handling

- 25 MB per file cap preserved. Filenames are never used as paths: new blobs are
  addressed by sha256; legacy files match the existing `^[0-9a-f]{32}\.<ext>$` regex.
- Uploaded bytes are never executed. Served with the recorded MIME type;
  non-safe types (`application/*` executables, archives, fonts) get
  `Content-Disposition: attachment`.
- Path traversal is impossible by construction: no user input becomes a filesystem path.
- Extracted document text, transcripts, and imported chat text are **untrusted content**:
  HTML-escaped on render, never evaluated, never treated as instructions. Retrieval
  surfaces source text for reading; it does not convert source text into actions.

## Secrets and logs

- The owner token lives in `.token` (mode 600), loaded at process start, never logged,
  never returned by any API, never written to the database or audit log.
- Audit log records actions and object ids, never file content or secrets.
- Retrieval audit (layer G) logs the query and selected ids, not source excerpts.

## Known limitations (not fixed in this build)

1. Single shared bearer token; no per-user identity, no scopes. Anyone with the token
   has full owner access. Rotation = rewrite `.token` + restart.
2. Token in query string (`?token=`) appears in proxy logs and browser history.
   Mitigation: `Authorization: Bearer` header and `substrate_token` cookie are also
   accepted; the UI prefers the cookie-less query form only for link portability.
3. No rate limiting yet. The 25 MB / 10-file caps bound single-request abuse only.
4. No encryption at rest beyond the VM's disk. TLS is terminated by Caddy.
5. Backups are file copies; there is no point-in-time recovery beyond SQLite WAL.

## Testing

`tests/test_auth.py` covers: unauthenticated API/UI access (403), private artifact
content without token (403, no metadata leak), share-link scope confinement,
expired/revoked links (404), and token-hash storage (raw token absent from DB).
