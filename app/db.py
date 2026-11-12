"""SQLite storage. The file lives on local disk; Litestream replicates it to Cloud Storage."""

import os
import sqlite3
from contextlib import contextmanager

from app.words import WORDS

DB_PATH = os.environ.get("DB_PATH", "/data/farsi.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY,
    fa TEXT NOT NULL UNIQUE,
    translit TEXT NOT NULL,
    en TEXT NOT NULL,
    example_fa TEXT NOT NULL,
    example_translit TEXT NOT NULL,
    example_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS served (
    id INTEGER PRIMARY KEY,
    word_id INTEGER NOT NULL REFERENCES words(id),
    served_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connect() as conn:
        # WAL mode is required by Litestream; busy_timeout avoids lock errors during its checkpoints.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO words (fa, translit, en, example_fa, example_translit, example_en) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            WORDS,
        )


def pick_word() -> dict:
    """Pick the least-recently-served word, record that it was served, and return it."""
    with connect() as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute(
            """
            SELECT w.*, MAX(s.served_at) AS last_served, COUNT(s.id) AS times_served
            FROM words w LEFT JOIN served s ON s.word_id = w.id
            GROUP BY w.id
            ORDER BY last_served IS NOT NULL, last_served, RANDOM()
            LIMIT 1
            """
        ).fetchone()
        conn.execute("INSERT INTO served (word_id) VALUES (?)", (row["id"],))
        total = conn.execute("SELECT COUNT(*) FROM served").fetchone()[0]
    return {
        "word": {"fa": row["fa"], "translit": row["translit"], "en": row["en"]},
        "example": {"fa": row["example_fa"], "translit": row["example_translit"], "en": row["example_en"]},
        "times_served_before": row["times_served"],
        "total_words_served": total,
    }
