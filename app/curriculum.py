"""Loads the curriculum YAML files into memory and validates them.

The server refuses to start if a check fails, so a broken file can't take practice down:
Cloud Run keeps serving the previous revision.
"""

from __future__ import annotations

import glob
import os
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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


class Entry(BaseModel):
    """One entry in a curriculum file. Unknown fields and wrong types are refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ClassVar[str]
    prefix: ClassVar[str]

    id: str

    @field_validator("id")
    @classmethod
    def _prefixed(cls, v: str) -> str:
        if not v.startswith(cls.prefix):
            raise ValueError(f"a {cls.kind} id must start with {cls.prefix}")
        return v


class Grammar(Entry):
    kind: ClassVar[str] = "grammar"
    prefix: ClassVar[str] = "g."

    label: str
    order: int
    stage: int = Field(ge=0, le=3)
    name: str
    needs: list[str]
    how: str
    example: str
    example_en: str
    tags: list[str] = []


class Function(Entry):
    kind: ClassVar[str] = "function"
    prefix: ClassVar[str] = "f."

    label: str
    order: int
    name: str
    needs: list[str]
    phrases: list[str]
    core_words: list[str]
    tags: list[str]
    mission: str


class Phrase(Entry):
    kind: ClassVar[str] = "phrase"
    prefix: ClassVar[str] = "p."

    function: str
    order: int
    fa: str
    translit: str
    en: str
    tags: list[str]
    slot: str | None = None


class Word(Entry):
    kind: ClassVar[str] = "word"
    prefix: ClassVar[str] = "w."

    fa: str
    translit: str
    en: str
    pos: Literal[tuple(POS_NEEDS)]  # type: ignore[valid-type]
    rank: int
    tags: list[str]
    tier: int = 1  # set from the file name (words/tier1.yaml)
    forms: dict[str, str] = {}


def _by_id(entries: list[dict]) -> dict[str, dict]:
    """Key a file's entries by id, refusing duplicates. Error messages then name the id, not a list index."""
    out: dict[str, dict] = {}
    for e in entries:
        key = e.get("id") if isinstance(e, dict) else None
        if not isinstance(key, str):
            raise ValueError(f"an entry has no id: {e!r}")
        if key in out:
            raise ValueError(f"{key}: duplicate id")
        out[key] = e
    return out


class Curriculum(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    notes: str = ""
    topics: list[str]
    grammar: dict[str, Grammar]
    functions: dict[str, Function]
    phrases: dict[str, Phrase]
    words: dict[str, Word]

    @field_validator("grammar", "functions", "phrases", "words", mode="before")
    @classmethod
    def _key_by_id(cls, v):
        return _by_id(v) if isinstance(v, list) else v

    @field_validator("grammar", "functions", "phrases")
    @classmethod
    def _in_order(cls, v: dict):
        return dict(sorted(v.items(), key=lambda kv: kv[1].order))

    @field_validator("words")
    @classmethod
    def _in_rank_order(cls, v: dict):
        return dict(sorted(v.items(), key=lambda kv: (kv[1].tier, kv[1].rank)))

    @model_validator(mode="after")
    def _references(self):
        """Checks across files: ids unique across kinds, tags from the topic list, references that resolve."""
        problems: list[str] = []
        seen: set[str] = set()
        for table in (self.grammar, self.functions, self.phrases, self.words):
            for item in table.values():
                if item.id in seen:
                    problems.append(f"{item.id}: duplicate id")
                seen.add(item.id)
                problems += [
                    f"{item.id}: tag '{t}' is not in the topic list in manifest.yaml" for t in item.tags if t not in self.topics
                ]
        for g in self.grammar.values():
            problems += [f"{g.id}: needs unknown grammar pattern {n}" for n in g.needs if n not in self.grammar]
        for f in self.functions.values():
            problems += [f"{f.id}: needs unknown grammar pattern {n}" for n in f.needs if n not in self.grammar]
            problems += [f"{f.id}: unknown set phrase {p}" for p in f.phrases if p not in self.phrases]
            problems += [f"{f.id}: unknown core word {w}" for w in f.core_words if w not in self.words]
        for p in self.phrases.values():
            if p.function not in self.functions:
                problems.append(f"{p.id}: unknown function {p.function}")
            elif p.id not in self.functions[p.function].phrases:
                problems.append(f"{p.id}: not listed under {p.function}'s phrases")
        if problems:
            raise ValueError("\n  ".join(problems))
        return self

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


def load(directory: str) -> Curriculum:
    raw = dict(_read(os.path.join(directory, "manifest.yaml")))
    for name in ("grammar", "functions", "phrases"):
        raw[name] = _read(os.path.join(directory, f"{name}.yaml"))
    raw["words"] = []
    for path in sorted(glob.glob(os.path.join(directory, "words", "tier*.yaml"))):
        tier = int(os.path.basename(path)[4:].split(".")[0])
        raw["words"] += [{**w, "tier": tier} for w in _read(path)]
    if "version" in raw:
        raw["version"] = str(raw["version"])
    try:
        return Curriculum.model_validate(raw)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            lines.append(f"{where}: {msg}" if where else msg)
        raise CurriculumError("Curriculum is invalid:\n  " + "\n  ".join(lines)) from None
