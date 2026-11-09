"""A test the factory recognises flaky opens its bug by itself (#2224).

Against a history base filled as the recorder fills it (`test_quality`'s
helpers): opening, never two, a comment per new failure, the closing, the
thresholds — and GitHub as a fake that records what it was asked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from tests.tracerworks.fakes import recorded_settings
from tests.tracerworks.test_quality import DOCS, HOUR, NOW, REPO, T, branch_files, run
from tracerworks import store
from tracerworks.flaky_bugs import (
    DAY,
    Thresholds,
    Tracked,
    decide,
    tend,
    title_names,
)
from tracerworks.process import CommandError
from tracerworks.quality import Failure, flaky_failures
from tracerworks.settings import ConfigError, Settings

RULE = Thresholds(times=2, days=7, close_days=14)
U = "https://x/runs/1"


def flaky(at: float, rule: str = "même commit") -> Failure:
    return Failure(at, U, (rule,))


# --- the rule, pure -------------------------------------------------------------------


def test_twice_in_seven_days_opens_with_its_runs() -> None:
    [action] = decide({T: [flaky(NOW - 3 * DAY), flaky(NOW - DAY)]}, {}, NOW, RULE)
    assert (action.kind, action.test, len(action.failures)) == ("open", T, 2)


@pytest.mark.parametrize(
    "failures",
    [
        [flaky(NOW - DAY)],  # once
        [flaky(NOW - 10 * DAY), flaky(NOW - DAY)],  # 10 days apart: once in 7
        [Failure(NOW - 2 * DAY, U), Failure(NOW - DAY, U)],  # red, never flaky
    ],
)
def test_short_of_the_threshold_nothing_opens(failures: list[Failure]) -> None:
    assert decide({T: failures}, {}, NOW, RULE) == []


@pytest.mark.parametrize(
    ("rules", "opens"),
    [
        (("2 PR", "2 PR"), False),  # one episode on two pull requests
        (("2 PR", "2 PR", "2 PR"), True),  # a third pull request: twice
        (("même commit", "2 PR"), False),
        (("même commit", "même commit"), True),
        (("même commit", "2 PR", "2 PR"), True),
    ],
)
def test_one_episode_on_two_pull_requests_is_one_recognition(
    rules: tuple[str, ...], opens: bool
) -> None:
    failures = [
        flaky(NOW - (len(rules) - i) * DAY, rule) for i, rule in enumerate(rules)
    ]
    assert bool(decide({T: failures}, {}, NOW, RULE)) is opens


def test_the_thresholds_are_the_settings() -> None:
    twice = {T: [flaky(NOW - 3 * DAY), flaky(NOW - DAY)]}
    assert decide(twice, {}, NOW, Thresholds(times=3)) == []
    assert decide(twice, {}, NOW, Thresholds(times=2, days=2)) == []
    assert decide(twice, {}, NOW, Thresholds(times=2, days=3))[0].kind == "open"


def test_a_tracked_test_is_never_opened_again_and_its_new_failure_commented() -> None:
    tracked = {T: Tracked(42, NOW - DAY)}
    failures = {T: [flaky(NOW - 3 * DAY), flaky(NOW - DAY), Failure(NOW - HOUR, U)]}
    [action] = decide(failures, tracked, NOW, RULE)
    assert (action.kind, action.issue) == ("comment", 42)
    assert [f.at for f in action.failures] == [NOW - HOUR]  # any red, flaky or not
    said = {T: Tracked(42, NOW - HOUR)}
    assert decide(failures, said, NOW, RULE) == []


@pytest.mark.parametrize(("quiet", "closes"), [(14, True), (13, False)])
def test_fourteen_days_without_a_failure_close_it(quiet: int, closes: bool) -> None:
    last = NOW - quiet * DAY
    actions = decide({T: [flaky(last)]}, {T: Tracked(42, last)}, NOW, RULE)
    assert [(a.kind, a.issue, a.last) for a in actions] == (
        [("close", 42, last)] if closes else []
    )


def test_a_title_names_a_test_by_its_id_or_its_function() -> None:
    assert title_names(T, f"Flaky test: {T}")
    assert title_names(T, "Flaky test in the merge queue: `test_the_file_moves`")
    assert not title_names(T, "Flaky test in the merge queue: test_the_file_moves_on")


# --- the base: which failure is recognised --------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(tmp_path / "f.sqlite3")) as base:
        yield base


def test_each_failure_carries_the_rules_that_recognise_it(
    db: sqlite3.Connection,
) -> None:
    run(db, "b1", "s1", NOW - 3 * DAY, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]}, run_id=500)  # fmt: skip
    run(db, "b1", "s1", NOW - 3 * DAY + HOUR, jobs={"browser": "success"},
        run_id=500, attempt=2)  # fmt: skip
    branch_files(db, "b1", DOCS)
    run(db, "b2", "s2", NOW - DAY, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]})  # fmt: skip
    branch_files(db, "b2", DOCS)
    run(db, "main", "s3", NOW - HOUR, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]})  # fmt: skip
    found = flaky_failures(db, REPO, "main")[T]
    assert [f.rules for f in found] == [("même commit", "2 PR"), ("2 PR",), ()]


# --- one pass, against GitHub ---------------------------------------------------------


class FakeIssues:
    def __init__(self, bugs: list[tuple[int, str]] | None = None) -> None:
        self.bugs = list(bugs or [])
        self.opened: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.refuse = False

    def open_bugs(self) -> list[tuple[int, str]]:
        made = [(900 + i, t) for i, (t, _) in enumerate(self.opened, 1)]
        return [b for b in [*self.bugs, *made] if b[0] not in self.closed]

    def open_bug(self, title: str, body: str) -> int:
        if self.refuse:
            raise CommandError("gh issue create failed (exit 1): HTTP 502")
        self.opened.append((title, body))
        return 900 + len(self.opened)

    def comment_issue(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close_issue(self, number: int, body: str) -> None:
        self.comments.append((number, body))
        self.closed.append(number)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return recorded_settings(tmp_path)


def failing(settings: Settings, *ats: float) -> None:
    """`T` red then green at the same commit, at each instant."""
    with closing(store.connect(settings.history_db)) as db:
        for at in ats:
            run_id = int(at)
            run(db, "b1", f"s{run_id}", at, "failure", jobs={"browser": "failure"},
                failed={"browser": [T]}, run_id=run_id)  # fmt: skip
            run(db, "b1", f"s{run_id}", at + 60, jobs={"browser": "success"},
                run_id=run_id, attempt=2)  # fmt: skip


def test_a_pass_opens_one_bug_then_comments_then_closes(settings: Settings) -> None:
    gh = FakeIssues()
    failing(settings, NOW - 3 * DAY, NOW - DAY)

    said = tend(gh, settings, NOW, RULE)
    [(title, body)] = gh.opened
    assert title == f"Flaky test: {T}"
    assert "Fix it at its cause, never rerun it" in body
    assert body.count("même commit") >= 2 and "https://x/runs/" in body
    assert said == [f"flaky test {T}: bug #901 opened (2 times in 7 days)"]

    # NEVER TWO, even from a fresh process: the base remembers.
    assert tend(FakeIssues(), settings, NOW + HOUR, RULE) == []

    failing(settings, NOW + 2 * HOUR)
    said = tend(gh, settings, NOW + 3 * HOUR, RULE)
    [(number, comment)] = gh.comments
    assert number == 901 and "New failure" in comment
    assert said == [f"flaky test {T}: bug #901 commented (1 new failure(s))"]

    later = NOW + 2 * HOUR + 60 + 14 * DAY
    said = tend(gh, settings, later, RULE)
    assert gh.closed == [901] and "closed by tracerworks" in gh.comments[-1][1]
    with closing(store.connect(settings.history_db)) as db:
        assert db.execute("SELECT count(*) FROM flaky_bug").fetchone() == (0,)


def test_the_open_bug_that_names_it_is_adopted_never_doubled(
    settings: Settings,
) -> None:
    gh = FakeIssues([(77, "Flaky test in the merge queue: test_the_file_moves")])
    failing(settings, NOW - 3 * DAY, NOW - DAY)
    tend(gh, settings, NOW, RULE)
    assert gh.opened == [] and [n for n, _ in gh.comments] == [77]
    failing(settings, NOW + HOUR)
    tend(gh, settings, NOW + 2 * HOUR, RULE)
    assert [n for n, _ in gh.comments] == [77, 77]


def test_a_refusal_is_said_and_the_next_pass_tries_again(settings: Settings) -> None:
    gh = FakeIssues()
    gh.refuse = True
    failing(settings, NOW - 3 * DAY, NOW - DAY)
    [said] = tend(gh, settings, NOW, RULE)
    assert "NOT tended" in said and "HTTP 502" in said
    gh.refuse = False
    tend(gh, settings, NOW + 60, RULE)
    assert len(gh.opened) == 1


def test_the_thresholds_come_from_the_settings(settings: Settings) -> None:
    three = replace(
        settings, merge_queue=replace(settings.merge_queue, flaky_bug_times=3)
    )
    failing(settings, NOW - 3 * DAY, NOW - DAY)
    assert tend(FakeIssues(), three, NOW) == []
    assert len(tend(FakeIssues(), settings, NOW)) == 1  # the default: 2


# --- tracerworks.toml -----------------------------------------------------------------


def test_the_file_sets_the_thresholds_and_refuses_zero(tmp_path: Path) -> None:
    from tests.tracerworks.fakes import FIXTURES
    from tracerworks.settings import load

    text = (FIXTURES / "project.toml").read_text()
    assert "[merge_queue]" in text
    tuned = tmp_path / "tracerworks.toml"
    tuned.write_text(
        text.replace(
            "[merge_queue]\n",
            "[merge_queue]\nflaky_bug_times = 3\nflaky_bug_close_days = 21\n",
        )
    )
    queue = load(str(tuned), {}).merge_queue
    assert (queue.flaky_bug_times, queue.flaky_bug_days) == (3, 7)
    assert queue.flaky_bug_close_days == 21
    tuned.write_text(
        text.replace("[merge_queue]\n", "[merge_queue]\nflaky_bug_days = 0\n")
    )
    with pytest.raises(
        ConfigError, match="flaky_bug_days must be a whole number from 1"
    ):
        load(str(tuned), {})


def test_a_bug_closed_by_somebody_else_is_forgotten_not_closed_again(
    settings: Settings,
) -> None:
    gh = FakeIssues()
    failing(settings, NOW - 3 * DAY, NOW - DAY)
    tend(gh, settings, NOW, RULE)
    gh.closed.append(901)  # Sam closes it, a fix's « Closes »
    failing(settings, NOW + HOUR)
    [said] = tend(gh, settings, NOW + 2 * HOUR, RULE)
    assert "closed by somebody else" in said and gh.comments == []
    with closing(store.connect(settings.history_db)) as db:
        assert db.execute("SELECT count(*) FROM flaky_bug").fetchone() == (0,)
