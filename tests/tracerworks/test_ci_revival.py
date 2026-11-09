"""No open pull request stays without CI on its head (#1790).

A fake GitHub (its open pull requests, the runs of each head) and the
test's clock: a head with only a cancelled run for 10 min sees that run
re-run and one comment; a run queued or running, a documentation-only
pull request, a conflict, a draft, an author outside the trusted list:
nothing. Once per head.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import FakeRunner, recorded_settings
from tracerworks import ci_revival, store
from tracerworks.merge_queue import GitHub
from tracerworks.process import Result
from tracerworks.settings import Settings

T0 = 1_790_000_000
MIN = 60
HEAD = "a" * 40
FIELDS = (
    "number,headRefOid,headRefName,isDraft,author,mergeable,labels,statusCheckRollup"
)


def pull(**over: Any) -> dict[str, Any]:
    found = {
        "number": 1779,
        "headRefOid": HEAD,
        "headRefName": "agent/1779-x",
        "isDraft": False,
        "author": {"login": "operator-login"},
        "mergeable": "MERGEABLE",
        "labels": [{"name": "merge-queue"}, {"name": "ci-github"}],
        "statusCheckRollup": [],
    }
    return {**found, **over}


CANCELLED = {
    "databaseId": 42,
    "status": "completed",
    "conclusion": "cancelled",
    "startedAt": "2026-10-24T15:30:00Z",
    "url": "https://github.com/owner/repo/actions/runs/42",
}


class World:
    def __init__(self, settings: Settings) -> None:
        self.pulls: list[dict[str, Any]] = [pull()]
        self.runs: list[dict[str, Any]] = [CANCELLED]
        self.reruns: list[str] = []
        self.comments: list[tuple[str, str]] = []
        fake = FakeRunner()
        repo = settings.github_repo
        fake.on(
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--limit", "100", "--json", FIELDS,
        )(lambda call, stdin: Result(0, json.dumps(self.pulls), ""))  # fmt: skip
        fake.on("gh", "run", "list", "--repo", repo, prefix=True)(
            lambda call, stdin: Result(0, json.dumps(self.runs), "")
        )
        fake.on("gh", "run", "rerun", prefix=True)(self._rerun)
        fake.on("gh", "pr", "comment", prefix=True)(self._comment)
        fake.answer("tmux", prefix=True)  # no worker's window
        self.gh = GitHub(fake, settings)

    def _rerun(self, call: Sequence[str], stdin: bytes | None) -> Result:
        self.reruns.append(call[3])
        return Result(0, "", "")

    def _comment(self, call: Sequence[str], stdin: bytes | None) -> Result:
        self.comments.append((call[3], (stdin or b"").decode()))
        return Result(0, "", "")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return recorded_settings(tmp_path)


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(settings.history_db)) as base:
        yield base


def passes(
    db: sqlite3.Connection, world: World, settings: Settings, *minutes: int
) -> list[str]:
    said: list[str] = []
    for at in minutes:
        said += ci_revival.revive(db, world.gh, settings, T0 + at * MIN)
    return said


def test_a_head_without_ci_for_10_min_is_re_run_and_said_once(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    assert passes(db, world, settings, 0, 9) == []  # first seen, then not yet
    assert world.reruns == []
    said = passes(db, world, settings, 10)
    assert said == [
        "PR #1779: head aaaaaaa without CI for 10 min — its last run re-run"
    ]
    assert world.reruns == ["42"]
    [(number, body)] = world.comments
    assert number == "1779" and "CI relancée par tracerworks" in body
    assert passes(db, world, settings, 20, 60) == []  # once per head
    assert world.reruns == ["42"] and len(world.comments) == 1


def test_a_new_head_has_its_own_clock(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    passes(db, world, settings, 0, 10)
    world.pulls = [pull(headRefOid="b" * 40)]
    assert passes(db, world, settings, 11, 21) != []
    assert world.reruns == ["42", "42"]


@pytest.mark.parametrize(
    "runs",
    [
        [{**CANCELLED, "status": "queued", "conclusion": ""}],
        [{**CANCELLED, "status": "in_progress", "conclusion": ""}],
        [{**CANCELLED, "conclusion": "failure"}],  # a verdict: its worker's
        [{**CANCELLED, "conclusion": "success"}],
    ],
    ids=["queued", "running", "red", "green"],
)
def test_a_head_whose_ci_is_alive_or_told_is_left_alone(
    db: sqlite3.Connection, settings: Settings, runs: list[dict[str, Any]]
) -> None:
    world = World(settings)
    world.runs = runs
    assert passes(db, world, settings, 0, 10, 30) == []
    assert world.reruns == [] and world.comments == []


@pytest.mark.parametrize(
    "over",
    [
        {"isDraft": True},
        {"mergeable": "CONFLICTING"},
        {"author": {"login": "someone-else"}},
        {"labels": [{"name": "merge-queue-testing"}]},
        {"statusCheckRollup": [{"name": "checks (nothing)", "conclusion": "SKIPPED"}]},
    ],
    ids=["draft", "conflict", "untrusted", "in-the-queue-s-test", "documentation"],
)
def test_a_pull_request_outside_the_rule_is_not_read(
    db: sqlite3.Connection, settings: Settings, over: dict[str, Any]
) -> None:
    world = World(settings)
    world.pulls = [pull(**over)]
    assert passes(db, world, settings, 0, 10, 30) == []
    assert world.reruns == [] and world.comments == []


def test_its_ci_coming_back_starts_the_clock_again(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    passes(db, world, settings, 0)
    world.runs = [{**CANCELLED, "status": "queued", "conclusion": ""}]
    passes(db, world, settings, 5)
    world.runs = [CANCELLED]
    assert passes(db, world, settings, 6, 15) == []  # seen again at 6 min
    assert passes(db, world, settings, 16) != []


def test_a_head_with_no_run_at_all_is_said_not_re_run(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    world.runs = []
    said = passes(db, world, settings, 0, 10)
    assert said == [
        "PR #1779: head aaaaaaa without CI for 10 min, no run to re-run — said"
    ]
    assert world.reruns == []
    [(_, body)] = world.comments
    assert "git commit --allow-empty" in body


def test_clean_s_pass_revives_after_the_queue(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from tracerworks import cli, merge_queue
    from tracerworks.cli import Env

    world = World(settings)
    clock = [float(T0)]
    out = io.StringIO()
    env = Env(
        settings, runner=world.gh.run, out=out, err=io.StringIO(), now=lambda: clock[0]
    )
    monkeypatch.setattr(merge_queue, "advance", lambda gh, s, now: [])
    cli._advance_queue(env)
    clock[0] += 10 * MIN
    cli._advance_queue(env)
    assert world.reruns == ["42"]
    assert "its last run re-run" in out.getvalue()


def test_a_run_the_merge_queue_cancelled_while_it_waits_is_not_revived(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """#2109: waiting in the queue, its run cancelled so the queue's own run
    has the heavy runners — the queue reruns everything at its turn."""
    db.execute(
        "INSERT INTO merge_queue (project, pull, entered_at, state)"
        " VALUES (?, 1779, ?, 'waiting')",
        (settings.github_repo, T0 - 5 * MIN),
    )
    db.execute(
        "INSERT INTO merge_queue_cancel (project, pull, head, run, attempt, at, url)"
        " VALUES (?, 1779, ?, 42, 2, ?, ?)",
        (settings.github_repo, HEAD, T0 - MIN, CANCELLED["url"]),
    )
    world = World(settings)
    assert passes(db, world, settings, 0, 10, 30) == []
    assert world.reruns == [] and world.comments == []
    # Gone without a test: still the queue's cancel, never revived.
    db.execute("UPDATE merge_queue SET state = 'out', outcome = 'gone'")
    assert passes(db, world, settings, 40, 50) == []
    # Tested since (the queue reran it): its rule again.
    db.execute("UPDATE merge_queue SET started_at = ?", (T0 + 55 * MIN,))
    passes(db, world, settings, 60, 70)
    assert world.reruns == ["42"]


def test_a_new_head_after_the_queue_s_cancel_keeps_its_revival(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """The cancel was of another head's run (#2139's review): the new head's
    own CI, cancelled, is revived as any other."""
    db.execute(
        "INSERT INTO merge_queue (project, pull, entered_at, state)"
        " VALUES (?, 1779, ?, 'waiting')",
        (settings.github_repo, T0 - 5 * MIN),
    )
    db.execute(
        "INSERT INTO merge_queue_cancel (project, pull, head, run, attempt, at, url)"
        " VALUES (?, 1779, 'older', 41, 2, ?, 'u')",
        (settings.github_repo, T0 - MIN),
    )
    world = World(settings)
    passes(db, world, settings, 0, 10)
    assert world.reruns == ["42"]
