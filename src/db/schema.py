from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# All CREATE TABLE statements. Run with IF NOT EXISTS so open_db() is idempotent.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id                   INTEGER PRIMARY KEY,
    file_path            TEXT UNIQUE NOT NULL,
    capture_date         DATE,
    file_type            TEXT,
    file_hash            TEXT,
    import_timestamp     DATETIME,
    enrichment_status    TEXT DEFAULT '{}',
    imported_by_protocol TEXT
);

CREATE TABLE IF NOT EXISTS metadata (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (file_id, key)
);

CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    embedding_model TEXT NOT NULL,
    embedding       BLOB NOT NULL,
    UNIQUE (file_id, embedding_model)
);

CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',
    protocol   TEXT NOT NULL,
    file_id    INTEGER REFERENCES files(id) ON DELETE CASCADE,
    created_at DATETIME NOT NULL,
    updated_at DATETIME,
    error      TEXT,
    result     TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_capture_date ON files(capture_date);
CREATE INDEX IF NOT EXISTS idx_files_file_type    ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_metadata_key       ON metadata(key);
CREATE INDEX IF NOT EXISTS idx_jobs_status        ON jobs(status);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """Open (creating if absent) the archive SQLite database.

    Applies WAL mode, foreign key enforcement, and the current schema DDL.
    Returns the open connection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_DDL)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations."""
    row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row else 0

    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # Future migrations: add elif current < N blocks here
