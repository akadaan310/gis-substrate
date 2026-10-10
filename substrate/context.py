"""Context package assembly (retrieval layer F).

Builds a compact, source-grounded package: candidate sources from
search_all + bounded graph traversal from seed ids, ranked (exact >
full-text BM25 > recency), greedily included under a token budget.
Every source points at its canonical id; what was omitted and why is
reported. Retrieval is audited via model.audit. Nothing is invented:
empty results yield an explicit missing_evidence entry.

Token counts are estimates (chars/4), always labeled as such.
"""
import json
import time

from . import db as _db
from . import store
from . import model as _model
from . import search as _search

EXCERPT_CHARS = 600
ADDITIONAL_INDEX_MAX = 30


def estimate_tokens(text):
    """Rough size estimate (chars/4). ALWAYS labeled as an estimate."""
    return max(1, len(text or "") // 4)


def _truncate(text, n=EXCERPT_CHARS):
    text = text or ""
    if len(text) <= n:
        return text
    return text[:n] + "… [truncated]"


def _norm_seed(s):
    if isinstance(s, (list, tuple)) and len(s) == 2:
        return s[0], s[1]
    if isinstance(s, dict):
        return s.get("doc_type"), s.get("doc_id")
    return None, None


def _fetch_object(con, doc_type, doc_id):
    """Resolve one object to title/excerpt/locator/provenance/uncertainties.

    Returns None when the object does not exist. Never invents content.
    """
    unc = []
    if doc_type == "artifact":
        r = con.execute("SELECT * FROM artifact WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        art = dict(r)
        chunks = con.execute(
            "SELECT page_no, text, backend, method FROM extracted_chunk"
            " WHERE artifact_id=? ORDER BY chunk_no LIMIT 2",
            (doc_id,)).fetchall()
        if chunks:
            excerpt = "\n\n".join(
                ("[%s] %s" % (c["page_no"], c["text"])) for c in chunks)
            methods = sorted({(c["backend"] or "?", c["method"] or "?")
                              for c in chunks})
            if any(m[1] == "ocr" for m in methods):
                unc.append("artifact %s: excerpt includes OCR-recognized text; "
                           "verify against the original image" % doc_id)
            prov_extra = "; extraction=%s" % ", ".join(
                "%s/%s" % m for m in methods)
        else:
            job = con.execute(
                "SELECT status, last_error FROM extract_job WHERE artifact_id=?"
                " ORDER BY created_at DESC LIMIT 1", (doc_id,)).fetchone()
            excerpt = "(no extracted text"
            if job and job["status"] != "done":
                err = (": " + job["last_error"]) if job["last_error"] else ""
                excerpt += "; extraction job status: %s%s" % (job["status"], err)
            excerpt += ")"
            if job and job["status"] in ("failed", "unavailable"):
                unc.append("artifact %s: extraction %s%s" % (
                    doc_id, job["status"],
                    (" — " + job["last_error"]) if job["last_error"] else ""))
        try:
            meta = json.loads(art.get("meta_json") or "{}")
        except ValueError:
            meta = {}
        if meta.get("extraction_note"):
            unc.append("artifact %s: %s" % (doc_id, meta["extraction_note"]))
        prov = "source=%s; sha256=%s; visibility=%s" % (
            art.get("source"), art.get("blob_sha256"), art.get("visibility"))
        if chunks:
            prov += prov_extra
        if art.get("source") == "legacy":
            # stable legacy file id, never a host path (DESIGN: no fs paths out)
            leg = con.execute(
                "SELECT legacy_id FROM legacy_file WHERE artifact_id=?",
                (doc_id,)).fetchone()
            if leg:
                prov += "; legacy_id=%s" % leg[0]
        elif art.get("source_ref"):
            prov += "; source_ref=%s" % art["source_ref"]
        return {"doc_type": "artifact", "doc_id": doc_id,
                "title": art.get("filename") or "(untitled)",
                "excerpt": excerpt, "locator": "sha256:%s" % art.get("blob_sha256"),
                "provenance": prov, "created_at": art.get("created_at") or 0,
                "uncertainties": unc}

    if doc_type == "event":
        r = con.execute("SELECT * FROM event WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        ev = dict(r)
        if ev.get("time_quality") != "exact":
            unc.append("event %s has time_quality=%s" % (
                doc_id, ev.get("time_quality")))
        return {"doc_type": "event", "doc_id": doc_id,
                "title": "%s / %s" % (ev.get("actor") or "?", ev.get("kind")),
                "excerpt": ev.get("text") or "(empty event text)",
                "locator": "session %s seq %d" % (ev.get("session_id"), ev.get("seq")),
                "provenance": "session=%s; actor=%s; kind=%s" % (
                    ev.get("session_id"), ev.get("actor"), ev.get("kind")),
                "created_at": ev.get("created_at") or 0,
                "uncertainties": unc}

    if doc_type == "session":
        r = con.execute("SELECT * FROM session WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        s = dict(r)
        if s.get("time_quality") != "exact":
            unc.append("session %s has time_quality=%s" % (
                doc_id, s.get("time_quality")))
        body = (s.get("title") or "(untitled session)")
        if s.get("description"):
            body += "\n" + s["description"]
        return {"doc_type": "session", "doc_id": doc_id,
                "title": s.get("title") or "(untitled session)",
                "excerpt": body,
                "locator": "workspace %s" % s.get("workspace_id"),
                "provenance": "source=%s; source_id=%s" % (
                    s.get("source"), s.get("source_id")),
                "created_at": s.get("created_at") or 0,
                "uncertainties": unc}

    if doc_type in ("decision", "task", "claim"):
        r = con.execute("SELECT * FROM %s WHERE id=?" % doc_type,
                        (doc_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        body = d.get("title") or "(untitled)"
        if d.get("body"):
            body += "\n" + d["body"]
        extra = ""
        if doc_type == "decision":
            extra = "; status=%s" % d.get("status")
            if d.get("time_quality") != "exact":
                unc.append("decision %s has time_quality=%s" % (
                    doc_id, d.get("time_quality")))
        elif doc_type == "task":
            extra = "; status=%s" % d.get("status")
        elif doc_type == "claim":
            extra = "; verification_status=%s; confidence=%s" % (
                d.get("verification_status"), d.get("confidence"))
        return {"doc_type": doc_type, "doc_id": doc_id,
                "title": d.get("title") or "(untitled)",
                "excerpt": body,
                "locator": "workspace %s" % d.get("workspace_id"),
                "provenance": ("workspace=%s" % d.get("workspace_id")) + extra,
                "created_at": d.get("created_at") or 0,
                "uncertainties": unc}

    if doc_type == "checkpoint":
        r = con.execute("SELECT * FROM checkpoint WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        c = dict(r)
        return {"doc_type": "checkpoint", "doc_id": doc_id,
                "title": c.get("title") or "(untitled checkpoint)",
                "excerpt": "checkpoint body references canonical ids; "
                           "schema_version=%s" % c.get("schema_version"),
                "locator": "workspace %s" % c.get("workspace_id"),
                "provenance": "immutable; created_at=%s" % c.get("created_at"),
                "created_at": c.get("created_at") or 0,
                "uncertainties": unc}

    if doc_type == "handoff":
        r = con.execute("SELECT * FROM handoff WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        h = dict(r)
        body = h.get("objective") or "(no objective)"
        if h.get("payload_hash"):
            body += "\npayload_hash=%s" % h["payload_hash"]
        return {"doc_type": "handoff", "doc_id": doc_id,
                "title": (h.get("objective") or "(no objective)")[:80],
                "excerpt": body,
                "locator": "status %s" % h.get("status"),
                "provenance": "status=%s; to_env=%s" % (
                    h.get("status"), h.get("to_env")),
                "created_at": h.get("created_at") or 0,
                "uncertainties": unc}

    if doc_type == "collection":
        r = con.execute("SELECT * FROM collection WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        c = dict(r)
        n = con.execute("SELECT COUNT(*) FROM collection_member WHERE collection_id=?",
                        (doc_id,)).fetchone()[0]
        return {"doc_type": "collection", "doc_id": doc_id,
                "title": c.get("title") or "(untitled collection)",
                "excerpt": "%s (%d members)" % (
                    c.get("description") or "(no description)", n),
                "locator": "collection %s" % doc_id,
                "provenance": "visibility=%s" % c.get("visibility"),
                "created_at": c.get("created_at") or 0,
                "uncertainties": unc}

    return None  # unknown doc_type: do not fabricate


def _edges_among(con, ids, cap=200):
    if not ids:
        return []
    ids = list(ids)
    clause = " OR ".join(["(src_id=? OR dst_id=?)"] * len(ids))
    params = []
    for i in ids:
        params.extend([i, i])
    rows = con.execute("SELECT * FROM edge WHERE " + clause +
                       " ORDER BY created_at DESC LIMIT ?", params + [cap * 4])
    idset = set(ids)
    out = []
    for r in rows:
        e = dict(r)
        if e["src_id"] in idset and e["dst_id"] in idset:
            out.append({
                "id": e["id"], "src_type": e["src_type"], "src_id": e["src_id"],
                "dst_type": e["dst_type"], "dst_id": e["dst_id"],
                "rel": e["rel"], "provenance": e["provenance"],
                "inferred": bool(e["inferred"]), "note": e["note"]})
            if len(out) >= cap:
                break
    return out


def build_package(con, question, seed_ids=None, budget_tokens=4000,
                  filters=None, actor="owner", data_dir=None):
    """Assemble a budgeted, source-grounded context package."""
    question = question or ""
    budget_tokens = max(256, int(budget_tokens or 4000))
    retrieval_id = store.new_id("ctx")
    now = time.time()

    # -- gather candidates ------------------------------------------------
    candidates = []  # (sort_key, fetched_obj, why)
    seen = set()

    def consider(obj, sort_key, why):
        if obj is None:
            return
        key = (obj["doc_type"], obj["doc_id"])
        if key in seen:
            return
        seen.add(key)
        candidates.append((sort_key, obj, why))

    search_res = _search.search_all(con, question,
                                    filters=filters, limit=50,
                                    data_dir=data_dir)
    for item in search_res["results"]:
        obj = _fetch_object(con, item["doc_type"], item["doc_id"])
        if item["layer"] == "exact":
            sk = (0, 0, 0)
        else:  # fulltext: BM25 rank asc is better
            sk = (1, item.get("rank") or 0, 0)
        consider(obj, sk, item.get("why") or "retrieved via %s" % item["layer"])

    # graph expansion from seeds (bounded, depth 2)
    seeds = []
    for s in (seed_ids or []):
        t, i = _norm_seed(s)
        if t and i:
            seeds.append((t, i))
            obj = _fetch_object(con, t, i)
            consider(obj, (0, 0, 0), "explicit seed id")
    if seeds:
        try:
            trav = _model.traverse(con, seeds, max_depth=2, per_depth_budget=200)
            for node in trav["nodes"]:
                if node["depth"] == 0:
                    continue
                obj = _fetch_object(con, node["obj_type"], node["obj_id"])
                consider(obj, (2, node["depth"], 0),
                         "graph traversal from seed (depth %d)" % node["depth"])
        except ValueError:
            pass  # invalid depth config; seeds alone still work

    # -- rank: exact > fulltext(BM25 asc) > graph depth > recency desc -------
    ranked = sorted(
        candidates,
        key=lambda c: (c[0][0], c[0][1], c[0][2], -(c[1].get("created_at") or 0)))

    # -- greedy budgeted inclusion -----------------------------------------
    sources, additional, uncertainties = [], [], []
    total = 0
    omitted_count, omit_reasons = 0, {}
    for _sk, obj, why in ranked:
        excerpt = _truncate(obj["excerpt"])
        tokens = estimate_tokens(excerpt)
        if total + tokens <= budget_tokens:
            sources.append({
                "doc_type": obj["doc_type"], "doc_id": obj["doc_id"],
                "title": obj["title"], "excerpt": excerpt,
                "locator": obj["locator"], "provenance": obj["provenance"],
                "why_included": why, "tokens_est": tokens})
            total += tokens
            for u in obj.get("uncertainties") or []:
                if u not in uncertainties:
                    uncertainties.append(u)
        else:
            omitted_count += 1
            omit_reasons["over budget"] = omit_reasons.get("over budget", 0) + 1
            if len(additional) < ADDITIONAL_INDEX_MAX:
                additional.append({
                    "doc_type": obj["doc_type"], "doc_id": obj["doc_id"],
                    "title": obj["title"], "why_not_included": "over budget"})

    # unresolvable candidates become missing evidence, not silent drops
    missing = []
    if not candidates:
        missing.append("no matching records found for: '%s'" % question[:120])

    included_ids = {s["doc_id"] for s in sources}
    relationships = _edges_among(con, included_ids)
    for e in relationships:
        if e["inferred"] and not any(
                "inferred edge" in u for u in uncertainties):
            uncertainties.append(
                "some relationships among included sources are inferred, "
                "not user-confirmed")

    # -- audit (query text logged; source content is NOT logged) ------------
    _model.audit(con, actor, "retrieval", "", "",
                 {"retrieval_id": retrieval_id,
                  "query": question[:500],
                  "filters": filters or {},
                  "selected_ids": sorted(included_ids),
                  "rationale": "exact > FTS5 BM25 > graph depth 2 > recency; "
                               "greedy excerpt inclusion under token budget",
                  "algorithm": "ctx-v1",
                  "limitations": "token counts are estimates (chars/4); "
                                 "no semantic embeddings; "
                                 "FTS5 only over extracted/indexed text"})

    return {
        "question": question,
        "retrieval_id": retrieval_id,
        "algorithm": "ctx-v1",
        "created_at": now,
        "sources": sources,
        "relationships": relationships,
        "uncertainties": uncertainties,
        "missing_evidence": missing,
        "additional_index": additional,
        "omitted": {"count": omitted_count, "reasons": omit_reasons},
        "token_estimate": total,
        "token_estimate_is_estimate": True,
        "budget_tokens": budget_tokens,
    }
