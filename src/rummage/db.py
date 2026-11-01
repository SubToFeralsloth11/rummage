"""SQLite storage: file metadata, text chunks, FTS5 keyword index, embedding blobs."""

from __future__ import annotations

import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    indexed_at REAL NOT NULL,
    ocr INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    summary TEXT  -- JSON code summary (language, functions, tags, …)
);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    emb BLOB
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
-- emb IS NULL = waiting to be embedded; empty blob = deliberately not embedded (keyword-only chunk).
CREATE INDEX IF NOT EXISTS chunks_pending ON chunks(id) WHERE emb IS NULL;
-- rowid = chunks.id. "name" holds the file name plus folder words so names are searchable.
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, name, tokenize='porter unicode61');
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if "summary" not in cols:  # index made by an older version
        conn.execute("ALTER TABLE files ADD COLUMN summary TEXT")
    return conn


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (file_id,))
    conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
    conn.execute("DELETE FROM files WHERE id=?", (file_id,))


def stats(conn: sqlite3.Connection) -> dict:
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM chunks WHERE emb IS NULL").fetchone()[0]
    kinds = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind")}
    return {"files": files, "chunks": chunks, "embedded": chunks - pending, "kinds": kinds}
