"""The practice service behind the MCP tools: sessions, logging attempts, progress."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

from app import settings
from app.curriculum import Curriculum
from app.db import Database
from app.engine import LEVEL_NAMES, OUTCOME_RANK, Learner
from app.models import (
    FunctionProgress,
    Ignored,
    Logged,
    LogAttemptResult,
    Missed,
    Mode,
    MovedUp,
    NotYet,
    ProgressResult,
    SkipResult,
    StartSessionResult,
    Summary,
)
from app.planner import LiveSession, Planner


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Practice:
    def __init__(self, curriculum: Curriculum, db: Database, clock: Callable[[], datetime] = utcnow):
        self.cur = curriculum
        self.db = db
        self.clock = clock
        self.learner = Learner(curriculum)
        for review in db.reviews():
            self.learner.apply(review)
        self.planner = Planner(curriculum, self.learner)
        self.session: LiveSession | None = None

    # ----- sessions -----

    def _new_session(self, now: datetime) -> LiveSession:
        if self.session:
            self.db.end_session(self.session.id, now)
        budget = settings.BUDGET_MINUTES * 60
        row = self.db.start_session(now, budget)
        self.session = self.planner.start(now, row.id, budget)
        return self.session

    def _current(self, now: datetime) -> LiveSession:
        s = self.session
        if s is None or (now - s.last_activity) > timedelta(minutes=settings.SESSION_IDLE_MINUTES):
            s = self._new_session(now)
        return s

    def _summary(self, now: datetime) -> Summary:
        L = self.learner
        levels = Counter(LEVEL_NAMES[st.level] for i, st in L.items.items() if self.cur.is_schedulable(i))
        return Summary(
            curriculum_version=self.cur.version,
            stage=L.current_stage(),
            items_by_state=dict(levels),
            due_today=len(L.due_items(now)),
            functions_done=[self.cur.functions[f].name for f, fs in L.functions.items() if fs.done_at],
            attempts_so_far=len(L.attempts),
        )

    def start_session(self) -> StartSessionResult:
        now = self.clock()
        s = self._new_session(now)
        new_today = [self.planner.name(i) for i in s.new_queue]
        return StartSessionResult(
            session_id=s.id,
            summary=self._summary(now),
            seconds_left=s.seconds_left(now),
            known_grammar=[self.planner.name(g) for g in self.learner.known_grammar()],
            known_vocabulary=self.planner.known_vocabulary(now, s.id),
            grammar_in_flight=[self.planner.name(g) for g in self.learner.grammar_in_flight()],
            new_today=new_today,
            exercise=self.planner.next_exercise(now, s),
            note=(
                "Run this exercise, wait for the learner's answer, then call log_attempt. Each log_attempt "
                "returns the next exercise. When an exercise carries new_scene, move into it with a short bridge."
            ),
        )

    def skip_exercise(self) -> SkipResult:
        """Set the current exercise aside and get a different one."""
        now = self.clock()
        s = self._current(now)
        s.last_activity = now
        if s.pending is not None:
            tid = s.pending.target.id
            rung = s.learning.get(tid)
            if rung is not None and rung.step == 0:
                del s.learning[tid]  # not introduced after all: back of the queue
                s.introduced.discard(tid)
                s.new_queue.append(tid)
            elif rung is not None:
                rung.last = s.logged  # try it again a little later
            elif s.pending.mode != "mission":
                s.passed.add(tid)
        return SkipResult(exercise=self.planner.next_exercise(now, s), seconds_left=s.seconds_left(now))

    # ----- logging -----

    def log_attempt(
        self,
        sentence_fa: str,
        mode: Mode,
        words: list[str] | None = None,
        phrases: list[str] | None = None,
        grammar: list[str] | None = None,
        missed: list[Missed] | None = None,
        function: str | None = None,
        error_note: str | None = None,
    ) -> LogAttemptResult:
        now = self.clock()
        s = self._current(now)

        outcomes: dict[str, str] = {}
        for i in [*(words or []), *(phrases or []), *(grammar or [])]:
            outcomes.setdefault(i, "correct")
        for m in missed or []:
            if m.id not in outcomes or OUTCOME_RANK[m.outcome] > OUTCOME_RANK[outcomes[m.id]]:
                outcomes[m.id] = m.outcome

        logged: list[tuple[str, str]] = []
        ignored: list[Ignored] = []
        for i, outcome in outcomes.items():
            if not self.cur.is_schedulable(i):
                ignored.append(Ignored(id=i, why="not a word, set phrase or grammar pattern id from the exercises"))
            elif not self.learner.unlocked(i) and i not in s.introduced:
                ignored.append(Ignored(id=i, why="not introduced yet, so it isn't tracked"))
            else:
                logged.append((i, outcome))
        if function is not None and function not in self.cur.functions:
            ignored.append(Ignored(id=function, why="unknown function"))
            function = None
        if mode == "mission" and function is None:
            raise ValueError("a mission attempt needs the function it was for")

        review = self.db.add_review(now, s.id, sentence_fa, mode, logged, function, error_note)
        changes = self.learner.apply(review)
        s.practised.update(i for i, _ in logged)
        s.last_activity = now
        s.logged += 1
        for i, outcome in logged:
            s.record(i, outcome)

        result = LogAttemptResult(
            logged=[Logged(id=i, outcome=o) for i, o in logged],
            moved_up=[MovedUp(item=self.planner.name(i), now=LEVEL_NAMES[self.learner.level(i)]) for i in changes.moved_up],
            seconds_left=s.seconds_left(now),
            ignored=ignored or None,
        )
        if mode == "mission" and function:
            result.mission_passed = all(o != "wrong" for _, o in logged)
        result.next = self.planner.next_exercise(now, s)
        if result.seconds_left <= 0:
            self.db.end_session(s.id, now)
            result.done = (
                "Today's time is up. Wrap up warmly in a sentence or two. If the learner wants to keep going, "
                "carry on with next."
            )
        elif result.next is None:
            result.done = "Nothing left to practise right now. Wrap up warmly."
        elif result.next.part == "extra":
            result.note = "Today's practice is done; this is extra. Offer to stop here."
        return result

    # ----- progress -----

    def get_progress(self) -> ProgressResult:
        now = self.clock()
        L = self.learner
        groups: dict[str, list[str]] = {name: [] for name in LEVEL_NAMES}
        for i, st in L.items.items():
            if self.cur.is_schedulable(i):
                groups[LEVEL_NAMES[st.level]].append(self.planner.name(i))
        not_yet = [self.planner.name(i) for i in [*self.cur.grammar, *self.cur.phrases, *self.cur.words] if not L.unlocked(i)]
        gate = L.gate(now)
        return ProgressResult(
            by_state=groups,
            shaky=[self.planner.name(i) for i, st in L.items.items() if st.lapses and self.cur.is_schedulable(i)],
            not_yet=NotYet(count=len(not_yet), next_few=not_yet[:8]),
            functions=[
                FunctionProgress(
                    function=f.name,
                    status="done" if L.function_done(f.id) else ("open" if L.function_open(f) else "not yet"),
                    mission_ready=L.mission_ready(f, now),
                )
                for f in self.cur.functions.values()
            ],
            stage=L.current_stage(),
            accuracy_last_50=gate.accuracy,
            forecast_seconds_per_day=gate.forecast_seconds,
            new_material_allowed=gate.open,
        )
