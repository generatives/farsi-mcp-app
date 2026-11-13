"""Typed shapes of everything the tools return.

The MCP SDK publishes these as each tool's outputSchema and validates every response against
them. extra="forbid" means a field the planner adds by mistake fails loudly instead of
reaching Claude.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

Mode = Literal["repeat", "hear", "cued", "say", "situation", "respond", "mission"]
Outcome = Literal["correct", "helped", "wrong"]
Part = Literal["warm-up", "scene", "something new", "extra"]
State = Literal["introduced", "recognizes", "produces with help", "produces freely"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_serializer(mode="wrap")
    def _drop_unset_optionals(self, handler):
        """Leave out optional fields that are None, so Claude isn't handed a wall of nulls."""
        data = handler(self)
        fields = type(self).model_fields
        return {k: v for k, v in data.items() if v is not None or fields[k].is_required()}


class Item(Strict):
    """A word, set phrase, grammar pattern or function, described in plain language."""

    id: str = Field(description="Label only: hand it back in log_attempt.")
    kind: Literal["word", "phrase", "grammar", "function"]
    state: State | None = Field(None, description="How well the learner knows it. Absent for new items.")
    # words and set phrases
    fa: str | None = None
    en: str | None = None
    part_of_speech: str | None = None
    forms: dict[str, str] | None = None
    slot: str | None = None
    # grammar patterns and functions
    name: str | None = None
    how: str | None = None
    example: str | None = None
    example_en: str | None = None
    mission: str | None = None
    phrases: list[str] | None = None


class Missed(Strict):
    """An item that caused a problem in an attempt (log_attempt input)."""

    id: str
    outcome: Literal["helped", "wrong"]


class VocabEntry(Strict):
    id: str
    fa: str
    en: str


class Scene(Strict):
    function: str | None = Field(description="The function the scene is built on; null for the neutral scene.")
    function_name: str | None = None
    theme: str
    setting: str


class Exercise(Strict):
    exercise: int | None = Field(None, description="Position in today's plan.")
    part: Part
    mode: Mode
    instruction: str
    target: Item
    include: list[Item] = Field(description="Other due items Claude may work in, most at risk first.")
    include_note: str | None = None
    easy_words: list[VocabEntry] = Field(description="Well-known words and set phrases that fit the scene.")
    function: str | None = None
    carry_with: str | None = None
    note: str | None = None
    new_scene: Scene | None = Field(None, description="Present when this exercise starts a new scene: bridge to it.")
    first_encounter: str | None = None
    extra: bool | None = None


class Summary(Strict):
    curriculum_version: str
    stage: int
    items_by_state: dict[str, int]
    due_today: int
    functions_done: list[str]
    attempts_so_far: int


class SomethingNew(Strict):
    gate_open: bool
    mission: str | None
    new_items: list[str]


class Plan(Strict):
    warm_up: int
    scene_exercises: int
    scenes: list[Scene]
    something_new: SomethingNew


class StartSessionResult(Strict):
    session_id: int
    summary: Summary
    seconds_left: int
    plan: Plan
    known_grammar: list[str]
    known_vocabulary: list[VocabEntry] = Field(description="The learner's best-known words and set phrases.")
    grammar_in_flight: list[str]
    exercises: list[Exercise]
    note: str


class NextItemsResult(Strict):
    exercises: list[Exercise]
    seconds_left: int


class Logged(Strict):
    id: str
    outcome: Outcome


class MovedUp(Strict):
    item: str
    now: State


class Ignored(Strict):
    id: str
    why: str


class LogAttemptResult(Strict):
    logged: list[Logged]
    moved_up: list[MovedUp]
    seconds_left: int
    mission_passed: bool | None = None
    ignored: list[Ignored] | None = None
    next: Exercise | None = None
    done: str | None = Field(None, description="Present when the session should wrap up.")
    note: str | None = None


class NotYet(Strict):
    count: int
    next_few: list[str]


class FunctionProgress(Strict):
    function: str
    status: Literal["done", "open", "not yet"]
    mission_ready: bool


class ProgressResult(Strict):
    by_state: dict[str, list[str]]
    shaky: list[str]
    not_yet: NotYet
    functions: list[FunctionProgress]
    stage: int
    accuracy_last_50: float | None
    forecast_seconds_per_day: int
    new_material_allowed: bool
