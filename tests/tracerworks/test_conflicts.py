"""A pull request in conflict with the base no longer stays silent (#1791).

A fake GitHub (its open pull requests) and the test's clock: `CONFLICTING`
for 10 min wakes the worker in its window, once per conflict; without a
worker, one comment and an alert while it lasts; a parked worker is left
to its resume. `MERGEABLE` again ends the episode: a new conflict is told
again.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import FakeRunner, recorded_settings
from tracerworks import ci_revival, conflicts, store
from tracerworks.alerts import conditions
from tracerworks.merge_queue import GitHub
from tracerworks.process import CommandError, Result
from tracerworks.settings import Settings

T0 = 1_790_000_000
MIN = 60


def open_pull(mergeable: str, **over: Any) -> ci_revival.Open:
    found = ci_revival.Open(
        number=1779,
        head="a" * 40,
        branch="agent/1779-ci-github",
        draft=False,
        author="operator-login",
        mergeable=mergeable,
        labels=frozenset(),
        nothing=False,
    )
    return replace(found, **over)


class World:
    def __init__(self, settings: Settings) -> None:
        self.comments: list[tuple[str, str]] = []
        self.told: list[tuple[int, str]] = []
        self.unheard = False
        fake = FakeRunner()
        fake.on("gh", "pr", "comment", prefix=True)(self._comment)
        self.gh = GitHub(fake, settings)

    def _comment(self, call: Sequence[str], stdin: bytes | None) -> Result:
        self.comments.append((call[3], (stdin or b"").decode()))
        return Result(0, "", "")

    def tell(self, spec: int, text: str) -> None:
        if self.unheard:
            raise CommandError("not received")
        self.told.append((spec, text))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return recorded_settings(tmp_path)


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(settings.history_db)) as base:
        yield base


def tend(
    db: sqlite3.Connection,
    world: World,
    settings: Settings,
    mergeable: str,
    at: int,
    *,
    windows: Sequence[int] = (),
    parked: Sequence[int] = (),
    **over: Any,
) -> list[str]:
    return conflicts.tend(
        db,
        world.gh,
        settings,
        [open_pull(mergeable, **over)],
        T0 + at * MIN,
        windows=windows,
        parked=parked,
        tell=world.tell,
    )


def test_a_conflict_wakes_its_worker_once_after_10_min(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    for at in (0, 9):
        assert tend(db, world, settings, "CONFLICTING", at, windows=[1779]) == []
    said = tend(db, world, settings, "CONFLICTING", 10, windows=[1779])
    assert said == [
        "PR #1779 in conflict for 10 min — worker #1779 told to merge the base"
    ]
    [(spec, text)] = world.told
    assert spec == 1779 and "git merge origin/main" in text
    assert tend(db, world, settings, "CONFLICTING", 30, windows=[1779]) == []
    assert len(world.told) == 1 and world.comments == []


def test_mergeable_again_then_conflicting_is_a_new_conflict(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    tend(db, world, settings, "CONFLICTING", 0, windows=[1779])
    tend(db, world, settings, "CONFLICTING", 10, windows=[1779])
    assert tend(db, world, settings, "MERGEABLE", 20, windows=[1779]) == [
        "PR #1779: mergeable again"
    ]
    tend(db, world, settings, "CONFLICTING", 30, windows=[1779])
    tend(db, world, settings, "CONFLICTING", 40, windows=[1779])
    assert len(world.told) == 2


def test_unknown_changes_nothing(db: sqlite3.Connection, settings: Settings) -> None:
    world = World(settings)
    tend(db, world, settings, "UNKNOWN", 0, windows=[1779])  # GitHub computing
    tend(db, world, settings, "CONFLICTING", 5, windows=[1779])  # seen: 5 min
    tend(db, world, settings, "CONFLICTING", 10, windows=[1779])
    assert world.told == []
    tend(db, world, settings, "CONFLICTING", 15, windows=[1779])
    assert len(world.told) == 1


def test_without_a_worker_one_comment_and_an_alert_while_it_lasts(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    tend(db, world, settings, "CONFLICTING", 0)
    said = tend(db, world, settings, "CONFLICTING", 10)
    assert said == [
        "PR #1779 in conflict for 10 min, no worker — commented, alert open"
    ]
    tend(db, world, settings, "CONFLICTING", 60)
    [(number, body)] = world.comments
    assert number == "1779" and "aucun worker d'tracerworks ne la porte" in body
    assert world.told == []

    def alarm(at: int) -> list[str]:
        found, known = conditions(
            db, settings.github_repo, "main", "ci.yml", T0 + at * MIN,
            operator="op", human_label="needs-human", blocked={},
            conflicts=conflicts.silent(db, settings.github_repo),
        )  # fmt: skip
        assert f"conflict {settings.github_repo}" in known
        return [c.text for c in found if c.key.startswith("conflict ")]

    [text] = alarm(60)
    assert text.startswith("PR #1779 in conflict with the base for 60 min")
    tend(db, world, settings, "MERGEABLE", 70)
    assert alarm(70) == []  # the episode closes: « rétabli »


def test_a_parked_worker_is_left_to_its_resume(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    tend(db, world, settings, "CONFLICTING", 0, parked=[1779])
    said = tend(db, world, settings, "CONFLICTING", 10, parked=[1779])
    assert said == [
        "PR #1779 in conflict for 10 min — #1779 parked: its resume says it"
    ]
    assert world.told == [] and world.comments == []
    assert conflicts.silent(db, settings.github_repo) == []


def test_a_message_not_heard_is_tried_again(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    tend(db, world, settings, "CONFLICTING", 0, windows=[1779])
    world.unheard = True
    [line] = tend(db, world, settings, "CONFLICTING", 10, windows=[1779])
    assert "NOT told" in line
    world.unheard = False
    tend(db, world, settings, "CONFLICTING", 11, windows=[1779])
    assert len(world.told) == 1


@pytest.mark.parametrize(
    "over",
    [{"draft": True}, {"author": "someone-else"}],
    ids=["draft", "untrusted"],
)
def test_a_draft_or_an_untrusted_author_is_not_read(
    db: sqlite3.Connection, settings: Settings, over: dict[str, Any]
) -> None:
    world = World(settings)
    tend(db, world, settings, "CONFLICTING", 0, **over)
    assert tend(db, world, settings, "CONFLICTING", 30, **over) == []
    assert world.comments == [] and world.told == []


def test_a_pull_request_no_longer_open_closes_its_episode(
    db: sqlite3.Connection, settings: Settings
) -> None:
    world = World(settings)
    tend(db, world, settings, "CONFLICTING", 0)
    tend(db, world, settings, "CONFLICTING", 10)
    conflicts.tend(
        db, world.gh, settings, [], T0 + 20 * MIN,
        windows=(), parked=(), tell=world.tell,
    )  # fmt: skip
    assert conflicts.silent(db, settings.github_repo) == []


def test_clean_s_pass_reads_the_pull_requests_once_for_both(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracerworks import cli, merge_queue
    from tracerworks.cli import Env

    listed = [
        {
            "number": 1779, "headRefOid": "a" * 40, "headRefName": "agent/1779-x",
            "isDraft": False, "author": {"login": "operator-login"},
            "mergeable": "CONFLICTING", "labels": [], "statusCheckRollup": [],
        }
    ]  # fmt: skip
    fake = FakeRunner()
    fake.answer(
        "gh", "pr", "list", "--repo", settings.github_repo, "--state", "open",
        "--limit", "100", prefix=True, stdout=json.dumps(listed),
    )  # fmt: skip
    fake.answer("gh", "pr", "comment", prefix=True)
    fake.answer("gh", "run", "list", prefix=True, stdout="[]")
    fake.answer("tmux", prefix=True)  # no window
    clock = [float(T0)]
    out = io.StringIO()
    env = Env(settings, runner=fake, out=out, err=io.StringIO(), now=lambda: clock[0])
    monkeypatch.setattr(merge_queue, "advance", lambda gh, s, now: [])
    cli._advance_queue(env)
    clock[0] += 10 * MIN
    cli._advance_queue(env)
    assert "PR #1779 in conflict for 10 min, no worker" in out.getvalue()
    assert len(fake.ran("gh", "pr", "comment")) == 1
    assert len(fake.ran("gh", "pr", "list")) == 2  # once a pass, for both
    # a conflict: no revival; its head's runs read each pass for #2013, none alive
    assert len(fake.ran("gh", "run", "list")) == 2
    assert fake.ran("gh", "run", "rerun") == [] and fake.ran("gh", "api") == []
    status = cli.conflict_lines(env)
    assert status == ["PR #1779 in conflict with the base for 10 min, no worker"]
