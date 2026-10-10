"""Benchmarks: synthetic archives at 1k / 10k / 100k artifacts.

Measures metadata navigation, exact search, full-text search, bounded
traversal, and context assembly. Reports actual wall times + query plans.
No extrapolation: only measured numbers are reported.

Usage: python3 tests/bench.py [1k|10k|100k|all]
"""
import os
import random
import shutil
import sqlite3
import string
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from substrate import db as _db, store, model, search, context

WORDS = ("detector muon sqlite archive session artifact edge provenance "
         "checkpoint handoff retrieval quantum cipher pearl recitation "
         "embedding traversal thumbnail transcript ledger).split() ".split())
# fix the word list (avoid the paren artifact above)
WORDS = [w.strip("().") for w in WORDS if w.strip("().")]


def rand_text(rng, nwords):
    return " ".join(rng.choice(WORDS) for _ in range(nwords))


def build(tmp, n_artifacts, bulk=False):
    con = _db.connect(tmp)
    _db.init_db(con)
    rng = random.Random(42)
    ws = model.create_workspace(con, "bench")
    ses_ids = []
    for i in range(max(1, n_artifacts // 100)):
        s = model.create_session(con, workspace_id=ws["id"], title=f"s{i}")
        ses_ids.append(s["id"])
        for j in range(5):
            model.append_event(con, s["id"], rand_text(rng, 40),
                               actor="bench", kind="message")
    aids = []
    t0 = time.time()
    if bulk:
        # bulk load: same rows register_artifact would create, via executemany
        blobs, arts, revs, fts = [], [], [], []
        now = time.time()
        for i in range(n_artifacts):
            aid = store.new_id("art")
            sha = "%064x" % rng.getrandbits(256)
            fn = f"syn{i}.txt"
            blobs.append((sha, 2048, now))
            arts.append((aid, sha, fn, "text/plain", 2048, now,
                         "private", "upload", None, "{}"))
            revs.append((store.new_id("rev"), aid, 1, sha, now, "", None))
            fts.append(("artifact", aid, fn, rand_text(rng, 300)))
            aids.append(aid)
        with con:
            con.executemany("INSERT OR IGNORE INTO blob(sha256,size,stored_at) VALUES(?,?,?)", blobs)
            con.executemany(
                "INSERT INTO artifact(id,blob_sha256,filename,mime,size,created_at,"
                "visibility,source,source_ref,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?)", arts)
            con.executemany(
                "INSERT INTO artifact_revision(id,artifact_id,rev_no,blob_sha256,created_at,"
                "change_note,parent_rev) VALUES(?,?,?,?,?,?,?)", revs)
            con.executemany("INSERT INTO search_fts(doc_type,doc_id,title,body) VALUES(?,?,?,?)", fts)
    else:
        for i in range(n_artifacts):
            a = store.register_artifact(
                con, tmp, data=rand_text(rng, 300).encode(),
                filename=f"syn{i}.txt", mime="text/plain",
                visibility="private", enqueue_extract=False)
            aids.append(a["id"])
    # edges: each artifact linked from a random event-ish node
    ev_ids = [r[0] for r in con.execute("SELECT id FROM event LIMIT 5000")]
    edges = []
    now = time.time()
    for aid in aids:
        if ev_ids and rng.random() < 0.7:
            edges.append((store.new_id("edg"), "event", rng.choice(ev_ids),
                          "artifact", aid, "introduces", now, "explicit", 0, ""))
    with con:
        con.executemany(
            "INSERT INTO edge(id,src_type,src_id,dst_type,dst_id,rel,created_at,"
            "provenance,inferred,note) VALUES(?,?,?,?,?,?,?,?,?,?)", edges)
    con.commit()
    dt = time.time() - t0
    con.close()
    return dt


def measure(tmp, label):
    con = _db.connect(tmp)
    out = {"scale": label}
    db_bytes = os.path.getsize(os.path.join(tmp, "substrate.db"))
    out["db_mb"] = round(db_bytes / 1048576, 1)

    def t(fn, *a, **k):
        t0 = time.time()
        r = fn(*a, **k)
        return time.time() - t0, r

    # metadata navigation: newest-first artifact page
    dt, rows = t(lambda: con.execute(
        "SELECT id, filename, mime, size, created_at FROM artifact"
        " ORDER BY created_at DESC, id DESC LIMIT 50").fetchall())
    out["meta_nav_50_ms"] = round(dt * 1000, 1)

    # exact: by id and by sha prefix
    aid = con.execute("SELECT id FROM artifact LIMIT 1").fetchone()[0]
    sha = con.execute("SELECT blob_sha256 FROM artifact LIMIT 1").fetchone()[0]
    dt, _ = t(search.search_exact, con, None, aid)
    out["exact_id_ms"] = round(dt * 1000, 2)
    dt, _ = t(search.search_exact, con, None, sha[:12])
    out["exact_sha_prefix_ms"] = round(dt * 1000, 2)

    # full-text: common vs rare term
    dt, res = t(search.search_fulltext, con, "detector", limit=25)
    out["fts_common_ms"] = round(dt * 1000, 1)
    out["fts_common_hits"] = len(res)
    dt, res = t(search.search_fulltext, con, "zyxwv", limit=25)
    out["fts_rare_ms"] = round(dt * 1000, 1)

    # bounded traversal depth 2
    seed_ev = con.execute("SELECT id FROM event LIMIT 1").fetchone()
    if seed_ev:
        dt, tr = t(model.traverse, con, [("event", seed_ev[0])], max_depth=2)
        out["traverse_d2_ms"] = round(dt * 1000, 1)
        out["traverse_d2_nodes"] = len(tr["nodes"])

    # context package
    dt, pkg = t(context.build_package, con, "detector provenance archive",
                budget_tokens=2000)
    out["context_ms"] = round(dt * 1000, 1)
    out["context_sources"] = len(pkg["sources"])

    # query plans (representative)
    plan_fts = con.execute(
        "EXPLAIN QUERY PLAN SELECT doc_type, doc_id FROM search_fts"
        " WHERE search_fts MATCH '\"detector\"' ORDER BY bm25(search_fts) LIMIT 25").fetchall()
    out["plan_fts"] = [tuple(r) for r in plan_fts]
    plan_tr = con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM edge WHERE (src_type=? AND src_id=?)"
        " OR (dst_type=? AND dst_id=?) ORDER BY created_at DESC LIMIT 200",
        ("event", "x", "event", "x")).fetchall()
    out["plan_traverse"] = [tuple(r) for r in plan_tr]
    con.close()
    return out


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    scales = {"1k": 1000, "10k": 10000, "100k": 100000}
    if which != "all":
        scales = {which: scales[which]}
    results = []
    for label, n in scales.items():
        tmp = tempfile.mkdtemp(prefix=f"substrate-bench-{label}-")
        try:
            load_s = build(tmp, n, bulk=(n >= 100000))
            m = measure(tmp, label)
            m["load_s"] = round(load_s, 1)
            m["load_mode"] = "bulk" if n >= 100000 else "full-pipeline"
            results.append(m)
            print(f"[{label}] load={m['load_s']}s ({m['load_mode']}) db={m['db_mb']}MB "
                  f"meta50={m['meta_nav_50_ms']}ms exact_id={m['exact_id_ms']}ms "
                  f"fts_common={m['fts_common_ms']}ms({m['fts_common_hits']}hits) "
                  f"traverse_d2={m.get('traverse_d2_ms')}ms({m.get('traverse_d2_nodes')}n) "
                  f"context={m['context_ms']}ms({m['context_sources']}src)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\nQuery plans (representative):")
    for m in results:
        print(f"[{m['scale']}] fts: {m['plan_fts']}")
        print(f"[{m['scale']}] traverse: {m['plan_traverse']}")
    import json
    with open(os.path.expanduser("~/workspace/substrate/docs/BENCHMARKS.json"), "w") as f:
        json.dump(results, f, indent=1, default=str)
    print("\nwrote docs/BENCHMARKS.json")


if __name__ == "__main__":
    main()
