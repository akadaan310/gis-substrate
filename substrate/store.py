"""Content-addressed blob store + artifact registration.

Blobs: <data_dir>/blobs/ab/cd/<sha256>. Atomic write via temp+rename.
Legacy files are indexed IN PLACE (never moved); new uploads go to the blob store.
Dedup is by bytes only — provenance/ACL records are never merged.
"""
import hashlib
import mimetypes
import os
import secrets
import shutil
import string
import time

from . import db as _db

_ID_ALPHABET = string.ascii_lowercase + string.digits


def new_id(prefix, n=12):
    return "%s_%s" % (prefix, "".join(secrets.choice(_ID_ALPHABET) for _ in range(n)))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def blob_path(data_dir, sha):
    return os.path.join(data_dir, "blobs", sha[0:2], sha[2:4], sha)


def store_bytes(data_dir, data):
    """Store bytes in the blob store; return (sha256, size, is_new)."""
    sha = sha256_bytes(data)
    return _store_sha(data_dir, sha, len(data), lambda dst: _write_bytes(dst, data))


def store_file(data_dir, src_path):
    """Store a file's bytes in the blob store; return (sha256, size, is_new)."""
    sha = sha256_file(src_path)
    size = os.path.getsize(src_path)
    return _store_sha(data_dir, sha, size, lambda dst: shutil.copyfile(src_path, dst))


def _write_bytes(dst, data):
    with open(dst, "wb") as f:
        f.write(data)


def _store_sha(data_dir, sha, size, writer):
    dest = blob_path(data_dir, sha)
    if os.path.isfile(dest):
        return sha, size, False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp-%s" % secrets.token_hex(4)
    try:
        writer(tmp)
        os.replace(tmp, dest)
        os.chmod(dest, 0o644)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return sha, size, True


def read_blob(data_dir, sha):
    p = blob_path(data_dir, sha)
    with open(p, "rb") as f:
        return f.read()


def blob_exists(data_dir, sha):
    return os.path.isfile(blob_path(data_dir, sha))


# ---------------------------------------------------------------- artifacts

def register_artifact(con, data_dir, data=None, src_path=None, filename="file",
                      mime=None, visibility="private", source="upload",
                      source_ref=None, meta=None, enqueue_extract=True):
    """Register bytes (or a file) as an artifact. Returns artifact row dict.

    Same bytes -> same blob, but ALWAYS a new artifact record (provenance never merged).
    """
    if data is not None:
        sha, size, _is_new = store_bytes(data_dir, data)
    elif src_path is not None:
        sha, size, _is_new = store_file(data_dir, src_path)
    else:
        raise ValueError("data or src_path required")
    if not mime:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    now = time.time()
    with con:
        con.execute("INSERT OR IGNORE INTO blob(sha256, size, stored_at) VALUES(?,?,?)",
                    (sha, size, now))
        aid = new_id("art")
        con.execute(
            """INSERT INTO artifact(id, blob_sha256, filename, mime, size, created_at,
                                    visibility, source, source_ref, meta_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (aid, sha, filename, mime, size, now, visibility, source, source_ref,
             _json(meta)))
        rev_id = new_id("rev")
        con.execute(
            """INSERT INTO artifact_revision(id, artifact_id, rev_no, blob_sha256,
                                             created_at, change_note, parent_rev)
               VALUES(?,?,?,?,?,'',NULL)""",
            (rev_id, aid, 1, sha, now))
        if enqueue_extract:
            job_id = new_id("job")
            con.execute(
                """INSERT INTO extract_job(id, artifact_id, kind, status, attempts,
                                           last_error, created_at, updated_at)
                   VALUES(?,?,'auto','pending',0,'',?,?)""",
                (job_id, aid, now, now))
        _fts_upsert_artifact(con, aid, filename, "")
    return get_artifact(con, aid)


def get_artifact(con, aid):
    row = con.execute("SELECT * FROM artifact WHERE id=?", (aid,)).fetchone()
    return dict(row) if row else None


def add_revision(con, data_dir, artifact_id, data=None, src_path=None, change_note=""):
    """New revision of an existing artifact. Old revisions stay addressable."""
    art = get_artifact(con, artifact_id)
    if not art:
        raise KeyError("artifact not found: %s" % artifact_id)
    if data is not None:
        sha, size, _ = store_bytes(data_dir, data)
    else:
        sha, size, _ = store_file(data_dir, src_path)
    now = time.time()
    with con:
        con.execute("INSERT OR IGNORE INTO blob(sha256, size, stored_at) VALUES(?,?,?)",
                    (sha, size, now))
        rev_no = con.execute("SELECT COALESCE(MAX(rev_no),0)+1 FROM artifact_revision "
                             "WHERE artifact_id=?", (artifact_id,)).fetchone()[0]
        parent = con.execute("SELECT rev_no FROM artifact_revision WHERE artifact_id=? "
                             "ORDER BY rev_no DESC LIMIT 1", (artifact_id,)).fetchone()
        rid = new_id("rev")
        con.execute(
            """INSERT INTO artifact_revision(id, artifact_id, rev_no, blob_sha256,
                                             created_at, change_note, parent_rev)
               VALUES(?,?,?,?,?,?,?)""",
            (rid, artifact_id, rev_no, sha, now, change_note,
             parent[0] if parent else None))
        con.execute("UPDATE artifact SET blob_sha256=?, size=? WHERE id=?",
                    (sha, size, artifact_id))
        # The previous revision's extracted text is stale: drop its chunks so
        # search never serves old content for the new revision, and enqueue
        # a fresh extraction job. The old bytes remain addressable via
        # artifact_revision; extraction is derived data and regenerable.
        con.execute("DELETE FROM extracted_chunk WHERE artifact_id=?",
                    (artifact_id,))
        con.execute("DELETE FROM search_fts WHERE doc_type='artifact' AND doc_id=?",
                    (artifact_id,))
        row = con.execute("SELECT filename FROM artifact WHERE id=?",
                          (artifact_id,)).fetchone()
        con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body)"
                    " VALUES('artifact',?,?,?)",
                    (artifact_id, row[0] if row else "", ""))
        con.execute(
            """INSERT INTO extract_job(id, artifact_id, kind, status, attempts,
                                       last_error, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (new_id("job"), artifact_id, "auto", "pending", 0, "", now, now))
    return rev_no


def revisions(con, artifact_id):
    rows = con.execute("SELECT * FROM artifact_revision WHERE artifact_id=? "
                       "ORDER BY rev_no", (artifact_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- legacy

def index_legacy_file(con, data_dir, legacy_id, file_path, orig_name=None, mime=None):
    """Index an existing upload-tool file IN PLACE. Returns artifact row dict.

    The file is NOT moved or copied. A legacy_file row maps the old /f/<id> URL
    to the new artifact id. Idempotent: re-running returns the existing artifact.
    """
    existing = con.execute("SELECT artifact_id FROM legacy_file WHERE legacy_id=?",
                           (legacy_id,)).fetchone()
    if existing:
        return get_artifact(con, existing[0])
    sha = sha256_file(file_path)
    size = os.path.getsize(file_path)
    now = time.time()
    if not mime:
        mime = mimetypes.guess_type(orig_name or legacy_id)[0] or "application/octet-stream"
    with con:
        # Legacy bytes stay where they are; record the blob row pointing at the
        # existing path via source_ref so readers know it is not in blobs/.
        con.execute("INSERT OR IGNORE INTO blob(sha256, size, stored_at) VALUES(?,?,?)",
                    (sha, size, now))
        aid = new_id("art")
        con.execute(
            """INSERT INTO artifact(id, blob_sha256, filename, mime, size, created_at,
                                    visibility, source, source_ref, meta_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (aid, sha, orig_name or legacy_id, mime, size,
             os.path.getmtime(file_path), "public-link", "legacy", file_path, "{}"))
        con.execute("INSERT INTO legacy_file(legacy_id, artifact_id) VALUES(?,?)",
                    (legacy_id, aid))
        rev_id = new_id("rev")
        con.execute(
            """INSERT INTO artifact_revision(id, artifact_id, rev_no, blob_sha256,
                                             created_at, change_note, parent_rev)
               VALUES(?,?,?,?,?,'legacy import',NULL)""",
            (rev_id, aid, 1, sha, now))
        _fts_upsert_artifact(con, aid, orig_name or legacy_id, "")
    return get_artifact(con, aid)


def legacy_artifact_id(con, legacy_id):
    row = con.execute("SELECT artifact_id FROM legacy_file WHERE legacy_id=?",
                      (legacy_id,)).fetchone()
    return row[0] if row else None


def read_artifact_bytes(con, data_dir, artifact):
    """Read bytes for an artifact, whether in the blob store or a legacy path."""
    sha = artifact["blob_sha256"]
    p = blob_path(data_dir, sha)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    # legacy in-place file
    ref = artifact.get("source_ref")
    if ref and os.path.isfile(ref):
        with open(ref, "rb") as f:
            return f.read()
    raise FileNotFoundError("bytes unavailable for artifact %s" % artifact["id"])


# ---------------------------------------------------------------- FTS sync

def _json(meta):
    import json
    if meta is None:
        return "{}"
    if isinstance(meta, str):
        return meta
    return json.dumps(meta)


def _fts_upsert_artifact(con, aid, title, body):
    con.execute("DELETE FROM search_fts WHERE doc_type='artifact' AND doc_id=?", (aid,))
    con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body) VALUES(?,?,?,?)",
                ("artifact", aid, title or "", body or ""))


def fts_upsert_event(con, eid, title, body):
    con.execute("DELETE FROM search_fts WHERE doc_type='event' AND doc_id=?", (eid,))
    con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body) VALUES(?,?,?,?)",
                ("event", eid, title or "", body or ""))


def fts_upsert_record(con, doc_type, doc_id, title, body):
    con.execute("DELETE FROM search_fts WHERE doc_type=? AND doc_id=?", (doc_type, doc_id))
    con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body) VALUES(?,?,?,?)",
                (doc_type, doc_id, title or "", body or ""))


def fts_delete(con, doc_type, doc_id):
    con.execute("DELETE FROM search_fts WHERE doc_type=? AND doc_id=?", (doc_type, doc_id))
