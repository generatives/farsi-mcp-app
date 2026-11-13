from app.models import Missed
from app.practice import Practice
from tests.conftest import run_exercise


def play_session(p, clock, n=None, missed_for=None):
    r = p.start_session()
    queue = list(r.exercises)
    done = 0
    while queue and (n is None or done < n):
        ex = queue.pop(0)
        missed = missed_for(ex) if missed_for else None
        res = run_exercise(p, ex, clock, missed)
        done += 1
        if res.next and not res.next.extra:
            queue.append(res.next)
        if res.done:
            break
    return r, done


def test_day_one_introduces_greetings_and_stage_0_grammar(practice):
    r = practice.start_session()
    names = r.plan.something_new.new_items
    assert names[0].startswith("سلام")
    assert any(n.startswith("G1") for n in names)
    first = r.exercises[0]
    assert first.mode == "repeat" and first.first_encounter == "step 1 of 3"
    assert len(r.exercises) == 3


def test_first_encounter_unlocks_and_budget_ends_session(practice, clock):
    _, done = play_session(practice, clock)
    assert 15 <= done <= 25  # about 15 minutes at 40 s an attempt
    assert practice.learner.unlocked("p.salam")
    assert practice.learner.unlocked("g.to-be")
    assert not practice.learner.unlocked("w.madar")


def test_locked_ids_are_ignored(practice, clock):
    practice.start_session()
    res = practice.log_attempt("مادر", "say", words=["w.madar"])
    assert res.logged == []
    assert res.ignored[0].id == "w.madar"


def test_replay_rebuilds_the_same_state(practice, clock, db, cur):
    play_session(practice, clock)
    clock.advance(days=2)
    play_session(practice, clock, missed_for=lambda ex: [Missed(id=ex.target.id, outcome="helped")])
    again = Practice(cur, db, clock=clock)
    for i, st in practice.learner.items.items():
        other = again.learner.items[i]
        assert (st.level, st.attempts, st.lapses, st.card.due) == (other.level, other.attempts, other.lapses, other.card.due)


def test_levels_move_up_over_days_and_down_on_wrong(practice, clock):
    play_session(practice, clock)
    L = practice.learner
    assert L.level("p.salam") == 0
    # A later day: correct hear and cued move an item to "recognizes".
    clock.advance(days=1)
    practice.start_session()
    for mode in ("hear", "cued"):
        clock.advance(seconds=30)
        practice.log_attempt("سلام", mode, phrases=["p.salam"])
    assert L.level("p.salam") == 1
    clock.advance(seconds=30)
    practice.log_attempt("سلام", "say", phrases=["p.salam"], missed=[Missed(id="p.salam", outcome="wrong")])
    assert L.level("p.salam") == 0


def test_due_reviews_come_back_inside_scenes(practice, clock):
    play_session(practice, clock)
    clock.advance(days=3)
    r = practice.start_session()
    assert r.plan.scene_exercises > 0
    first_scene = next(ex for ex in r.exercises if ex.part == "scene")
    assert first_scene.new_scene is not None


def test_one_hard_thing_per_sentence(practice, clock):
    play_session(practice, clock)
    clock.advance(days=3)
    practice.start_session()
    for ex in practice.session.queue:
        if ex.part == "scene" and practice.learner.level(ex.target.id) <= 1:
            assert ex.include == []


def test_mission_marks_function_done(practice, clock, cur):
    L = practice.learner
    practice.start_session()
    f = cur.functions["f.thank"]
    res = practice.log_attempt("نه، ممنون… دستت درد نکنه", "mission", phrases=[], function=f.id)
    assert res.mission_passed and L.function_done(f.id)


def test_progress_reports_functions(practice, clock):
    play_session(practice, clock, n=5)
    prog = practice.get_progress()
    assert {f.function for f in prog.functions} >= {"Greet and say goodbye"}
    assert prog.by_state["introduced"]


def test_known_vocabulary_lists_met_words_and_phrases(practice, clock):
    assert practice.start_session().known_vocabulary == []
    play_session(practice, clock)
    clock.advance(days=2)
    vocab = practice.start_session().known_vocabulary
    ids = {v.id for v in vocab}
    assert 0 < len(vocab) <= 60
    assert "p.salam" in ids  # set phrases count as vocabulary
    assert all(practice.learner.unlocked(i) for i in ids)
    assert not any(i.startswith("g.") for i in ids)


def test_easy_words_include_set_phrases(practice, clock):
    play_session(practice, clock)
    clock.advance(days=2)
    practice.start_session()
    easy = {w.id for ex in practice.session.queue for w in ex.easy_words}
    assert any(i.startswith("p.") for i in easy)


def test_results_round_trip_through_their_schemas(practice, clock):
    """What the server sends (optional nulls left out) must parse back against the published schema."""
    from app.models import LogAttemptResult, NextItemsResult, ProgressResult, StartSessionResult

    real_log = practice.log_attempt

    def checked_log(*args, **kwargs):
        res = real_log(*args, **kwargs)
        LogAttemptResult.model_validate(res.model_dump())
        return res

    practice.log_attempt = checked_log
    wrong = lambda ex: [Missed(id=ex.target.id, outcome="wrong")]  # noqa: E731
    for day in range(12):
        r, _ = play_session(practice, clock, missed_for=wrong if day % 4 == 3 else None)
        StartSessionResult.model_validate(r.model_dump())
        NextItemsResult.model_validate(practice.next_items(5).model_dump())
        ProgressResult.model_validate(practice.get_progress().model_dump())
        clock.advance(days=1)
