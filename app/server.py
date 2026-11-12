"""Farsi practice MCP server.

The MCP endpoint is served at /mcp/<MCP_SECRET>. The secret path is the only auth,
so keep the full URL private (it lives in your claude.ai connector settings).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Literal, TypedDict

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app import curriculum, settings
from app.db import Database
from app.practice import Practice

log = logging.getLogger("farsi")

INSTRUCTIONS = """\
Tools for practising spoken colloquial Persian (Farsi) by voice. The server decides what to practise \
and when; you turn its plan into natural spoken exchanges and grade what the learner says.

- Call start_session first in every practice conversation, then run its exercises in order, one at a time, \
and call log_attempt after each. Each log_attempt returns the next exercise.
- Always speak and write Persian in Persian script, because voice mode pronounces script correctly and \
mangles transliteration. Use the translit field only when the learner asks how something is spelled or asks \
for transliteration.
- Use colloquial Tehrani forms (miram, not miravam).
- Build sentences from the exercise's target, its include list and its easy_words. Avoid other Persian words \
the learner hasn't met; if you need one, give its meaning. Use only the learner's known grammar, plus the \
target.
- One hard thing per sentence: the target is what's being tested. Keep everything else easy.
- Never decide that something new is unlocked; only the server does that.
- Never name a grammar pattern ("ezâfe", "subjunctive") to the learner unless they ask.
- Ask the due exercises inside the scenes the server gives, not as a list of prompts. When an exercise \
carries new_scene, bridge to it in a sentence ("Later that evening, at your aunt's…").
- Session shape: a short warm-up, the scenes, then something new. Keep your own talk brief; the learner \
should be speaking most of the time.
- In the very first session, start with a minute on the sounds English lacks (kh, q, the long â).
"""

RUBRIC = """\
Log one attempt after every exercise. This is how the server learns what the learner knows, so grade the \
same way every time. You only ever see a speech-to-text transcript of the learner's Persian.

Modes. Use the exercise's mode. For repeat, grade whether they repeated it intelligibly. For hear, grade the \
meaning they gave. For cued, say and situation, grade the Persian they produced. For respond, grade both \
halves: an item in your line goes in missed if they misunderstood it, and an item in their reply goes in \
missed if they said it wrong. In situation and respond, if the learner answers well without the target, say \
so and nudge once toward it by meaning ("now say it more politely"); a target used after the nudge is \
helped, and one still missing after you model it is wrong. Grade a mission the same way as respond, across \
the whole role-play, and log it as one attempt with mode "mission" and the function id.

Outcomes.
- correct: the meaning is right and the target is used correctly, with no hint. Ignore spelling or \
word-boundary differences that come from the transcript.
- helped: the learner got there after a hint, a repeat or a partial model, or the answer was close but with \
one small slip. Also use helped when you can't tell whether the transcript or the learner caused an error, \
and write "transcript unclear" in error_note.
- wrong: the learner didn't understand or couldn't produce it, or used a different grammar pattern from the \
target (for example present instead of past).

Credit. List the ids of every word, set phrase and grammar pattern the sentence actually exercised, from the \
exercise's target, include and easy_words. Put each item that \
caused a problem in missed, with its own outcome, helped or wrong. Every listed item not in missed is credited \
correct. Describe any error in error_note in a few words, such as "used present instead of past". The missed \
list matters most: without it, one wrong ending would mark every word in the sentence as failed.
"""


class Missed(TypedDict):
    id: str
    outcome: Literal["helped", "wrong"]


SECRET = os.environ.get("MCP_SECRET", "")

mcp = MCPServer(name="farsi-practice", version="0.2.0", instructions=INSTRUCTIONS)
_practice: Practice | None = None
_lock = threading.Lock()


def practice() -> Practice:
    if _practice is None:
        raise RuntimeError("server not initialised")
    return _practice


@mcp.tool(
    description=(
        "Start a practice session. Call this first in every practice conversation. Returns a short summary, "
        "today's plan (warm-up, scenes, something new), the learner's known grammar, and the first three "
        "exercises to run in order."
    )
)
def start_session() -> dict:
    with _lock:
        return practice().start_session()


@mcp.tool(description="Rarely needed: more planned exercises without logging an attempt, for example after a skipped exercise or for extra practice once the plan is done.")
def next_items(limit: int = 3) -> dict:
    with _lock:
        return practice().next_items(max(1, min(limit, 10)))


@mcp.tool(description=RUBRIC)
def log_attempt(
    sentence_fa: str,
    mode: Literal["repeat", "hear", "cued", "say", "situation", "respond", "mission"],
    words: list[str] | None = None,
    phrases: list[str] | None = None,
    grammar: list[str] | None = None,
    missed: list[Missed] | None = None,
    function: str | None = None,
    error_note: str | None = None,
) -> dict:
    """sentence_fa is the Persian sentence (or, for a mission, the key line) in Persian script."""
    with _lock:
        return practice().log_attempt(
            sentence_fa, mode, words, phrases, grammar, [dict(m) for m in missed or []], function, error_note
        )


@mcp.tool(description="How the learner is doing: items by state, shaky items, functions open and done, accuracy. Call it when the learner asks about their progress.")
def get_progress() -> dict:
    with _lock:
        return practice().get_progress()


def build_app():
    global _practice
    if len(SECRET) < 32:
        sys.exit("MCP_SECRET must be set to at least 32 random characters (try: openssl rand -hex 24)")
    try:
        cur = curriculum.load(settings.CURRICULUM_DIR)
    except curriculum.CurriculumError as exc:
        sys.exit(str(exc))
    db = Database(settings.DB_PATH)
    _practice = Practice(cur, db)
    log.warning("curriculum %s loaded; replayed %d attempts", cur.version, len(_practice.learner.attempts))
    return mcp.streamable_http_app(
        streamable_http_path=f"/mcp/{SECRET}",
        # Stateless JSON responses: no session affinity needed, which suits Cloud Run.
        stateless_http=True,
        json_response=True,
        # Cloud Run fronts the container with its own hostname; the secret path is the guard.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
