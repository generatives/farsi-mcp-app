"""Learner state, rebuilt by replaying the attempt log.

Nothing here is stored. `Learner.apply` folds one logged attempt into the in-memory state, and
the same function is used both at startup (replaying the whole log) and after each new attempt,
so the two can never disagree.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from fsrs import Card, Rating, Scheduler

from app import settings
from app.curriculum import POS_NEEDS, Curriculum, Function
from app.db import Review

RATINGS = {"correct": Rating.Good, "helped": Rating.Hard, "wrong": Rating.Again}
OUTCOME_RANK = {"correct": 0, "helped": 1, "wrong": 2}
PRODUCING = {"say", "situation", "respond", "mission"}

LEVEL_NAMES = ["introduced", "recognizes", "produces with help", "produces freely"]


def local_day(at: datetime) -> date:
    return at.astimezone(settings.TIMEZONE).date()


def worst(outcomes) -> str | None:
    outcomes = list(outcomes)
    return max(outcomes, key=OUTCOME_RANK.__getitem__) if outcomes else None


@dataclass
class ItemState:
    item_id: str
    card: Card
    first_at: datetime
    level: int = 0
    attempts: int = 0
    lapses: int = 0
    last_at: datetime | None = None
    last_mode: str | None = None
    last_outcome: str | None = None
    # Evidence toward the next level. Cleared whenever the level changes.
    hear_ok_days: set = field(default_factory=set)
    cued_ok_days: set = field(default_factory=set)
    say_days: set = field(default_factory=set)
    free_days: set = field(default_factory=set)

    @property
    def leech(self) -> bool:
        return self.lapses >= settings.LEECH_LAPSES

    def set_level(self, level: int) -> None:
        if level != self.level:
            self.level = level
            self.hear_ok_days.clear()
            self.cued_ok_days.clear()
            self.say_days.clear()
            self.free_days.clear()


@dataclass
class FunctionState:
    function_id: str
    done_at: datetime | None = None
    checks_passed: int = 0
    next_check: datetime | None = None


@dataclass
class Changes:
    """What one attempt changed: items that moved up a state, and items that just became leeches."""

    moved_up: list[str]
    new_leeches: list[str]


@dataclass
class Gate:
    """Whether new material is allowed today, and why."""

    open: bool
    forecast_seconds: int
    load_limit_seconds: int
    accuracy: float | None
    headroom_seconds: float


@dataclass
class Attempt:
    at: datetime
    session_id: int
    mode: str
    n_items: int
    outcome: str | None  # worst outcome over items that weren't first exposures; None if all were


class Learner:
    def __init__(self, curriculum: Curriculum):
        self.cur = curriculum
        self.scheduler = Scheduler(
            desired_retention=settings.TARGET_RETENTION,
            learning_steps=(),  # practice is daily, so no same-day learning steps
            relearning_steps=(),
            enable_fuzzing=False,  # replay must be deterministic
        )
        self.items: dict[str, ItemState] = {}
        self.functions: dict[str, FunctionState] = {f: FunctionState(f) for f in curriculum.functions}
        self.attempts: list[Attempt] = []

    # ----- replay -----

    def apply(self, review: Review) -> Changes:
        """Fold one attempt into the state. Returns what changed, for the tool response."""
        moved_up: list[str] = []
        new_leeches: list[str] = []
        non_first: list[str] = []
        day = local_day(review.at)
        mode = review.mode

        for item_id, outcome in review.items:
            if not self.cur.is_schedulable(item_id):
                continue  # retired from the curriculum, or a function id: ignore
            st = self.items.get(item_id)
            if st is None:
                st = self.items[item_id] = ItemState(item_id, Card(due=review.at), first_at=review.at)
            first_exposure = st.attempts < settings.FIRST_EXPOSURES
            st.attempts += 1
            st.card, _ = self.scheduler.review_card(st.card, RATINGS[outcome], review.at)
            before_level, was_leech = st.level, st.leech

            if not first_exposure:
                non_first.append(outcome)
                if outcome == "wrong":
                    st.lapses += 1
                    st.set_level(max(0, st.level - 1))
                else:
                    self._progress(st, mode, outcome, day)
            st.last_at, st.last_mode, st.last_outcome = review.at, mode, outcome
            if st.level > before_level:
                moved_up.append(item_id)
            if st.leech and not was_leech:
                new_leeches.append(item_id)

        if mode == "mission" and review.function_id in self.functions:
            self._mission(self.functions[review.function_id], review)

        self.attempts.append(Attempt(review.at, review.session_id, mode, len(review.items), worst(non_first)))
        return Changes(moved_up, new_leeches)

    def _progress(self, st: ItemState, mode: str, outcome: str, day: date) -> None:
        after_first_day = day > local_day(st.first_at)
        if st.level == 0:
            if after_first_day and mode == "hear" and outcome == "correct":
                st.hear_ok_days.add(day)
            if after_first_day and mode == "cued":
                st.cued_ok_days.add(day)
            if st.hear_ok_days and st.cued_ok_days:
                st.set_level(1)
        elif st.level == 1:
            if mode in PRODUCING and outcome == "correct":
                st.say_days.add(day)
            if len(st.say_days) >= 2:
                st.set_level(2)
        elif st.level == 2:
            if mode in PRODUCING and outcome == "correct":
                st.free_days.add(day)
            if len(st.free_days) >= 2:
                st.set_level(3)

    def _mission(self, fs: FunctionState, review: Review) -> None:
        passed = all(outcome != "wrong" for _, outcome in review.items)
        if fs.done_at is None:
            if passed:
                fs.done_at = review.at
                fs.next_check = review.at + timedelta(days=settings.MISSION_CHECK_DAYS[0])
            return
        if passed:
            fs.checks_passed += 1
            days = settings.MISSION_CHECK_DAYS
            fs.next_check = review.at + timedelta(days=days[fs.checks_passed]) if fs.checks_passed < len(days) else None
        else:
            fs.checks_passed = 0
            fs.next_check = review.at + timedelta(days=settings.MISSION_CHECK_DAYS[0])

    # ----- items -----

    def unlocked(self, item_id: str) -> bool:
        return item_id in self.items

    def level(self, item_id: str) -> int:
        st = self.items.get(item_id)
        return st.level if st else -1

    def retrievability(self, item_id: str, now: datetime) -> float:
        return self.scheduler.get_card_retrievability(self.items[item_id].card, now)

    def due_items(self, now: datetime) -> list[str]:
        """Unlocked items due by the end of today, most at risk of being forgotten first."""
        end_of_today = datetime.combine(local_day(now) + timedelta(days=1), datetime.min.time(), settings.TIMEZONE)
        due = [
            i for i, st in self.items.items()
            if not st.leech and st.card.due <= end_of_today and self.cur.is_schedulable(i)
        ]
        return sorted(due, key=lambda i: self.retrievability(i, now))

    def leeches(self) -> list[str]:
        return [i for i, st in self.items.items() if st.leech and self.cur.is_schedulable(i)]

    def new_today(self, now: datetime) -> list[str]:
        today = local_day(now)
        return [i for i, st in self.items.items() if local_day(st.first_at) == today]

    # ----- grammar and stages -----

    def stage_open(self, stage: int) -> bool:
        if stage <= 0:
            return True
        prev = [g for g in self.cur.grammar.values() if g.stage == stage - 1]
        return all(self.level(g.id) >= 2 for g in prev)

    def current_stage(self) -> int:
        stages = sorted({g.stage for g in self.cur.grammar.values()})
        open_ = [s for s in stages if self.stage_open(s)]
        return max(open_) if open_ else 0

    def stage_0_complete(self) -> bool:
        return all(self.level(g.id) >= 2 for g in self.cur.grammar.values() if g.stage == 0)

    def grammar_in_flight(self) -> list[str]:
        """Unlocked grammar patterns (outside Stage 0) not yet at 'produces with help'."""
        return [g.id for g in self.cur.grammar.values() if g.stage > 0 and self.unlocked(g.id) and self.level(g.id) < 2]

    def known_grammar(self) -> list[str]:
        return [g.id for g in self.cur.grammar.values() if self.unlocked(g.id)]

    def word_allowed(self, word_id: str, also_known: set[str] = frozenset()) -> bool:
        """Part-of-speech rule: a word appears only once the grammar to use it is introduced
        (or is being introduced in the same session, passed as also_known)."""
        word = self.cur.words[word_id]
        needed = self.cur.grammar_by_label(POS_NEEDS[word.pos])
        return needed is not None and (self.unlocked(needed.id) or needed.id in also_known)

    # ----- functions -----

    def function_open(self, f: Function) -> bool:
        if not f.needs:
            return True
        first = min((self.cur.grammar[g] for g in f.needs), key=lambda g: g.order)
        return self.stage_open(first.stage)

    def open_functions(self) -> list[Function]:
        return [f for f in self.cur.functions.values() if self.function_open(f)]

    def function_done(self, function_id: str) -> bool:
        return self.functions[function_id].done_at is not None

    def mission_ready(self, f: Function, now: datetime) -> bool:
        fs = self.functions[f.id]
        if fs.done_at is not None:
            return fs.next_check is not None and fs.next_check <= now
        if not self.function_open(f):
            return False
        return all(self.level(g) >= 2 for g in f.needs) and all(
            self.level(i) >= 1 for i in [*f.phrases, *f.core_words]
        )

    # ----- measurements and gates -----

    def _recent(self) -> list[Attempt]:
        return self.attempts[-settings.ACCURACY_WINDOW:]

    def seconds_per_attempt(self) -> float:
        gaps = []
        recent = self.attempts[-(settings.ACCURACY_WINDOW + 1):]
        for a, b in zip(recent, recent[1:]):
            if a.session_id == b.session_id:
                gaps.append(min((b.at - a.at).total_seconds(), 180))
        return statistics.median(gaps) if len(gaps) >= 5 else settings.DEFAULT_SECONDS_PER_ATTEMPT

    def items_per_attempt(self) -> float:
        counts = [a.n_items for a in self._recent() if a.n_items]
        return statistics.median(counts) if len(counts) >= 5 else settings.DEFAULT_ITEMS_PER_ATTEMPT

    def accuracy(self) -> float | None:
        graded = [a.outcome for a in self.attempts if a.outcome is not None][-settings.ACCURACY_WINDOW:]
        if len(graded) < settings.ACCURACY_MIN_ATTEMPTS:
            return None
        return sum(o != "wrong" for o in graded) / len(graded)

    def forecast_mean_seconds(self, now: datetime) -> float:
        today = local_day(now)
        per_day = [0] * 7
        for i, st in self.items.items():
            if st.leech or not self.cur.is_schedulable(i):
                continue
            offset = max(0, (local_day(st.card.due) - today).days)
            if offset < 7:
                per_day[offset] += 1
        spa, ipa = self.seconds_per_attempt(), self.items_per_attempt()
        return statistics.mean(n / ipa * spa for n in per_day)

    def gate(self, now: datetime) -> Gate:
        budget = settings.BUDGET_MINUTES * 60
        forecast = self.forecast_mean_seconds(now)
        accuracy = self.accuracy()
        load_ok = forecast < settings.LOAD_SHARE * budget
        accuracy_ok = accuracy is None or accuracy >= settings.ACCURACY_FLOOR
        return Gate(
            open=load_ok and accuracy_ok,
            forecast_seconds=round(forecast),
            load_limit_seconds=round(settings.LOAD_SHARE * budget),
            accuracy=None if accuracy is None else round(accuracy, 2),
            headroom_seconds=max(0.0, settings.LOAD_SHARE * budget - forecast),
        )
