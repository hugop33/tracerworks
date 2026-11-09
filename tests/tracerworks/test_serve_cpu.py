"""`serve`'s CPU at rest (#2196): one reading of a source serves every
thread of a cycle, and nothing GitHub is asked twice for one snapshot.

Measured on the VPS on 01/11: a snapshot cost 3.15 s of CPU (2.58 s in
`gh`), the history's minute 0.70 s; `actions/runs` was read three times a
minute (twice by the snapshot, once by the history), `actions/runners`
twice."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from tests.tracerworks.fakes import recorded_world
from tests.tracerworks.test_snapshot import github_world, snapshot
from tracerworks.process import Result, read_once

RUNS = ["gh", "api", "repos/o/r/actions/runs?per_page=100"]
EMPTY = Result(0, "[]")


class Counted:
    """A runner double that counts each argv, and answers `answer`."""

    def __init__(self, answer: Result = EMPTY) -> None:
        self.asked: Counter[tuple[str, ...]] = Counter()
        self.answer = answer

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> Result:
        self.asked[tuple(argv)] += 1
        return self.answer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_gh_api_read_is_asked_once_within_its_window() -> None:
    inner, clock = Counted(), Clock()
    run = read_once(inner, clock, 30)
    assert run(RUNS) == run(RUNS) == Result(0, "[]")
    clock.now += 29
    run(RUNS)
    assert inner.asked[tuple(RUNS)] == 1
    clock.now += 1
    run(RUNS)
    assert inner.asked[tuple(RUNS)] == 2


def test_another_read_or_another_program_is_its_own() -> None:
    inner = Counted()
    run = read_once(inner, Clock(), 30)
    runners = ["gh", "api", "repos/o/r/actions/runners"]
    for argv in (RUNS, runners, ["git", "status"], ["git", "status"]):
        run(argv)
    assert inner.asked[tuple(RUNS)] == inner.asked[tuple(runners)] == 1
    assert inner.asked[("git", "status")] == 2  # local, cheap, never kept


def test_the_same_read_under_way_is_waited_for_not_asked_again() -> None:
    """The snapshot's threads ask the latest runs at the same moment."""
    entered, release = threading.Event(), threading.Event()
    inner = Counted()

    def slow(
        argv: Sequence[str], *, cwd: Path | None = None, stdin: bytes | None = None
    ) -> Result:
        entered.set()
        release.wait(5)
        return inner(argv)

    run = read_once(slow, Clock(), 30)
    answers: list[Result] = []
    first = threading.Thread(target=lambda: answers.append(run(RUNS)))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=lambda: answers.append(run(RUNS)))
    second.start()
    second.join(0.1)  # it waits for the first one's answer
    release.set()
    first.join(5)
    second.join(5)
    assert answers == [Result(0, "[]")] * 2
    assert inner.asked[tuple(RUNS)] == 1


def test_a_failed_read_is_not_kept() -> None:
    inner = Counted(Result(1, "", "HTTP 502"))
    run = read_once(inner, Clock(), 30)
    run(RUNS)
    run(RUNS)
    assert inner.asked[tuple(RUNS)] == 2


def test_other_gh_reads_go_through_and_forget_nothing() -> None:
    inner = Counted()
    run = read_once(inner, Clock(), 30)
    listing = ["gh", "issue", "list", "-R", "o/r", "--state", "open"]
    run(RUNS)
    run(listing)
    run(listing)
    run(["gh", "pr", "view", "7"])
    run(RUNS)
    assert inner.asked[tuple(listing)] == 2  # never kept: not a `gh api` read
    assert inner.asked[tuple(RUNS)] == 1


def test_a_write_is_never_kept_and_forgets_every_read() -> None:
    """A star, a label, a variable, a comment: what was read before it may
    have changed — read again."""
    inner = Counted()
    run = read_once(inner, Clock(), 30)
    writes = [
        ["gh", "api", "-X", "PATCH", "repos/o/r/actions/variables/X", "-f", "v=1"],
        ["gh", "api", "repos/o/r/issues/1/labels", "-f", "labels[]=star"],
        ["gh", "api", "--method", "POST", "repos/o/r/issues/1/comments"],
        ["gh", "api", "graphql", "--input", "-"],
        ["gh", "issue", "edit", "1", "--add-label", "x"],
        ["gh", "issue", "comment", "1", "--body-file", "-"],
        ["gh", "run", "rerun", "5", "--failed"],
    ]
    for write in writes:
        run(RUNS)
        run(write)
        run(write)
        assert inner.asked[tuple(write)] == 2
    assert inner.asked[tuple(RUNS)] == len(writes)


def test_one_snapshot_asks_github_each_thing_once(tmp_path: Path) -> None:
    """The control panel and the deliveries both list the latest runs; the
    runners are read for the panel and for the launch's admission."""
    fake = github_world(recorded_world())
    clock = Clock()
    snapshot(read_once(fake, clock, 30), tmp_path)  # type: ignore[arg-type]
    asked = Counter(tuple(c.argv) for c in fake.calls if c.argv[0] == "gh")
    assert [argv for argv, n in asked.items() if n > 1] == []
