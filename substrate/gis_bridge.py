"""GIS bridge prototype: thin tool wrapper over the Substrate REST API.

Implements the five proposed GIS tools using existing authenticated endpoints.
No new backend work. Credentials never appear in URLs, logs, or results.
"""
import hashlib
import json
import urllib.request
import urllib.parse


class GISBridge:
    def __init__(self, base_url, token):
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use https://")
        self.base = base_url.rstrip("/")
        # Token must come from environment or secure storage, never hardcoded.
        # It is used only in the Authorization header, never logged or printed.
        self.token = token

    def _req(self, method, path, data=None, idem_key=None):
        req = urllib.request.Request(
            self.base + path, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        if idem_key:
            req.add_header("Idempotency-Key", idem_key)
        body = json.dumps(data).encode() if data is not None else None
        try:
            with urllib.request.urlopen(req, data=body, timeout=30) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read()), e.code
            except Exception:
                return {"error": {"code": "http_error"}}, e.code

    def gis_bootstrap(self, workspace_id, task_or_project=None):
        """Idempotent read-only bootstrap. Returns context package + classification."""
        out = {"workspace_id": workspace_id, "retrieved": {}, "unknown": []}
        d, code = self._req("GET", "/api/v1/health")
        out["retrieved"]["health"] = code
        if code != 200:
            out["unknown"].append("GIS unavailable — no context retrieved")
            return out
        d, code = self._req("GET", f"/api/v1/workspaces/{workspace_id}/context")
        if code != 200:
            out["unknown"].append(f"context fetch failed: HTTP {code}")
            return out
        ctx = d["context"]
        out["retrieved"]["context"] = {
            "protocols": [(p["id"], p["title"], p["status"]) for p in ctx["protocols"]],
            "tasks": [(t["id"], t["title"], t["status"]) for t in ctx["tasks"]],
            "decisions": [(x["id"], x["title"]) for x in ctx["decisions"]],
            "next_actions": ctx["next_actions"],
            "truncated": ctx["truncated"],
        }
        # Latest handoff: surface the concrete next_action from the most
        # recent session. Read-only; failure is non-fatal.
        try:
            hd, hcode = self._req("GET",
                f"/api/v1/handoffs?workspace_id={workspace_id}&limit=1")
            hs = hd.get("handoffs", []) if hcode == 200 else []
            if hs:
                h = hs[0]
                exp = h.get("expected_json", {})
                if isinstance(exp, str):
                    import json as _j
                    try: exp = _j.loads(exp)
                    except: exp = {}
                out["retrieved"]["latest_handoff"] = {
                    "id": h["id"],
                    "next_action": exp.get("next_action"),
                    "created_at": h.get("created_at"),
                }
            else:
                out["unknown"].append("no handoffs found for workspace")
        except Exception as e:
            out["unknown"].append(f"handoff fetch failed: {type(e).__name__}")
        if task_or_project:
            tid = task_or_project
            d, code = self._req("GET", f"/api/v1/tasks/{tid}")
            if code == 200:
                t = d["task"]
                out["retrieved"]["focus_task"] = {
                    "id": t["id"], "title": t["title"],
                    "status": t["status"], "kind": t["kind"],
                    "edges": d.get("edges_out", []),
                }
            else:
                out["unknown"].append(f"focus task {tid}: HTTP {code}")
        return out

    def gis_search(self, query, workspace_id=None, limit=20):
        q = urllib.parse.quote(query)
        path = f"/api/v1/search?q={q}&limit={min(limit, 50)}"
        d, code = self._req("GET", path)
        if code != 200:
            return {"error": f"HTTP {code}"}
        return d

    def gis_get_record(self, record_id, include_payload=False):
        prefix = record_id.split("_")[0] if "_" in record_id else ""
        routes = {"tsk": "tasks", "art": "artifacts", "ho": "handoffs",
                  "ws": "workspaces", "dec": "decisions", "clm": "claims",
                  "evd": "evidence"}
        rtype = routes.get(prefix)
        if not rtype:
            return {"error": f"unknown record type: {record_id}"}
        d, code = self._req("GET", f"/api/v1/{rtype}/{record_id}")
        out = {"record": d, "http": code}
        # Handoff payloads live in a linked artifact; follow it when asked.
        if include_payload and code == 200 and rtype == "handoffs":
            h = d.get("handoff", {})
            paid = h.get("payload_artifact_id")
            if paid:
                # Use the dedicated payload endpoint (returns JSON with hash_match)
                pd, pcode = self._req("GET", f"/api/v1/handoffs/{record_id}/payload")
                out["payload_http"] = pcode
                if pcode == 200:
                    out["payload"] = pd
        return out

    def gis_update_task(self, task_id, changes):
        allowed = {"status", "title", "body", "meta", "note"}
        data = {k: v for k, v in changes.items() if k in allowed}
        if not data:
            return {"error": "no valid fields in changes"}
        d, code = self._req("PATCH", f"/api/v1/tasks/{task_id}", data)
        return {"result": d, "http": code}

    def gis_save_handoff(self, workspace_id, session_summary, completed,
                         pending, next_action, evidence_refs=None):
        payload = {
            "objective": session_summary,
            "workspace_id": workspace_id,
            "payload_text": json.dumps({
                "completed": completed, "pending": pending,
                "next_action": next_action,
                "evidence_refs": evidence_refs or [],
                "contract_version": "v1",
            }),
            "expected": {"next_action": next_action},
            "meta": {"tool": "gis_save_handoff"},
        }
        # Deterministic idempotency key: same content → same key → no duplicates
        key_src = json.dumps(payload, sort_keys=True)
        idem = "handoff-" + hashlib.sha256(key_src.encode()).hexdigest()[:16]
        d, code = self._req("POST", "/api/v1/handoffs", payload, idem_key=idem)
        return {"result": d, "http": code, "idempotency_key": idem[:12] + "…"}
