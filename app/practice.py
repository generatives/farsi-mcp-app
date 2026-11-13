"""The practice service behind the MCP tools: sessions, logging attempts, progress."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from app import settings
from app.curriculum import Curriculum
from app.db import Database
from app.engine import LEVEL_NAMES, OUTCOME_RANK, Learner
from app.models import (
    Exercise,
    FunctionProgress,
    Ignored,
    Logged,
    LogAttemptResult,
    Missed,
    Mode,
    MovedUp,
    NextItemsResult,
    NotYet,
    Plan,
    ProgressResult,
    SomethingNew,
    StartSessionResult,
    Summary,
)
from app.planner import Planner

LOOKAHEAD = 3  # start_session hands out this many; each log_attempt hands out one more


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LiveSession:
    id: int
    started_at: datetime
    budget_s: int
    queue: list[Exercise]
    handed_out: int = 0
    last_activity: datetime | None = None
    practised: set[str] = field(default_factory=set)
    new_items: set[str] = field(default_factory=set)

    def seconds_left(self, now: datetime) -> int:
        return max(0, int(self.budget_s - (now - self.started_at).total_seconds()))


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
        queue = self.planner.plan_session(now, row.id, budget)
        live = LiveSession(row.id, now, budget, queue, last_activity=now)
        live.new_items = {ex.target.id for ex in queue if ex.first_encounter}
        for n, ex in enumerate(queue, 1):
            ex.exercise = n
        self.session = live
        return live

    def _current(self, now: datetime) -> LiveSession:
        s = self.session
        if s is None or (now - (s.last_activity or s.started_at)) > timedelta(minutes=settings.SESSION_IDLE_MINUTES):
            s = self._new_session(now)
        return s

    def _next(self, s: LiveSession, count: int) -> list[Exercise]:
        """Hand out the next exercises, skipping review targets already practised this session."""
        out = []
        while len(out) < count and s.handed_out < len(s.queue):
            ex = s.queue[s.handed_out]
            s.handed_out += 1
            if not ex.first_encounter and ex.mode != "mission" and ex.target.id in s.practised:
                continue
            out.append(ex)
        return out

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
        first = self._next(s, LOOKAHEAD)
        new_ids = dict.fromkeys(ex.target.id for ex in s.queue if ex.first_encounter)
        mission = next((ex for ex in s.queue if ex.mode == "mission"), None)
        return StartSessionResult(
            session_id=s.id,
            summary=self._summary(now),
            seconds_left=s.seconds_left(now),
            plan=Plan(
                warm_up=sum(ex.part == "warm-up" for ex in s.queue),
                scene_exercises=sum(ex.part == "scene" for ex in s.queue),
                scenes=[ex.new_scene for ex in s.queue if ex.new_scene and ex.part == "scene"],
                something_new=SomethingNew(
                    gate_open=self.learner.gate(now).open,
                    mission=mission.target.name if mission else None,
                    new_items=[self.planner.name(i) for i in new_ids],
                ),
            ),
            known_grammar=[self.planner.name(g) for g in self.learner.known_grammar()],
            known_vocabulary=self.planner.known_vocabulary(now, s.id),
            grammar_in_flight=[self.planner.name(g) for g in self.learner.grammar_in_flight()],
            leeches=[self.planner.name(i) for i in self.learner.leeches()],
            exercises=first,
            note=(
                "Run these exercises in order, one at a time, and call log_attempt after each. "
                "Each log_attempt returns the next exercise. When an exercise carries new_scene, move into that scene "
                "with a short bridge."
            ),
        )

    def next_items(self, limit: int = 3) -> NextItemsResult:
        now = self.clock()
        s = self._current(now)
        s.last_activity = now
        out = self._next(s, limit)
        if not out:
            out = self.planner.extra(now, s.practised, limit, s.id)
        return NextItemsResult(exercises=out, seconds_left=s.seconds_left(now))

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
            elif not self.learner.unlocked(i) and i not in s.new_items:
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

        result = LogAttemptResult(
            logged=[Logged(id=i, outcome=o) for i, o in logged],
            moved_up=[MovedUp(item=self.planner.name(i), now=LEVEL_NAMES[self.learner.level(i)]) for i in changes.moved_up],
            new_leeches=[self.planner.name(i) for i in changes.new_leeches],
            seconds_left=s.seconds_left(now),
            ignored=ignored or None,
        )
        if mode == "mission" and function:
            result.mission_passed = all(o != "wrong" for _, o in logged)
        if result.seconds_left <= 0:
            self.db.end_session(s.id, now)
            result.done = (
                "Today's time is up. Wrap up warmly in a sentence or two. If the learner wants to keep going, "
                "call next_items for extra practice."
            )
            return result
        nxt = self._next(s, 1)
        if not nxt:
            nxt = self.planner.extra(now, s.practised, 1, s.id)
            if nxt:
                result.note = "The plan for today is done; this is extra practice. Offer to stop here."
        result.next = nxt[0] if nxt else None
        if not nxt:
            result.done = "Nothing left to practise right now. Wrap up warmly."
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
