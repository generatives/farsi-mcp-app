"""Plans a session: warm-up, scenes and something new, as a queue of exercises for Claude.

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
    "repeat": "First encounter, step 1 of 3. Say a short sentence using the target, give its meaning in English, and ask the learner to repeat it.",
    "hear": "Say a sentence in Persian only, using the target. The learner gives the meaning in English.",
    "cued": "Give the English for a sentence using the target, plus a Persian cue (the target itself, or the key form). The learner says it in Persian.",
    "say": "Give only the English for a sentence using the target. The learner says it in Persian.",
    "situation": "Describe a moment in the scene in English so the target comes naturally. The learner says what fits, in Persian. If they answer well without the target, say so and nudge once toward it by meaning, never by grammar name.",
    "respond": "Play someone in the scene and say one line in Persian that invites the target. The learner understands it and replies in Persian. If they answer well without the target, say so and nudge once toward it by meaning.",
    "mission": "Run this function's mission as a short role-play: set the situation in English, play the other side in Persian, and keep it to a few exchanges. Log the whole role-play as one attempt with mode 'mission' and this function.",
}

NEUTRAL_SCENE = Scene(function=None, theme="daily", setting="Chatting with a friend over tea")
FIRST_ENCOUNTER: list[Mode] = ["repeat", "hear", "cued"]


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
            out.fa, out.translit, out.en, out.part_of_speech = item.fa, item.translit, item.en, item.pos
            out.forms = item.forms or None
        elif item.kind == "phrase":
            out.fa, out.translit, out.en = item.fa, item.translit, item.en
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
            grammar = stage0  # Stage 0 patterns start together
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
        plan.new_items = order
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

    # ----- the whole session -----

    def plan_session(self, now: datetime, session_id: int, seconds: float) -> list[Exercise]:
        L = self.learner
        rng = random.Random(session_id)
        spa = L.seconds_per_attempt()
        recent_modes: list[str] = []
        assigned: set[str] = set()
        out: list[Exercise] = []

        # Warm-up: the best-known items, for an easy win.
        warm = sorted((i for i in L.items if L.level(i) == 3),
                      key=lambda i: -L.retrievability(i, now))[: settings.WARMUP_MAX]
        for i in warm:
            mode = rng.choice(["say", "situation"])
            out.append(self._exercise(i, mode, "warm-up", [], NEUTRAL_SCENE, now))
            assigned.add(i)

        new = self.new_material(now)
        new_seconds = settings.NEW_MATERIAL_SECONDS if (new.new_items or new.mission) else 0
        due = [i for i in L.due_items(now) if i not in assigned]
        n_scene = max(0, int((seconds - len(out) * spa - new_seconds) // spa)) if due else 0

        # Targets and their candidates: one hard thing per sentence.
        raw: list[tuple[str, Mode, list[str]]] = []
        for i in due:
            if len(raw) >= n_scene:
                break
            if i in assigned:
                continue
            assigned.add(i)
            mode = self.pick_mode(i, rng, recent_modes)
            recent_modes.append(mode)
            include: list[str] = []
            if L.level(i) >= 2 and self.cur.item(i).kind != "phrase":
                include = [c for c in due if c not in assigned and L.level(c) >= 2][: settings.INCLUDE_MAX]
                assigned.update(include)
            raw.append((i, mode, include))

        # Group into scenes by function.
        open_fns = L.open_functions()
        groups: dict[str | None, list] = {}
        loose = []
        for i, mode, include in raw:
            f = self._function_fit([i, *include], open_fns)
            if f is None and self.cur.item(i).kind == "grammar":
                loose.append((i, mode, include))
            else:
                groups.setdefault(f.id if f else None, []).append((i, mode, include))
        ranked = sorted((k for k in groups if k is not None), key=lambda k: -len(groups[k]))
        scenes = [k for k in ranked[: settings.SCENES_MAX - 1] if len(groups[k]) >= 2] or ranked[:1]
        neutral = [x for k, v in groups.items() if k not in scenes for x in v]
        if loose:
            target = scenes[0] if scenes else None
            if target:
                groups[target] += loose
            else:
                neutral += loose
        ordered = [(self.scene_for(self.cur.functions[k]), groups[k]) for k in scenes]
        if neutral:
            ordered.append((self.scene_for(None), neutral))

        for scene, items in ordered:
            for n, (i, mode, include) in enumerate(items):
                ex = self._exercise(i, mode, "scene", include, scene, now)
                if n == 0:
                    ex.new_scene = scene
                out.append(ex)

        # Something new: a mission, then each new item's first encounter.
        if new.mission:
            f = self.cur.functions[new.mission]
            out.append(Exercise(
                part="something new", mode="mission", instruction=INSTRUCTIONS["mission"],
                target=self.describe(f.id), function=f.id, include=[], easy_words=[],
                new_scene=self.scene_for(f),
            ))
        # First encounters, three items at a time: all three repeat, then hear, then cued,
        # so each item comes back after a short gap.
        items = new.new_items
        for start in range(0, len(items), 3):
            group = items[start:start + 3]
            for step, mode in enumerate(FIRST_ENCOUNTER, 1):
                for i in group:
                    ex = self._exercise(i, mode, "something new", [], self.scene_for(self.cur.function_of(i)), now)
                    ex.first_encounter = f"step {step} of 3"
                    if mode != "repeat":
                        ex.instruction = f"First encounter, step {step} of 3. " + INSTRUCTIONS[mode]
                    out.append(ex)
        return out

    def _exercise(self, item_id: str, mode: Mode, part: Part, include: list[str], scene: Scene,
                  now: datetime) -> Exercise:
        item = self.cur.item(item_id)
        ex = Exercise(
            part=part,
            mode=mode,
            instruction=INSTRUCTIONS[mode],
            target=self.describe(item_id),
            include=[self.describe(c) for c in include],
            easy_words=self.easy_words(now, scene, {item_id, *include}),
            function=scene.function,
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

    def extra(self, now: datetime, exclude: set[str], limit: int, session_id: int) -> list[Exercise]:
        """Extra practice once the plan runs out: unlocked items not yet practised this session."""
        L = self.learner
        rng = random.Random(session_id * 7919 + len(exclude))
        pool = sorted((i for i in L.items if i not in exclude and self.cur.is_schedulable(i)),
                      key=lambda i: L.retrievability(i, now))[:limit]
        out = []
        for i in pool:
            ex = self._exercise(i, self.pick_mode(i, rng, []), "extra", [], self.scene_for(None), now)
            ex.extra = True
            out.append(ex)
        return out
