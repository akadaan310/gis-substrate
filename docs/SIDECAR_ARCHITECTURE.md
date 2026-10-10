# Architecture Decision: GIS Browser Sidecar (Option B)

## Decision
**Option 1: Authenticated capture page within existing Substrate UI** (`/s/capture/`)

NOT a browser extension.

## Why
1. **Existing auth**: `/s/` already has owner-token + cookie sessions. No new auth to build.
2. **Existing API**: `POST /api/v1/handoffs` already exists with idempotency.
3. **Smaller attack surface**: No extension permissions, no content scripts, no store review.
4. **User-mediated by design**: The requirement says "first version may be user-mediated."
   A capture page IS user-mediated — copy from ChatGPT, paste, review, approve.
5. **No silent capture**: An extension cannot "silently read every ChatGPT message"
   per the constraints. The page makes the manual step explicit and honest.

## What was built
- `substrate/ui.py`: `_page_capture()` + route `/s/capture/` + NAV entry
- Form: workspace, objective, summary, completed/pending tasks, evidence,
  decisions, uncertainties, next action, pasted content
- Client-side JS: builds exact JSON payload, shows preview, requires explicit
  "Approve & Save" click
- POSTs to existing `/api/v1/handoffs` with `Idempotency-Key` header
- Success shows record ID with link to `/s/handoffs/<id>`

## Security properties
- Requires owner token (same as all `/s/` routes) — unauthenticated returns 403
- No token in client-side code, URLs, or logs
- Reuses existing authenticated API — no new endpoints, no anonymous writes
- Idempotency-Key prevents duplicate records on retry
- Payload preview shows EXACT JSON before approval
- MCP write gate unchanged

## Tests run
- Page renders (200) with all form fields
- Unauthenticated access → 403
- Idempotency-Key present in submission
- Full suite: 106 tests pass (20 core + 66 API + 20 continuity)
- Live API test: same Idempotency-Key twice → same handoff ID (no duplicate)
- Test handoff: `ho_7hpjjc2pbpkn`

## Manual vs automated
**Manual** (user does):
- Copy content from ChatGPT web
- Paste into capture page
- Fill structured fields
- Review preview
- Click Approve & Save

**Automated**:
- Payload JSON construction
- Idempotency key generation
- API submission
- Success/failure feedback with record link

**Still requires user**: The copy-paste step. This is intentional for v1.
Option A (native MCP) would eliminate it when plan/platform supports it.

## Limitations
- Not deployed to production yet — code is local only (commit ba5aa13)
- Requires user to manually copy from ChatGPT (no auto-capture)
- Web UI only — no mobile capture page yet
- Test handoff `ho_7hpjjc2pbpkn` preserved as debris (no delete API)
