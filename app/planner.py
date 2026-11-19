"""Picks each exercise when it's needed, from the learner's state and what happened so far this session.

Order of preference: a new item due back on its ladder, a warm-up at the start, a ready mission,
a new item (while fewer than two are still being learned), a due review, then extra practice.

Everything Claude needs is spelled out in plain language. Ids are labels only, which Claude
hands back in log_attempt.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app import settings
from app.curriculum import Curriculum, Function
from app.engine import LEVEL_NAMES, Learner, local_day
from app.models import Exercise, Item, Mode, Part, Scene, VocabEntry

# Exercise mix per level: (mode, weight)
MIXES = {
    0: [("hear", 50), ("cued", 50)],
    1: [("hear", 30), ("cued", 30), ("say", 40)],
    2: [("hear", 20), ("cued", 15), ("say", 35), ("situation", 15), ("respond", 15)],
    3: [("hear", 20), ("say", 20), ("situation", 30), ("respond", 30)],
}

INSTRUCTIONS = {
    "repeat": "Introduce the target: say it on its own, then in one short sentence, give the meaning in English, say the Persian once more, and ask the learner to repeat it.",
    "hear": "Say a sentence in Persian only, using the target. The learner gives the meaning in English.",
    "cued": "Give the English for a sentence using the target, plus a cue in Persian script (the target itself, or the key form). The learner says it in Persian.",
    "say": "Give only the English for a sentence using the target. The learner says it in Persian.",
    "situation": "Describe a moment in the scene in English so the target comes naturally. The learner says what fits, in Persian. If they answer well without the target, say so and nudge once toward it by meaning, never by grammar name.",
    "respond": "Play someone in the scene and say one line in Persian that invites the target. The learner understands it and replies in Persian. If they answer well without the target, say so and nudge once toward it by meaning.",
    "mission": "Run this function's mission as a short role-play: set the situation in English, play the other side in Persian, and keep it to a few exchanges. Log the whole role-play as one attempt with mode 'mission' and this function.",
}

NEUTRAL_SCENE = Scene(function=None, theme="daily", setting="Chatting with a friend over tea")

# A new item's ladder within its first session: (mode, exercises to wait since the last step).
LADDER: list[tuple[Mode, int]] = [("repeat", 0), ("hear", 2), ("cued", 4), ("say", 8), ("say", 12)]
STILL_LEARNING = 3  # an item on a step before this counts toward settings.LEARNING_MAX


@dataclass
class Rung:
    """Where a new item is on its ladder: the step it does next, and when it was last practised."""

    step: int
    last: int  # the session's attempt count when it was last practised


@dataclass
class LiveSession:
    """What's happened so far in the current session. Lives in memory only."""

    id: int
    started_at: datetime
    budget_s: int
    last_activity: datetime
    new_queue: list[str]  # new items still to introduce, in order
    mission: str | None  # a function whose mission is still to offer
    handed_out: int = 0
    logged: int = 0
    practised: set[str] = field(default_factory=set)
    passed: set[str] = field(default_factory=set)  # skipped reviews, not offered again this session
    introduced: set[str] = field(default_factory=set)
    learning: dict[str, Rung] = field(default_factory=dict)
    scene: Scene | None = None
    scene_since: int = 0  # handed_out when the current scene started
    pending: Exercise | None = None
    recent_modes: list[str] = field(default_factory=list)

    def seconds_left(self, now: datetime) -> int:
        return max(0, int(self.budget_s - (now - self.started_at).total_seconds()))

    def still_learning(self) -> int:
        return sum(r.step < STILL_LEARNING for r in self.learning.values())

    def record(self, item_id: str, outcome: str) -> None:
        """Move a new item along its ladder after an attempt: up on correct, same step on helped,
        two steps back on wrong. It leaves the ladder after the last step."""
        rung = self.learning.get(item_id)
        if rung is None:
            return
        if outcome == "correct":
            rung.step += 1
        elif outcome == "wrong":
            rung.step = max(0, rung.step - 2)
        rung.last = self.logged
        if rung.step >= len(LADDER):
            del self.learning[item_id]


@dataclass
class NewMaterial:
    """What's new today: items for their first encounter, and a mission if one is ready."""

    new_items: list[str] = field(default_factory=list)
    mission: str | None = None


class Planner:
    def __init__(self, curriculum: Curriculum, learner: Learner):
        self.cur = curriculum
        self.learner = learner

    # ----- describing items -----

    def describe(self, item_id: str) -> Item:
        item = self.cur.item(item_id)
        level = self.learner.level(item_id)
        out = Item(id=item_id, kind=item.kind, state=LEVEL_NAMES[level] if level >= 0 else None)
        if item.kind == "word":
            out.fa, out.en, out.part_of_speech = item.fa, item.en, item.pos
            out.forms = item.forms or None
        elif item.kind == "phrase":
            out.fa, out.en = item.fa, item.en
            if item.slot:
                out.slot = f"The … takes one easy {item.slot}."
        elif item.kind == "grammar":
            out.name, out.how, out.example, out.example_en = item.name, item.how.strip(), item.example, item.example_en
        elif item.kind == "function":
            out.name, out.mission = item.name, item.mission.strip()
            out.phrases = [self.cur.phrases[p].fa for p in item.phrases]
        return out

    def name(self, item_id: str) -> str:
        item = self.cur.item(item_id)
        if item.kind in ("word", "phrase"):
            return f"{item.fa} ({item.en})"
        return f"{item.label} {item.name}"

    # ----- choosing modes -----

    def pick_mode(self, item_id: str, rng: random.Random, recent: list[str]) -> Mode:
        st = self.learner.items[item_id]
        options = MIXES[st.level]
        if len(options) > 1:
            options = [o for o in options if o[0] != st.last_mode] or options
        if len(recent) >= 2 and recent[-1] == recent[-2]:
            options = [o for o in options if o[0] != recent[-1]] or options
        modes, weights = zip(*options)
        return rng.choices(modes, weights)[0]

    # ----- new material -----

    def new_material(self, now: datetime) -> NewMaterial:
        plan = NewMaterial()
        gate = self.learner.gate(now)
        if not gate.open:
            return plan
        L, cur = self.learner, self.cur

        for f in L.open_functions():
            if L.mission_ready(f, now):
                plan.mission = f.id
                break

        grammar: list[str] = []
        stage0 = [g.id for g in cur.grammar.values() if g.stage == 0 and not L.unlocked(g.id)]
        if stage0:
            grammar = stage0[:1]  # Stage 0 patterns start one a day, in order
        elif not L.grammar_in_flight():
            for g in cur.grammar.values():
                if not L.unlocked(g.id) and L.stage_open(g.stage) and all(L.level(n) >= 2 for n in g.needs):
                    grammar = [g.id]
                    break

        first_day = not L.attempts or all(local_day(a.at) == local_day(now) for a in L.attempts)
        free_phrases = []
        if first_day:
            for fid in list(cur.functions)[:2]:  # greeting and thanking start on day one
                free_phrases += [p for p in cur.functions[fid].phrases if not L.unlocked(p)]

        spa, ipa = L.seconds_per_attempt(), L.items_per_attempt()
        cost = 4 / 7 * spa / ipa
        cap = min(settings.MAX_NEW_PER_DAY, math.floor(gate.headroom_seconds / cost))
        if not L.stage_0_complete():
            cap = min(cap, settings.MAX_NEW_PER_DAY_STAGE_0)
        grammar_today = any(cur.item(i).kind == "grammar" for i in L.new_today(now))
        if (grammar and not stage0) or (grammar_today and L.stage_0_complete()):
            cap = min(cap, settings.MAX_NEW_ON_GRAMMAR_DAY)
        already = [
            i for i in L.new_today(now)
            if cur.item(i).kind in ("word", "phrase") and not (first_day and i in self._day_one_phrases())
        ]
        remaining = max(0, cap - len(already))

        others: list[str] = []
        open_fns = L.open_functions()
        for f in open_fns:
            others += [p for p in f.phrases if not L.unlocked(p) and p not in free_phrases]
        words = [w for f in open_fns for w in f.core_words]
        favoured = {"food", "family", "travel"}
        words += [w.id for w in cur.words.values() if favoured & set(w.tags)]
        words += list(cur.words)
        seen = set()
        for w in words:
            if w not in seen and not L.unlocked(w) and L.word_allowed(w, also_known=set(grammar)):
                others.append(w)
            seen.add(w)
        others = list(dict.fromkeys(others))[:remaining]

        # Interleave kinds so a session doesn't spend its first ten minutes on one kind of item.
        lanes = [free_phrases, grammar, others]
        order: list[str] = []
        while any(lanes):
            for lane in lanes:
                if lane:
                    order.append(lane.pop(0))
        plan.new_items = order[: max(0, settings.NEW_ITEMS_PER_DAY - len(L.new_today(now)))]
        return plan

    def _day_one_phrases(self) -> set[str]:
        return {p for fid in list(self.cur.functions)[:2] for p in self.cur.functions[fid].phrases}

    # ----- scenes -----

    def scene_for(self, f: Function | None) -> Scene:
        if f is None:
            return NEUTRAL_SCENE.model_copy()
        return Scene(
            function=f.id,
            function_name=f.name,
            theme=f.tags[0] if f.tags else "daily",
            setting=f"A short everyday scene around this situation (not the mission itself): {f.mission.strip()}",
        )

    def _function_fit(self, item_ids: list[str], open_fns: list[Function]) -> Function | None:
        for i in item_ids[:1]:
            owner = self.cur.function_of(i)
            if owner and owner in open_fns:
                return owner
        tags = Counter(t for i in item_ids for t in getattr(self.cur.item(i), "tags", []) or [])
        if not tags:
            return None
        best = max(open_fns, key=lambda f: (sum(tags[t] for t in f.tags), -f.order), default=None)
        return best if best and sum(tags[t] for t in best.tags) > 0 else None

    def _vocab(self) -> dict:
        """Words and set phrases, the two kinds Claude can use as filler."""
        return {**self.cur.words, **self.cur.phrases}

    def _entry(self, item_id: str) -> VocabEntry:
        item = self._vocab()[item_id]
        return VocabEntry(id=item_id, fa=item.fa, en=item.en)

    def easy_words(self, now: datetime, scene: Scene, exclude: set[str]) -> list[VocabEntry]:
        """Up to 15 well-known words and set phrases that fit the scene."""
        L, vocab = self.learner, self._vocab()
        scene_tags = set()
        if scene.function:
            scene_tags = set(self.cur.functions[scene.function].tags)
        recent = now - timedelta(days=3)
        good = [
            i for i in vocab
            if L.unlocked(i) and i not in exclude and (
                L.level(i) >= 3 or (L.items[i].last_outcome == "correct" and L.items[i].last_at >= recent)
            )
        ]
        fitting = [i for i in good if not vocab[i].tags or scene_tags & set(vocab[i].tags)]
        chosen = (fitting + [i for i in good if i not in fitting])[: settings.EASY_WORDS_MAX]
        if len(chosen) < 5:
            chosen += [i for i in vocab if L.unlocked(i) and i not in exclude and i not in chosen][: 5 - len(chosen)]
        return [self._entry(i) for i in chosen]

    def known_vocabulary(self, now: datetime, session_id: int) -> list[VocabEntry]:
        """The learner's best-known words and set phrases, for the whole session.

        Highest level first, then most likely to be remembered; ties are shuffled per session
        so the list varies from day to day.
        """
        L = self.learner
        rng = random.Random(session_id)
        pool = [i for i in self._vocab() if L.unlocked(i)]
        rng.shuffle(pool)
        pool.sort(key=lambda i: (-L.level(i), -round(L.retrievability(i, now), 1)))
        return [self._entry(i) for i in pool[: settings.KNOWN_VOCABULARY_MAX]]

    # ----- picking the next exercise -----

    def start(self, now: datetime, session_id: int, budget_s: int) -> LiveSession:
        new = self.new_material(now)
        return LiveSession(session_id, now, budget_s, now, new_queue=list(new.new_items), mission=new.mission)

    def next_exercise(self, now: datetime, s: LiveSession) -> Exercise | None:
        ex = self._pick(now, s)
        if ex is not None:
            s.handed_out += 1
            ex.exercise = s.handed_out
            if ex.mode != "mission":
                s.recent_modes.append(ex.mode)
        s.pending = ex
        return ex

    def _pick(self, now: datetime, s: LiveSession) -> Exercise | None:
        L = self.learner
        rng = random.Random(s.id * 7919 + s.handed_out)
        closing = s.seconds_left(now) <= settings.CLOSING_SECONDS

        # 1. A new item due back on its ladder. Near the end, every unfinished one is due.
        def overdue(i: str) -> int:
            r = s.learning[i]
            return s.logged - r.last - LADDER[r.step][1]

        due_back = [i for i in s.learning if closing or overdue(i) >= 0]
        if due_back:
            return self._ladder_exercise(max(due_back, key=overdue), s, now)

        # 2. Warm-up: a couple of the best-known items, for an easy start.
        if s.handed_out < settings.WARMUP_MAX:
            warm = sorted(
                (i for i in L.items if L.level(i) == 3 and i not in s.practised and self.cur.is_schedulable(i)),
                key=lambda i: -L.retrievability(i, now),
            )
            if warm:
                return self._exercise(warm[0], rng.choice(["say", "situation"]), "warm-up", [], s, now)

        # 3. A ready mission, once nothing new is mid-ladder.
        if s.mission and s.still_learning() == 0:
            f = self.cur.functions[s.mission]
            s.mission = None
            scene = self.scene_for(f)
            s.scene, s.scene_since = scene, s.handed_out
            return Exercise(
                part="mission", mode="mission", instruction=INSTRUCTIONS["mission"],
                target=self.describe(f.id), function=f.id, include=[], easy_words=[], new_scene=scene,
            )

        # 4. Something new, while fewer than LEARNING_MAX items are still being learned.
        if s.new_queue and not closing and s.still_learning() < settings.LEARNING_MAX:
            item_id = s.new_queue.pop(0)
            s.learning[item_id] = Rung(step=0, last=s.logged)
            s.introduced.add(item_id)
            return self._ladder_exercise(item_id, s, now)

        # 5. A due review, most at risk of being forgotten first.
        due = [i for i in L.due_items(now) if i not in s.practised and i not in s.passed and i not in s.learning]
        if due:
            i = due[0]
            include: list[str] = []
            if L.level(i) >= 2 and self.cur.item(i).kind != "phrase":
                include = [c for c in due[1:] if L.level(c) >= 2][: settings.INCLUDE_MAX]
            return self._exercise(i, self.pick_mode(i, rng, s.recent_modes), "review", include, s, now)

        # 6. Nothing due: bring a new item back early rather than wait.
        if s.learning:
            return self._ladder_exercise(max(s.learning, key=overdue), s, now)

        # 7. Extra practice: unlocked items not yet practised this session.
        pool = sorted(
            (i for i in L.items if i not in s.practised and i not in s.passed and self.cur.is_schedulable(i)),
            key=lambda i: L.retrievability(i, now),
        )
        if pool:
            return self._exercise(pool[0], self.pick_mode(pool[0], rng, s.recent_modes), "extra", [], s, now)
        return None

    def _ladder_exercise(self, item_id: str, s: LiveSession, now: datetime) -> Exercise:
        step = s.learning[item_id].step
        mode = LADDER[step][0]
        ex = self._exercise(item_id, mode, "new", [], s, now)
        ex.new_step = f"step {step + 1} of {len(LADDER)}"
        if step == 0:
            ex.instruction = "New item. " + INSTRUCTIONS["repeat"]
        else:
            ex.instruction = (
                f"New item, step {step + 1} of {len(LADDER)}: the learner met it a few minutes ago. "
                + INSTRUCTIONS[mode]
            )
        return ex

    def _scene(self, item_id: str, s: LiveSession) -> tuple[Scene, bool]:
        """The scene for this item. A scene lasts at least settings.SCENE_MIN_EXERCISES exercises; after
        that it changes when the item belongs to a different situation."""
        if s.scene is not None and s.handed_out - s.scene_since < settings.SCENE_MIN_EXERCISES:
            return s.scene, False
        f = self._function_fit([item_id], self.learner.open_functions())
        if s.scene is not None and (f is None or s.scene.function == f.id):
            return s.scene, False
        s.scene = self.scene_for(f)
        s.scene_since = s.handed_out
        return s.scene, True

    def _exercise(self, item_id: str, mode: Mode, part: Part, include: list[str], s: LiveSession,
                  now: datetime) -> Exercise:
        item = self.cur.item(item_id)
        scene, changed = self._scene(item_id, s)
        ex = Exercise(
            part=part,
            mode=mode,
            instruction=INSTRUCTIONS[mode],
            target=self.describe(item_id),
            include=[self.describe(c) for c in include],
            easy_words=self.easy_words(now, scene, {item_id, *include}),
            function=scene.function,
            new_scene=scene if changed else None,
        )
        if include:
            ex.include_note = "Work in at most two of these, from the front of the list, only if they fit naturally."
        if item.kind == "word":
            carrier = self._carrier()
            if carrier:
                ex.carry_with = f"Build the sentence on a pattern the learner knows well: {carrier}."
        if item.kind == "phrase":
            ex.note = "Use the set phrase whole; don't break it into its grammar."
        return ex

    def _carrier(self) -> str | None:
        known = [g for g in self.cur.grammar.values() if self.learner.unlocked(g.id)]
        if not known:
            return None
        best = max(known, key=lambda g: (self.learner.level(g.id), -g.order))
        return best.name
