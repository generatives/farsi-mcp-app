from app.models import Missed
from app.planner import LADDER
from app.practice import Practice
from tests.conftest import run_exercise


def play_session(p, clock, n=None, missed_for=None, watch=None):
    """Run a session as Claude would, crediting each exercise's target. Returns the exercises run."""
    r = p.start_session()
    ex, done = r.exercise, []
    while ex is not None and (n is None or len(done) < n):
        if watch:
            watch(p, ex)
        missed = missed_for(ex) if missed_for else None
        res = run_exercise(p, ex, clock, missed)
        done.append(ex)
        if res.done:
            break
        ex = res.next
    return r, done


def test_day_one_starts_with_a_new_greeting(practice):
    r = practice.start_session()
    assert r.new_today[0].startswith("سلام")
    assert 0 < len(r.new_today) <= 5
    assert sum(n.startswith("G") for n in r.new_today) == 1  # one starter grammar pattern a day
    ex = r.exercise
    assert ex.part == "new" and ex.mode == "repeat" and ex.new_step == "step 1 of 5"


def test_budget_ends_the_session(practice, clock):
    _, done = play_session(practice, clock)
    assert 15 <= len(done) <= 25  # about 15 minutes at 40 s an attempt
    assert practice.learner.unlocked("p.salam")
    assert practice.learner.unlocked("g.to-be")


def test_new_items_climb_their_ladder_many_times(practice, clock):
    _, done = play_session(practice, clock)
    salam = [ex.mode for ex in done if ex.target.id == "p.salam"]
    assert salam == [mode for mode, _ in LADDER]


def test_at_most_two_new_items_still_being_learned(practice, clock):
    seen = []
    play_session(practice, clock, watch=lambda p, ex: seen.append(p.session.still_learning()))
    assert max(seen) <= 2


def test_new_item_waits_its_gap_when_there_is_other_work(practice, clock):
    play_session(practice, clock)
    clock.advance(days=3)
    order = []
    play_session(practice, clock, watch=lambda p, ex: order.append(ex))
    new_ids = {ex.target.id for ex in order if ex.new_step == "step 1 of 5"}
    for item in new_ids:
        positions = [n for n, ex in enumerate(order) if ex.target.id == item]
        if len(positions) >= 2 and any(ex.part == "review" for ex in order):
            assert positions[1] - positions[0] >= 2


def test_a_miss_sends_a_new_item_back_two_steps(practice, clock):
    practice.start_session()
    s = practice.session
    s.learning.clear()
    from app.planner import Rung

    s.learning["p.salam"] = Rung(step=3, last=0)
    s.introduced.add("p.salam")
    practice.log_attempt("سلام", "say", phrases=["p.salam"], missed=[Missed(id="p.salam", outcome="wrong")])
    assert s.learning["p.salam"].step == 1


def test_skipping_a_new_item_puts_it_back_in_the_queue(practice):
    r = practice.start_session()
    first = r.exercise.target.id
    res = practice.skip_exercise()
    assert res.exercise.target.id != first
    assert practice.session.new_queue[-1] == first
    assert first not in practice.session.learning


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


def test_due_reviews_come_back_on_later_days(practice, clock):
    play_session(practice, clock)
    clock.advance(days=3)
    _, done = play_session(practice, clock)
    assert any(ex.part == "review" for ex in done)
    assert done[0].new_scene is not None


def test_one_hard_thing_per_sentence(practice, clock):
    for _ in range(4):
        _, done = play_session(practice, clock)
        for ex in done:
            if practice.learner.level(ex.target.id) <= 1:
                assert ex.include == []
        clock.advance(days=2)


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
    _, done = play_session(practice, clock)
    easy = {w.id for ex in done for w in ex.easy_words}
    assert any(i.startswith("p.") for i in easy)


def test_results_round_trip_through_their_schemas(practice, clock):
    """What the server sends (optional nulls left out) must parse back against the published schema."""
    from app.models import LogAttemptResult, ProgressResult, SkipResult, StartSessionResult

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
        SkipResult.model_validate(practice.skip_exercise().model_dump())
        ProgressResult.model_validate(practice.get_progress().model_dump())
        clock.advance(days=1)
