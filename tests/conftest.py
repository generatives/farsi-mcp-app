from datetime import datetime, timedelta, timezone

import pytest

from app import curriculum
from app.db import Database
from app.practice import Practice


class Clock:
    def __init__(self):
        # 6 PM in Vancouver
        self.now = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def cur():
    return curriculum.load("curriculum")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "farsi.db"))


@pytest.fixture
def practice(cur, db, clock):
    return Practice(cur, db, clock=clock)


def run_exercise(p: Practice, ex: dict, clock: Clock, missed=None):
    """Log an exercise as Claude would, crediting just its target."""
    t = ex["target"]
    kind = {"word": "words", "phrase": "phrases", "grammar": "grammar"}.get(t["kind"])
    clock.advance(seconds=40)
    kwargs = {kind: [t["id"]]} if kind else {}
    return p.log_attempt("جمله", ex["mode"], missed=missed or [], function=ex.get("function"), **kwargs)
