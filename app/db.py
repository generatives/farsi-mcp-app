"""SQLite storage: a log of sessions and attempts, and nothing derived from it.

The file lives on local disk; Litestream replicates it to Cloud Storage. All learner state
(FSRS cards, levels, unlocks, function progress) is rebuilt in memory from this log.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    budget_s   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    sentence_fa TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('repeat', 'hear', 'cued', 'say', 'situation', 'respond', 'mission')),
    error_note  TEXT,
    function_id TEXT
);
CREATE TABLE IF NOT EXISTS review_items (
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    item_id   TEXT NOT NULL,
    outcome   TEXT NOT NULL CHECK (outcome IN ('correct', 'helped', 'wrong'))
);
"""


@dataclass
class Review:
    id: int
    at: datetime
    session_id: int
    sentence_fa: str
    mode: str
    error_note: str | None
    function_id: str | None
    items: list[tuple[str, str]]  # (item_id, outcome)


@dataclass
class Session:
    id: int
    started_at: datetime
    ended_at: datetime | None
    budget_s: int


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode is required by Litestream; busy_timeout avoids lock errors during its checkpoints.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def start_session(self, at: datetime, budget_s: int) -> Session:
        with self._tx() as c:
            cur = c.execute("INSERT INTO sessions (started_at, budget_s) VALUES (?, ?)", (at.isoformat(), budget_s))
        return Session(cur.lastrowid, at, None, budget_s)

    def end_session(self, session_id: int, at: datetime) -> None:
        with self._tx() as c:
            c.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (at.isoformat(), session_id))

    def sessions(self) -> list[Session]:
        rows = self._conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        return [
            Session(
                r["id"],
                datetime.fromisoformat(r["started_at"]),
                datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else None,
                r["budget_s"],
            )
            for r in rows
        ]

    def add_review(
        self,
        at: datetime,
        session_id: int,
        sentence_fa: str,
        mode: str,
        items: list[tuple[str, str]],
        function_id: str | None = None,
        error_note: str | None = None,
    ) -> Review:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO reviews (at, session_id, sentence_fa, mode, error_note, function_id) VALUES (?, ?, ?, ?, ?, ?)",
                (at.isoformat(), session_id, sentence_fa, mode, error_note, function_id),
            )
            c.executemany(
                "INSERT INTO review_items (review_id, item_id, outcome) VALUES (?, ?, ?)",
                [(cur.lastrowid, item_id, outcome) for item_id, outcome in items],
            )
        return Review(cur.lastrowid, at, session_id, sentence_fa, mode, error_note, function_id, list(items))

    def reviews(self) -> list[Review]:
        """Every attempt with its items, oldest first."""
        reviews: dict[int, Review] = {}
        for r in self._conn.execute("SELECT * FROM reviews ORDER BY at, id"):
            reviews[r["id"]] = Review(
                r["id"], datetime.fromisoformat(r["at"]), r["session_id"], r["sentence_fa"], r["mode"],
                r["error_note"], r["function_id"], [],
            )
        for r in self._conn.execute("SELECT review_id, item_id, outcome FROM review_items ORDER BY rowid"):
            if r["review_id"] in reviews:
                reviews[r["review_id"]].items.append((r["item_id"], r["outcome"]))
        return list(reviews.values())
