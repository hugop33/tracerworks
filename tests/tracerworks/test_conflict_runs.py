"""The run of a worker's head in conflict is cancelled, once per head (#2013).

A fake GitHub (its open pull requests, the runs of each head): a worker's
pull request `CONFLICTING`, outside the queue's test, with a run of its head
still queued or running, sees that run force-cancelled and one comment; the
next pass, nothing. In the queue's test, without a conflict, without a run
alive: nothing. A head whose run was cancelled so is never revived (#1790).
The case of 28/10: #1996, run 40763050431 queued without a runner.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from tests.tracerworks.fakes import FakeRunner, recorded_settings
from tests.tracerworks.test_ci_revival import CANCELLED, MIN, T0, pull
from tests.tracerworks.test_ci_revival import World as RevivalWorld
from tracerworks import ci_revival, conflict_runs, store
from tracerworks.process import Result
from tracerworks.settings import Settings

RUN = 40763050431
QUEUED = {
    **CANCELLED,
    "databaseId": RUN,
    "status": "queued",
    "conclusion": "",
    "url": f"https://github.com/owner/repo/actions/runs/{RUN}",
}


class World(RevivalWorld):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.pulls = [pull(number=1996, headRefName="agent/1974-x",
                           mergeable="CONFLICTING", labels=[])]  # fmt: skip
        self.runs = [QUEUED]
        self.cancelled: list[str] = []
        self.refuse = False
        fake = cast(FakeRunner, self.gh.run)
        fake.on("gh", "api", "-X", "POST", prefix=True)(self._cancel)

    def _cancel(self, call: Sequence[str], stdin: bytes | None) -> Result:
        if self.refuse:
            return Result(1, "", "HTTP 502")
        self.cancelled.append(call[4])
        self.runs = [{**r, "status": "completed", "conclusion": "cancelled"}
                     for r in self.runs]  # fmt: skip
        return Result(0, "", "")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return recorded_settings(tmp_path)


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(settings.history_db)) as base:
        yield base


def tend(
    db: sqlite3.Connection, world: World, settings: Settings, at: int
) -> list[str]:
    pulls = ci_revival.open_pulls(world.gh, settings)
    return conflict_runs.tend(db, world.gh, settings, pulls, T0 + at * MIN)


def test_the_run_of_a_head_in_conflict_is_force_cancelled_and_said_once(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """#1996 on 28/10: its old head's run queued 25 min without a runner."""
    world = World(settings)
    said = tend(db, world, settings, 0)
    assert world.cancelled == [f"repos/owner/repo/actions/runs/{RUN}/force-cancel"]
    assert said == [
        f"PR #1996: head aaaaaaa in conflict — its run {RUN} cancelled (#2013)"
    ]
    [(number, body)] = world.comments
    assert number == "1996"
    assert f"actions/runs/{RUN}" in body and "conflit" in body
    assert tend(db, world, settings, 1) == []  # once per head
    world.runs = [{**QUEUED, "status": "in_progress"}]  # even a run come back
    assert tend(db, world, settings, 2) == []
    assert len(world.cancelled) == 1 and len(world.comments) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"labels": [{"name": "merge-queue-testing"}]},  # the queue's own rule
        {"mergeable": "MERGEABLE"},
        {"mergeable": "UNKNOWN"},
        {"headRefName": "fix/mac-session"},  # no worker of tracerworks
        {"author": {"login": "stranger"}},
    ],
)
def test_nothing_is_cancelled_outside_a_worker_s_conflict(
    db: sqlite3.Connection, settings: Settings, over: dict[str, object]
) -> None:
    world = World(settings)
    world.pulls = [{**world.pulls[0], **over}]
    assert tend(db, world, settings, 0) == []
    assert world.cancelled == [] and world.comments == []


def test_a_conflict_without_a_run_alive_yet_is_read_again(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """GitHub may say `CONFLICTING` before the run of a fresh push is
    queued: nothing cancelled, nothing written — the run queued after is."""
    world = World(settings)
    world.runs = [CANCELLED, {**CANCELLED, "conclusion": "failure"}]
    assert tend(db, world, settings, 0) == []
    assert world.cancelled == [] and world.comments == []
    world.runs = [QUEUED]
    assert len(tend(db, world, settings, 1)) == 1
    assert len(world.cancelled) == 1 and len(world.comments) == 1


def test_a_cancel_github_refuses_is_said_and_tried_again(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    world.refuse = True
    [line] = tend(db, world, settings, 0)
    assert line.startswith("PR #1996: its run NOT cancelled (next pass)")
    assert world.comments == []
    world.refuse = False
    assert len(tend(db, world, settings, 1)) == 1
    assert len(world.cancelled) == 1 and len(world.comments) == 1


def test_a_head_cancelled_for_its_conflict_is_never_revived(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Its conflict gone without a push (the base moved back): the head has
    only the run cancelled on purpose — #1790 does not re-run it."""
    world = World(settings)
    tend(db, world, settings, 0)
    world.pulls = [{**world.pulls[0], "mergeable": "MERGEABLE"}]
    for at in (1, 20, 60):
        assert ci_revival.revive(db, world.gh, settings, T0 + at * MIN) == []
    assert world.reruns == []


def test_the_head_after_the_merge_is_revived_as_any_other(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    tend(db, world, settings, 0)
    world.pulls = [{**world.pulls[0], "mergeable": "MERGEABLE",
                    "headRefOid": "b" * 40}]  # fmt: skip
    world.runs = [CANCELLED]
    ci_revival.revive(db, world.gh, settings, T0 + 1 * MIN)
    assert ci_revival.revive(db, world.gh, settings, T0 + 11 * MIN) != []
    assert world.reruns == ["42"]
