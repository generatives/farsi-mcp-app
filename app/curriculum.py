"""Loads the curriculum YAML files into memory and validates them.

The server refuses to start if a check fails, so a broken file can't take practice down:
Cloud Run keeps serving the previous revision.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

import yaml

PREFIXES = {"w.": "word", "p.": "phrase", "g.": "grammar", "f.": "function"}

# Which grammar pattern (by curriculum label) a part of speech needs before a word can appear.
POS_NEEDS = {
    "noun": "G1",
    "adjective": "G1",
    "adverb": "G1",
    "pronoun": "G1",
    "verb": "G6",
    "two-part verb": "G8",
    "number": "G22",
}


class CurriculumError(Exception):
    pass


@dataclass
class Grammar:
    id: str
    label: str
    order: int
    stage: int
    name: str
    needs: list[str]
    how: str
    example: str
    example_en: str
    kind: str = "grammar"
    tags: list[str] = field(default_factory=list)


@dataclass
class Function:
    id: str
    label: str
    order: int
    name: str
    needs: list[str]
    phrases: list[str]
    core_words: list[str]
    tags: list[str]
    mission: str
    kind: str = "function"


@dataclass
class Phrase:
    id: str
    function: str
    order: int
    fa: str
    translit: str
    en: str
    tags: list[str]
    slot: str | None = None
    kind: str = "phrase"


@dataclass
class Word:
    id: str
    fa: str
    translit: str
    en: str
    pos: str
    rank: int
    tags: list[str]
    tier: int = 1
    forms: dict = field(default_factory=dict)
    kind: str = "word"


@dataclass
class Curriculum:
    version: str
    topics: list[str]
    grammar: dict[str, Grammar]
    functions: dict[str, Function]
    phrases: dict[str, Phrase]
    words: dict[str, Word]

    def item(self, item_id: str):
        for table in (self.words, self.phrases, self.grammar, self.functions):
            if item_id in table:
                return table[item_id]
        return None

    def is_schedulable(self, item_id: str) -> bool:
        return item_id in self.words or item_id in self.phrases or item_id in self.grammar

    def grammar_by_label(self, label: str) -> Grammar | None:
        return next((g for g in self.grammar.values() if g.label == label), None)

    def function_of(self, item_id: str) -> Function | None:
        """The function an item belongs to directly, as one of its set phrases or core words."""
        for f in self.functions.values():
            if item_id in f.phrases or item_id in f.core_words:
                return f
        return None


def _read(path: str) -> list | dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def _build(cls, raw: dict, path: str, **extra):
    names = set(cls.__dataclass_fields__) - {"kind"}
    unknown = set(raw) - names
    if unknown:
        raise CurriculumError(f"{path}: {raw.get('id')}: unknown fields {sorted(unknown)}")
    try:
        return cls(**raw, **extra)
    except TypeError as exc:
        raise CurriculumError(f"{path}: {raw.get('id')}: {exc}") from None


def load(directory: str) -> Curriculum:
    manifest = _read(os.path.join(directory, "manifest.yaml"))
    grammar = [_build(Grammar, r, "grammar.yaml") for r in _read(os.path.join(directory, "grammar.yaml"))]
    functions = [_build(Function, r, "functions.yaml") for r in _read(os.path.join(directory, "functions.yaml"))]
    phrases = [_build(Phrase, r, "phrases.yaml") for r in _read(os.path.join(directory, "phrases.yaml"))]
    words = []
    for path in sorted(glob.glob(os.path.join(directory, "words", "tier*.yaml"))):
        tier = int(os.path.basename(path)[4:].split(".")[0])
        words += [_build(Word, r, os.path.basename(path), tier=tier) for r in _read(path)]

    cur = Curriculum(
        version=str(manifest.get("version", "?")),
        topics=list(manifest.get("topics", [])),
        grammar={g.id: g for g in sorted(grammar, key=lambda g: g.order)},
        functions={f.id: f for f in sorted(functions, key=lambda f: f.order)},
        phrases={p.id: p for p in sorted(phrases, key=lambda p: p.order)},
        words={w.id: w for w in sorted(words, key=lambda w: (w.tier, w.rank))},
    )
    validate(cur, [*grammar, *functions, *phrases, *words])
    return cur


def validate(cur: Curriculum, all_items: list) -> None:
    problems: list[str] = []
    seen: set[str] = set()
    for item in all_items:
        prefix = item.id[:2]
        if PREFIXES.get(prefix) != item.kind:
            problems.append(f"{item.id}: a {item.kind} id must start with " + next(k for k, v in PREFIXES.items() if v == item.kind))
        if item.id in seen:
            problems.append(f"{item.id}: duplicate id")
        seen.add(item.id)
        for tag in getattr(item, "tags", []) or []:
            if tag not in cur.topics:
                problems.append(f"{item.id}: tag '{tag}' is not in the topic list in manifest.yaml")

    for g in cur.grammar.values():
        problems += [f"{g.id}: needs unknown grammar pattern {n}" for n in g.needs if n not in cur.grammar]
    for f in cur.functions.values():
        problems += [f"{f.id}: needs unknown grammar pattern {n}" for n in f.needs if n not in cur.grammar]
        problems += [f"{f.id}: unknown set phrase {p}" for p in f.phrases if p not in cur.phrases]
        problems += [f"{f.id}: unknown core word {w}" for w in f.core_words if w not in cur.words]
    for p in cur.phrases.values():
        if p.function not in cur.functions:
            problems.append(f"{p.id}: unknown function {p.function}")
        elif p.id not in cur.functions[p.function].phrases:
            problems.append(f"{p.id}: not listed under {p.function}'s phrases")
    for w in cur.words.values():
        if w.pos not in POS_NEEDS:
            problems.append(f"{w.id}: unknown part of speech '{w.pos}'")

    if problems:
        raise CurriculumError("Curriculum is invalid:\n  " + "\n  ".join(problems))
