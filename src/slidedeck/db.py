"""Low-level SQLite schema and connection helpers for a slidedeck project."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pptx_path TEXT UNIQUE NOT NULL,
    pptx_mtime REAL NOT NULL,
    pdf_path TEXT,
    pdf_mtime REAL,
    hidden_pdf_path TEXT,
    hidden_pdf_mtime REAL,
    last_scanned REAL
);

CREATE TABLE IF NOT EXISTS slides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    index_in_deck INTEGER NOT NULL,
    visible_pdf_page INTEGER,
    hidden INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    UNIQUE(deck_id, index_in_deck)
);

CREATE INDEX IF NOT EXISTS idx_slides_deck ON slides(deck_id);

CREATE TABLE IF NOT EXISTS slide_embeddings (
    slide_id INTEGER PRIMARY KEY REFERENCES slides(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    updated_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS slides_fts USING fts5(
    text,
    content='slides',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS slides_ai AFTER INSERT ON slides BEGIN
    INSERT INTO slides_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS slides_ad AFTER DELETE ON slides BEGIN
    INSERT INTO slides_fts(slides_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS slides_au AFTER UPDATE ON slides BEGIN
    INSERT INTO slides_fts(slides_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO slides_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the project database, creating the schema if needed."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn
