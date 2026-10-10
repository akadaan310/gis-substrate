"""SQLite schema + connection management for the substrate.

One file: <data_dir>/substrate.db. WAL mode. FTS5 for full-text.
Application code (not triggers) keeps search_fts in sync -> deterministic, testable.
"""
import os
import sqlite3
import time

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES workspace(id),
  title TEXT NOT NULL,
  started_at REAL,
  ended_at REAL,
  time_quality TEXT NOT NULL DEFAULT 'exact',
  source TEXT NOT NULL DEFAULT 'manual',
  source_id TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_ws ON session(workspace_id, started_at);

CREATE TABLE IF NOT EXISTS event (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES session(id),
  seq INTEGER NOT NULL,
  ts REAL,
  time_quality TEXT NOT NULL DEFAULT 'exact',
  actor TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'message',
  text TEXT NOT NULL DEFAULT '',
  content_hash TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_event_session ON event(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);

CREATE TABLE IF NOT EXISTS blob (
  sha256 TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  stored_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact (
  id TEXT PRIMARY KEY,
  blob_sha256 TEXT NOT NULL REFERENCES blob(sha256),
  filename TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT 'application/octet-stream',
  size INTEGER NOT NULL,
  created_at REAL NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'private',
  source TEXT NOT NULL DEFAULT 'upload',
  source_ref TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_artifact_blob ON artifact(blob_sha256);
CREATE INDEX IF NOT EXISTS idx_artifact_name ON artifact(filename);
CREATE INDEX IF NOT EXISTS idx_artifact_created ON artifact(created_at);

CREATE TABLE IF NOT EXISTS artifact_revision (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifact(id),
  rev_no INTEGER NOT NULL,
  blob_sha256 TEXT NOT NULL REFERENCES blob(sha256),
  created_at REAL NOT NULL,
  change_note TEXT NOT NULL DEFAULT '',
  parent_rev INTEGER,
  UNIQUE(artifact_id, rev_no)
);

CREATE TABLE IF NOT EXISTS edge (
  id TEXT PRIMARY KEY,
  src_type TEXT NOT NULL,
  src_id TEXT NOT NULL,
  dst_type TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  rel TEXT NOT NULL,
  created_at REAL NOT NULL,
  provenance TEXT NOT NULL DEFAULT 'explicit',
  inferred INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_type, src_id, rel);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_type, dst_id, rel);
CREATE INDEX IF NOT EXISTS idx_edge_rel ON edge(rel);
CREATE INDEX IF NOT EXISTS idx_edge_pair ON edge(src_type, src_id, dst_type, dst_id, rel);

CREATE TABLE IF NOT EXISTS decision (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES workspace(id),
  session_id TEXT REFERENCES session(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  decided_at REAL,
  time_quality TEXT NOT NULL DEFAULT 'exact',
  supersedes_id TEXT REFERENCES decision(id),
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_ws ON decision(workspace_id, decided_at);

CREATE TABLE IF NOT EXISTS task (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES workspace(id),
  kind TEXT NOT NULL DEFAULT 'task',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_ws ON task(workspace_id, status);

CREATE TABLE IF NOT EXISTS claim (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES workspace(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT 'unknown',
  verification_status TEXT NOT NULL DEFAULT 'unverified',
  created_at REAL NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_claim_ws ON claim(workspace_id, verification_status);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claim(id),
  artifact_id TEXT REFERENCES artifact(id),
  event_id TEXT REFERENCES event(id),
  locator TEXT NOT NULL DEFAULT '',
  stance TEXT NOT NULL DEFAULT 'context',
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);

CREATE TABLE IF NOT EXISTS collection (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'private',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS collection_member (
  id TEXT PRIMARY KEY,
  collection_id TEXT NOT NULL REFERENCES collection(id),
  artifact_id TEXT REFERENCES artifact(id),
  child_collection_id TEXT REFERENCES collection(id),
  position REAL NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  added_at REAL NOT NULL,
  CHECK (artifact_id IS NOT NULL OR child_collection_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_colmember_col ON collection_member(collection_id, position);

CREATE TABLE IF NOT EXISTS share_link (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_share_scope ON share_link(scope_type, scope_id);

CREATE TABLE IF NOT EXISTS checkpoint (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES workspace(id),
  session_id TEXT REFERENCES session(id),
  title TEXT NOT NULL,
  created_at REAL NOT NULL,
  schema_version INTEGER NOT NULL,
  body_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_ws ON checkpoint(workspace_id, created_at);

CREATE TABLE IF NOT EXISTS handoff (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES workspace(id),
  from_session_id TEXT REFERENCES session(id),
  to_env TEXT NOT NULL DEFAULT '',
  objective TEXT NOT NULL DEFAULT '',
  payload_artifact_id TEXT REFERENCES artifact(id),
  payload_hash TEXT,
  status TEXT NOT NULL DEFAULT 'prepared',
  scope_json TEXT NOT NULL DEFAULT '{}',
  expected_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_handoff_ws ON handoff(workspace_id, status);

CREATE TABLE IF NOT EXISTS extract_job (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifact(id),
  kind TEXT NOT NULL DEFAULT 'auto',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extract_status ON extract_job(status, created_at);

CREATE TABLE IF NOT EXISTS extracted_chunk (
  artifact_id TEXT NOT NULL REFERENCES artifact(id),
  chunk_no INTEGER NOT NULL,
  page_no TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  revision_id TEXT,
  backend TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL DEFAULT '',
  source_sha256 TEXT NOT NULL DEFAULT '',
  extracted_at REAL NOT NULL DEFAULT 0,
  confidence REAL,
  PRIMARY KEY (artifact_id, chunk_no)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  doc_type, doc_id, title, body, tokenize='porter'
);

CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  scope_type TEXT NOT NULL DEFAULT '',
  scope_id TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts);

CREATE TABLE IF NOT EXISTS legacy_file (
  legacy_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifact(id)
);
"""


def connect(data_dir, check_same_thread=False):
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "substrate.db")
    con = sqlite3.connect(path, check_same_thread=check_same_thread)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def init_db(con):
    con.executescript(DDL)
    _migrate(con)
    row = con.execute("SELECT value FROM kv WHERE key='schema_version'").fetchone()
    if row is None:
        con.execute("INSERT INTO kv(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
    con.commit()


def _migrate(con):
    """Additive migrations for existing databases. Idempotent."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(extracted_chunk)").fetchall()}
    for name, ddl in (("revision_id", "TEXT"),
                      ("backend", "TEXT NOT NULL DEFAULT ''"),
                      ("method", "TEXT NOT NULL DEFAULT ''"),
                      ("source_sha256", "TEXT NOT NULL DEFAULT ''"),
                      ("extracted_at", "REAL NOT NULL DEFAULT 0"),
                      ("confidence", "REAL")):
        if name not in cols:
            con.execute("ALTER TABLE extracted_chunk ADD COLUMN %s %s" % (name, ddl))
    con.commit()


def now():
    return time.time()
