"""The factory's order (#1823): a rank for every chantier, at most `in_flight`
in the factory, `max_workers` sessions for the best ranked; it replaced the
lanes (#1261).

The world of `test_clean`: worktrees #501, #510, #520, #540 and #550, only
the windows a test opens, the queue's #565 ready to start. A chantier is put
in CI by parking it (a `parked` row, its PR green: nothing to resume), and in
the order by `order.enter` — what `start` and `start-next` write.
"""

from __future__ import annotations

import dataclasses
import io
import json
from collections.abc import Collection, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import TMUX, FakeRunner, fixture
from tests.tracerworks.test_clean import GREEN, answer_state, clean_world
from tests.tracerworks.test_start_next import FACTORY, gh_lists
from tests.tracerworks.test_start_stop import (
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
)
from tracerworks import order, parking, store
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, Env, _preemptible, run
from tracerworks.observe import Worker, WorkerState
from tracerworks.order import Factory, Role
from tracerworks.runtime.tmux import WINDOW_FORMAT, Window

LIST_WINDOWS = (*TMUX, "list-windows", "-t", "=widgets", "-F", WINDOW_FORMAT)
SESSION = "0f1e2d3c-4b5a-4968-8776-655443322110"

Shop = tuple[FakeRunner, Env, io.StringIO, io.StringIO]


def shop(tmp_path: Path, windows: Collection[int] = ()) -> Shop:
    fake = clean_world(tmp_path)
    gh_lists(fake, FACTORY)
    issue_answer(fake, 565, "Spec — Factory V1 : tracerworks")
    git_repo_answers(fake)
    fake.answer(*TMUX, "new-window", prefix=True)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    open_windows(fake, windows)
    return fake, env, out, err


def open_windows(fake: FakeRunner, specs: Collection[int]) -> None:
    """Only these workers' windows (#501 working, #510 at rest, #530 dead)."""
    kept = [
        line
        for line in fixture("list-windows.txt").splitlines()
        if not line.startswith("worker-")
        or int(line.split("\t")[0].removeprefix("worker-")) in specs
    ]
    fake.answer(*LIST_WINDOWS, stdout="\n".join(kept))


def in_ci(fake: FakeRunner, env: Env, pulls: Mapping[int, int]) -> None:
    """Parked, their CI green: nothing to resume."""
    for spec, pull in pulls.items():
        parking.park(env.settings, spec, SESSION, pull, 0)
        answer_state(fake, pull, GREEN)


def in_order(env: Env, entered: Mapping[int, int]) -> None:
    """Each chantier's entry into the factory: the lower, the better ranked."""
    for spec, at in entered.items():
        order.enter(env.settings, spec, at)


def in_state_since(env: Env, spec: int, at: int, phase: str = "parked") -> None:
    """The chantier's state, as `serve` recorded it (#1729), since `at`."""
    with closing(store.connect(env.settings.history_db)) as db:
        db.execute(
            "INSERT INTO chantier_state (project, spec, at, phase, cause)"
            " VALUES (?, ?, ?, ?, 'test')",
            (env.settings.github_repo, spec, at, phase),
        )


def limited(env: Env, in_flight: int) -> Env:
    return dataclasses.replace(
        env, settings=dataclasses.replace(env.settings, in_flight=in_flight)
    )


def said(out: io.StringIO, err: io.StringIO) -> str:
    return out.getvalue() + err.getvalue()


# --- the order, its limit, its victims: the rules alone -----------------------------


def factory(
    roles: Mapping[int, Role],
    entered: Mapping[int, int],
    limit: int = 6,
    sessions: int = 2,
) -> Factory:
    return Factory(roles, entered, {}, limit, sessions)


DEV, CI, ASIDE = Role.DEVELOPMENT, Role.CI, Role.SET_ASIDE


def test_the_order_is_the_entry_into_the_factory_the_oldest_first() -> None:
    f = factory({501: DEV, 510: CI, 520: ASIDE}, {510: 5, 501: 9, 520: 1})
    assert f.ranked == [520, 510, 501]
    assert [f.rank(s) for s in (520, 510, 501, 999)] == [1, 2, 3, None]
    assert factory({7: DEV, 3: DEV}, {7: 1, 3: 1}).ranked == [3, 7], "a tie: by spec"


def test_the_limit_in_flight_counts_every_role_and_says_why() -> None:
    five = dict.fromkeys(range(1, 6), CI)
    assert factory(five, {}, sessions=2).why_full() is None
    six = {**five, 6: CI}
    assert factory(six, {}).why_full() == (
        "6 in flight, the limit is 6 — waits for a place"
    )
    assert factory({**five, 6: ASIDE}, {}).why_full() is not None, (
        "set aside: in flight"
    )


def test_the_sessions_are_the_windows_and_say_who_holds_them() -> None:
    f = factory({501: DEV, 510: DEV, 520: CI}, {501: 2, 510: 1, 520: 3}, sessions=2)
    assert f.sessions_free == 0
    assert f.why_full() == "2/2 sessions busy: #510 (1st) · #501 (2nd)"
    assert factory({501: DEV, 520: CI}, {}, sessions=2).sessions_free == 1


def test_a_red_preempts_the_most_recent_in_dev_ranked_after_it() -> None:
    f = factory({1: DEV, 2: CI, 3: DEV, 4: DEV}, {1: 1, 2: 2, 3: 3, 4: 4})
    assert f.victim(2, lambda _: True) == 4
    assert f.victim(2, lambda s: s != 4) == 3, "not at a safe point: the next one"
    assert f.victim(2, lambda s: s not in (3, 4)) is None, "never one ranked before"
    assert f.victim(4, lambda _: True) is None, "the last ranked preempts nobody"


def test_the_ranks_read_in_english() -> None:
    assert [order.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd",
        "101st", "111th",
    ]  # fmt: skip


def worker(state: WorkerState, window: bool = True) -> Worker:
    opened = Window("worker-501", "claude", 0, "claude") if window else None
    return Worker(501, state, "claude", opened, None, None, None)


NOW = 1_790_000_000


@pytest.mark.parametrize(
    ("state", "in_dev_for", "red_for", "safe"),
    [
        (WorkerState.WAITING, 3600, 0, True),  # at rest: a safe point
        (WorkerState.ASKING, 3600, 0, True),
        (WorkerState.WORKING, 3600, 9 * 60, False),  # its step not over
        (WorkerState.WORKING, 3600, 10 * 60, True),  # the red waited 10 min: at most
        (WorkerState.WORKING, 3600, None, False),  # the red's wait unknown
        (WorkerState.WAITING, 19 * 60, 3600, False),  # resumed < 20 min: kept
        (WorkerState.WAITING, 20 * 60, 3600, True),
        (WorkerState.DEAD, 3600, 3600, False),  # someone has to look at it
    ],
)
def test_a_session_closes_for_a_red_only_at_a_safe_point(
    state: WorkerState, in_dev_for: int, red_for: int | None, safe: bool
) -> None:
    red_since = None if red_for is None else NOW - red_for
    assert _preemptible(worker(state), NOW - in_dev_for, red_since, NOW) is safe
    assert not _preemptible(worker(state, window=False), None, NOW - 3600, NOW)
    assert not _preemptible(None, None, NOW - 3600, NOW)


# --- the order in a pass ------------------------------------------------------------


def test_the_queue_starts_nothing_at_the_limit_in_flight(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Two windows of three, but 3 chantiers in flight for a limit of 3."""
    fake, env, _, err = shop(tmp_path, windows=[501])
    env.settings.state_file.write_text("max_workers: 3\n")
    env = limited(env, 3)
    in_ci(fake, env, {520: 720, 540: 740})

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "3 in flight, the limit is 3 — waits for a place" in err.getvalue()
    assert launched == [] and not fake.ran("gh", "issue", "list")


def test_under_the_limit_a_free_session_takes_the_queue(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = shop(tmp_path, windows=[501])
    in_ci(fake, env, {520: 720})
    in_order(env, {501: 10, 520: 20})

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert [n for n, _, _ in launched] == [565]
    assert "#565 entered the factory" in out.getvalue()
    entered = order.entries(env.settings)
    assert set(entered) == {501, 520, 565} and entered[565] > entered[520]


def test_every_session_busy_starts_nothing(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    _, env, _, err = shop(tmp_path, windows=[501, 510])
    in_order(env, {510: 1, 501: 2})

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "2/2 sessions busy: #510 (1st) · #501 (2nd)" in err.getvalue()
    assert launched == []


def test_chantiers_found_without_an_entry_enter_once(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Launched before the order: entered now, nothing stopped, once."""
    fake, env, out, _ = shop(tmp_path, windows=[501, 510])
    in_ci(fake, env, {520: 720})

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert [x for x in out.getvalue().splitlines() if "entered" in x] == [
        "#501 entered the factory (it had no entry)",
        "#510 entered the factory (it had no entry)",
        "#520 entered the factory (it had no entry)",
    ]
    assert not fake.ran(*TMUX, "kill-window")
    again = io.StringIO()
    env.out = again
    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "entered" not in again.getvalue()


def test_a_relaunched_chantier_keeps_its_place(tmp_path: Path) -> None:
    _, env, _, _ = shop(tmp_path)
    order.enter(env.settings, 501, 10)
    order.enter(env.settings, 501, 99)
    assert order.entries(env.settings) == {501: 10}


# --- status, stop, clean -------------------------------------------------------------


def status_lines(env: Env, out: io.StringIO) -> list[str]:
    out.truncate(0)
    out.seek(0)
    assert run(env, ["status"]) == EXIT_OK
    return out.getvalue().splitlines()


def line_of(lines: list[str], spec: int) -> str:
    [line] = [x for x in lines if x.startswith(f"#{spec}")]
    return line


def status_json(env: Env, out: io.StringIO) -> dict[str, Any]:
    out.truncate(0)
    out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    report: dict[str, Any] = json.loads(out.getvalue())
    return report


def test_status_says_each_chantiers_rank_and_the_order(tmp_path: Path) -> None:
    fake, env, out, _ = shop(tmp_path, windows=[501, 510])
    in_ci(fake, env, {520: 720})
    in_order(env, {520: 1, 501: 2, 510: 3})

    lines = status_lines(env, out)
    assert line_of(lines, 520).endswith(" — 1st in the order")
    assert line_of(lines, 510).endswith(" — 3rd in the order")
    assert not line_of(lines, 540).endswith("in the order")  # stopped: not in flight
    assert "1st #520 light check (PR #720, its session closed)" in lines
    assert order.entries(env.settings) == {520: 1, 501: 2, 510: 3}  # read only

    report = status_json(env, out)
    by_spec = {w["spec"]: w for w in report["workers"]}
    assert (by_spec[501]["rank"], by_spec[501]["role"]) == (2, "development")
    assert (by_spec[540]["rank"], by_spec[540]["role"]) == (None, None)
    assert [(x["spec"], x["rank"]) for x in report["order"]] == [
        (520, 1),
        (501, 2),
        (510, 3),
    ]
    assert (report["in_flight"], report["in_flight_limit"]) == (3, 6)


def test_stop_and_clean_take_a_chantier_out_of_the_order(tmp_path: Path) -> None:
    fake, env, out, err = shop(tmp_path, windows=[510])
    in_ci(fake, env, {540: 740})
    in_order(env, {510: 1, 540: 2, 550: 3})

    assert run(env, ["stop", "510", "--force"]) == EXIT_OK, err.getvalue()
    assert "#510 left the factory's order" in out.getvalue()
    assert run(env, ["clean"]) == EXIT_OK, err.getvalue()
    assert "#540 left the factory's order" in out.getvalue()
    assert set(order.entries(env.settings)) == {550}


def test_stop_frees_a_chantier_once_parked_for_good(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#510 was parked, then resumed (its `parked` row stays): stopped, it
    must not come back « in CI » at the next pass — nor be resumed on a red:
    an explicit stop is explicit."""
    fake, env, out, err = shop(tmp_path, windows=[510])
    in_ci(fake, env, {510: 710})
    env.settings.state_file.write_text("max_workers: 1\n")
    in_order(env, {510: 1})

    assert run(env, ["stop", "510", "--force"]) == EXIT_OK, err.getvalue()
    open_windows(fake, [])
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert "#510 entered" not in out.getvalue()
    assert [n for n, _, _ in launched] == [565]
    assert parking.parked(env.settings) == {}


def test_start_by_hand_refuses_at_the_limit_saying_so(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = shop(tmp_path, windows=[501])
    issue_answer(fake, 600, "Console")

    assert run(env, ["start", "600"]) == EXIT_OK, err.getvalue()
    assert "#600 entered the factory" in out.getvalue()

    issue_answer(fake, 610, "Autre")
    assert run(limited(env, 1), ["start", "610"]) == EXIT_REFUSED
    assert "1 in flight, the limit is 1" in err.getvalue()


def test_a_chantier_already_in_flight_is_held_back_by_the_sessions_only() -> None:
    """`start N` of a parked or set-aside chantier at the limit: it adds
    nothing to the count (review of #1823)."""
    six = dict.fromkeys(range(1, 7), CI)
    assert factory(six, {}).why_full(3) is None
    assert factory(six, {}).why_full(9) is not None
    full = factory({**six, 1: DEV, 2: DEV}, {}, sessions=2)
    assert (
        full.why_full(3)
        == full.why_no_session()
        == ("2/2 sessions busy: #1 (1st) · #2 (2nd)")
    )


def test_in_flight_below_one_is_refused_by_the_settings(tmp_path: Path) -> None:
    from tracerworks.settings import ConfigError, _at_least_one

    assert _at_least_one(6, tmp_path) == 6
    with pytest.raises(ConfigError, match="in_flight must be at least 1, not 0"):
        _at_least_one(0, tmp_path)
