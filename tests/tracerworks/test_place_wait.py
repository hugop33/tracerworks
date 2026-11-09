"""A `start` refused for want of a place takes the next one (#2118): the spec
waits for a place, and `start-next` serves it before any new spec — after a
red to fix and a chantier set aside.

The world of `test_order`: worktrees #501, #510, #520, #540 and #550, only
the windows a test opens, the queue's #565 ready to start; #610 a spec with
no worktree yet, started by hand.
"""

from __future__ import annotations

from pathlib import Path

from tests.tracerworks.fakes import TMUX, FakeRunner
from tests.tracerworks.test_clean import RED, answer_state, pr_list
from tests.tracerworks.test_order import (
    SESSION,
    Shop,
    in_ci,
    in_order,
    limited,
    open_windows,
    shop,
    status_json,
    status_lines,
)
from tests.tracerworks.test_set_aside import DEV_SESSION, opened, window_of
from tests.tracerworks.test_set_aside import transcripts as transcripts  # a fixture
from tests.tracerworks.test_start_stop import issue_answer
from tracerworks import order, parking
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, Env, run

WAITS = "#610 waits for a place — it comes back before any new spec"


def refused_610(tmp_path: Path) -> Shop:
    """#501 in dev holds the one place: `start 610` is refused, #610 waits."""
    fake, env, out, err = shop(tmp_path, windows=[501])
    issue_answer(fake, 610, "Spec — Console")
    in_order(env, {501: 1})
    assert run(limited(env, 1), ["start", "610"]) == EXIT_REFUSED
    return fake, env, out, err


def free_place(fake: FakeRunner, env: Env) -> Env:
    """#501's window closed (stopped by hand, its entry gone): one place free."""
    open_windows(fake, [])
    order.leave(env.settings, 501)
    return limited(env, 1)


def test_a_start_refused_for_want_of_a_place_waits_for_one_said(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    _, env, _, err = refused_610(tmp_path)

    assert (
        "1 in flight, the limit is 1 — #610 waits for a place — it comes back"
        " before any new spec" in err.getvalue()
    ), "said once"
    assert list(order.place_waits(env.settings)) == [610]
    assert launched == []


def test_a_second_request_neither_doubles_it_nor_moves_it_back(
    tmp_path: Path,
) -> None:
    _, env, _, _ = refused_610(tmp_path)
    first = order.place_waits(env.settings)
    env.now = lambda: 1_900_000_000
    assert run(limited(env, 1), ["start", "610"]) == EXIT_REFUSED

    assert order.place_waits(env.settings) == first


def test_a_spec_held_back_by_the_sessions_only_waits_too(
    tmp_path: Path,
) -> None:
    """Under the limit, every session busy: a new spec would take the session
    first — the refusal comes from the same rule."""
    fake, env, _, err = shop(tmp_path, windows=[501, 510])
    issue_answer(fake, 610, "Spec — Console")

    assert run(env, ["start", "610"]) == EXIT_REFUSED
    assert "2/2 sessions busy" in err.getvalue() and WAITS in err.getvalue()
    assert list(order.place_waits(env.settings)) == [610]


def test_a_start_refused_for_another_reason_waits_for_nothing(
    tmp_path: Path,
) -> None:
    """The quota refuses it, not the order: no place to wait for."""
    fake, env, _, err = shop(tmp_path, windows=[501])
    issue_answer(fake, 610, "Spec — Console")
    env.settings.state_file.write_text("claude:\n  weekly_used: 95\n")

    assert run(env, ["start", "610"]) == EXIT_REFUSED
    assert "cap reached" in err.getvalue() and "waits for a place" not in err.getvalue()
    assert order.place_waits(env.settings) == {}


def test_the_next_free_place_goes_to_it_not_to_the_new_spec(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = refused_610(tmp_path)
    env = free_place(fake, env)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert [n for n, _, _ in launched] == [610]
    assert "#610 waited for a place since" in out.getvalue()
    assert order.place_waits(env.settings) == {}
    assert 610 in order.entries(env.settings)


def test_the_oldest_request_first(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = shop(tmp_path)
    issue_answer(fake, 610, "Spec — Console")
    issue_answer(fake, 600, "Spec — Autre")
    order.wait_for_place(env.settings, 610, 100)
    order.wait_for_place(env.settings, 600, 200)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert [n for n, _, _ in launched] == [610]
    assert list(order.place_waits(env.settings)) == [600]


def test_a_place_still_taken_keeps_it_waiting_and_starts_nothing(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    _, env, out, _err = refused_610(tmp_path)

    assert run(limited(env, 1), ["start-next"]) == EXIT_REFUSED
    assert "#610 waits for a place (1st) — 1 in flight, the limit is 1" in (
        out.getvalue()
    )
    assert launched == [] and list(order.place_waits(env.settings)) == [610]


def test_a_closed_spec_stops_waiting_and_the_queue_goes_on(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = refused_610(tmp_path)
    env = free_place(fake, env)
    issue_answer(fake, 610, "Spec — Console", state="CLOSED")

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert "#610 no longer waits for a place: #610 is closed" in out.getvalue()
    assert [n for n, _, _ in launched] == [565]
    assert order.place_waits(env.settings) == {}


def test_a_red_goes_before_it(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _out, err = shop(tmp_path)
    issue_answer(fake, 610, "Spec — Console")
    parking.park(env.settings, 520, SESSION, 720, 0)
    answer_state(fake, 720, RED)
    in_order(env, {520: 1})
    order.wait_for_place(env.settings, 610, 100)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert [window_of(a) for a in opened(fake)] == ["worker-520"]
    assert launched == [] and list(order.place_waits(env.settings)) == [610]


def test_a_chantier_set_aside_goes_before_it(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = shop(tmp_path)
    issue_answer(fake, 610, "Spec — Console")
    order.set_aside(env.settings, 501, DEV_SESSION, None, 50)
    in_order(env, {501: 1})
    order.wait_for_place(env.settings, 610, 100)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    [argv] = opened(fake)
    assert window_of(argv) == "worker-501" and f"--resume {DEV_SESSION}" in argv[-1]
    assert launched == [] and list(order.place_waits(env.settings)) == [610]


def test_start_by_hand_stop_and_clean_end_the_wait(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = shop(tmp_path)
    issue_answer(fake, 610, "Spec — Console")
    in_ci(fake, env, {540: 740})
    for spec in (610, 510, 540):
        order.wait_for_place(env.settings, spec, 100)

    assert run(env, ["start", "610"]) == EXIT_OK, err.getvalue()
    open_windows(fake, [510])
    assert run(env, ["stop", "510", "--force"]) == EXIT_OK, err.getvalue()
    assert run(env, ["clean"]) == EXIT_OK, err.getvalue()
    assert order.place_waits(env.settings) == {}


def test_status_says_who_waits_for_a_place_its_rank_and_since_when(
    tmp_path: Path,
) -> None:
    _, env, out, _ = refused_610(tmp_path)
    order.wait_for_place(env.settings, 600, 1_790_413_000)

    lines = status_lines(env, out)
    assert any(x.startswith("waits for a place: 1st #600 since ") for x in lines)
    assert any(x.startswith("waits for a place: 2nd #610 since ") for x in lines)


def test_status_json_ranks_the_waits_oldest_first(
    tmp_path: Path,
) -> None:
    _, env, out, _ = shop(tmp_path)
    order.wait_for_place(env.settings, 600, 200)
    order.wait_for_place(env.settings, 610, 100)

    report = status_json(env, out)
    assert report["place_waits"] == [
        {"spec": 610, "rank": 1, "since": 100},
        {"spec": 600, "rank": 2, "since": 200},
    ]


# --- over the limit, by hand (#2120) -------------------------------------------------


def test_over_limit_starts_above_the_limit_in_flight_once(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """30/10: #1958 had to be launched with a copy of `tracerworks.toml`."""
    _, env, out, err = refused_610(tmp_path)

    assert run(limited(env, 1), ["start", "610", "--over-limit"]) == EXIT_OK, (
        err.getvalue()
    )
    assert [n for n, _, _ in launched] == [610]
    assert order.place_waits(env.settings) == {}
    assert "#610 entered the factory" in out.getvalue()


def test_over_limit_lifts_neither_the_sessions_nor_the_quota(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = shop(tmp_path, windows=[501, 510])
    issue_answer(fake, 610, "Spec — Console")

    assert run(limited(env, 1), ["start", "610", "--over-limit"]) == EXIT_REFUSED
    assert "2/2 sessions busy" in err.getvalue()
    assert "in flight, the limit" not in err.getvalue()

    open_windows(fake, [501])
    env.settings.state_file.write_text("claude:\n  weekly_used: 95\n")
    assert run(limited(env, 1), ["start", "610", "--over-limit"]) == EXIT_REFUSED
    assert "cap reached" in err.getvalue() and launched == []


def test_start_next_never_goes_over_the_limit_even_for_a_wait(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    _, env, _, err = refused_610(tmp_path)

    assert run(limited(env, 1), ["start-next"]) == EXIT_REFUSED
    assert "1 in flight, the limit is 1" in err.getvalue() and launched == []


# --- stop says when set-aside was the gesture (#2120) --------------------------------

SET_ASIDE_HINT = "to come back before any new spec, `tracerworks set-aside {n}`"


def no_merged_pull(fake: FakeRunner) -> None:
    fake.answer(
        "gh", "pr", "list", "--repo", "owner/repo", "--head", prefix=True,
        stdout="[]",
    )  # fmt: skip


def test_stop_of_a_chantier_with_work_left_proposes_set_aside(tmp_path: Path) -> None:
    """#550: its worktree kept, no merged pull request."""
    fake, env, out, err = shop(tmp_path)
    no_merged_pull(fake)

    assert run(env, ["stop", "550"]) == EXIT_OK, err.getvalue()
    assert out.getvalue().splitlines()[-1] == SET_ASIDE_HINT.format(n=550)


def test_stop_of_a_window_with_work_left_ends_on_the_hint(tmp_path: Path) -> None:
    fake, env, out, err = shop(tmp_path, windows=[510])
    no_merged_pull(fake)

    assert run(env, ["stop", "510", "--force"]) == EXIT_OK, err.getvalue()
    assert out.getvalue().splitlines()[-1] == SET_ASIDE_HINT.format(n=510)


def test_stop_of_a_delivered_chantier_proposes_nothing(tmp_path: Path) -> None:
    """#540: its pull request merged."""
    fake, env, out, err = shop(tmp_path)
    fake.answer(*pr_list("agent/540-fini"), stdout='[{"number": 640}]')

    assert run(env, ["stop", "540"]) == EXIT_OK, err.getvalue()
    assert "set-aside" not in out.getvalue()


def test_the_hint_works_a_stopped_chantier_set_aside_comes_back_first(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Stopped by hand, then set aside after all: no window to close, its
    session kept — start-next resumes it before the queue's #565."""
    fake, env, out, err = shop(tmp_path, windows=[501])
    no_merged_pull(fake)
    in_order(env, {501: 1})

    assert run(env, ["stop", "501", "--force"]) == EXIT_OK, err.getvalue()
    open_windows(fake, [])
    assert run(env, ["set-aside", "501"]) == EXIT_OK, err.getvalue()
    assert "#501 set aside (session 11111111) by Sam" in out.getvalue()
    assert len(fake.ran(*TMUX, "kill-window")) == 1, "the stop's only"
    assert 501 in order.entries(env.settings)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    [argv] = [a for a in opened(fake) if window_of(a) == "worker-501"]
    assert f"--resume {DEV_SESSION}" in argv[-1]
    assert launched == []


def test_a_stopped_chantier_without_a_session_is_not_set_aside(tmp_path: Path) -> None:
    _, env, _, err = shop(tmp_path)

    assert run(env, ["set-aside", "550"]) == EXIT_REFUSED
    assert "#550 NOT set aside: no Claude session found for its worktree" in (
        err.getvalue()
    )
    assert order.set_aside_records(env.settings) == {}


def test_stop_cancels_the_wait_of_a_spec_that_never_started(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Review of #2146: #610 has no worker yet — `stop` still takes it out,
    and the next free place goes to the queue."""
    _, env, out, err = refused_610(tmp_path)

    assert run(env, ["stop", "610"]) == EXIT_OK, err.getvalue()
    assert "#610 no longer waits for a place: stopped by Sam" in out.getvalue()
    assert order.place_waits(env.settings) == {}
    assert run(env, ["stop", "610"]) == EXIT_REFUSED, "nothing left to stop"
