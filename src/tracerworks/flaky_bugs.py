"""A test the factory recognises flaky opens its bug by itself (#2224).

Spec #2219: a flaky test was rerun in silence — three reruns of the queue,
eight pull requests parked, fourteen reds read and left — and nobody fixed
it. The detection existed (`quality.flaky_tests`, its two rules: green again
at the same commit; red on two pull requests that do not touch it, within a
week). It now acts, on the model of the hosting plan's issue that opens and
closes by itself (#2206):

- a test recognised flaky `flaky_bug_times` times within `flaky_bug_days`
  opens ONE issue `bug` in its name, with its runs, its rules and the rule
  « fix it at its cause, never rerun » — or adopts the open `bug` whose title
  names it already (the merge queue's, #2130): never two;
- each new failure of it comments that issue;
- `flaky_bug_close_days` without a failure close it, with a comment.

The issue is kept in the history (`flaky_bug`): one per test while open,
found again after a restart. Nothing is rerun, skipped or set aside here.
The rule is pure (`decide`); `tend` reads the base and talks to GitHub.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from tracerworks import store, texts
from tracerworks.process import CommandError
from tracerworks.quality import SAME_COMMIT, TWO_PULLS, Failure, flaky_failures
from tracerworks.settings import Settings

DAY = 86_400


@dataclass(frozen=True)
class Tracked:
    issue: int
    #: the last failure the issue has said (its opening or a comment)
    said_until: float


@dataclass(frozen=True)
class Thresholds:
    times: int = 2
    days: int = 7
    close_days: int = 14


@dataclass(frozen=True)
class Action:
    kind: Literal["open", "comment", "close"]
    test: str
    failures: tuple[Failure, ...] = ()
    issue: int | None = None
    last: float | None = None  # a closing's last failure


def decide(
    failures: Mapping[str, Sequence[Failure]],
    tracked: Mapping[str, Tracked],
    now: float,
    rule: Thresholds,
) -> list[Action]:
    """What to do for each test: open its bug, comment it, close it."""
    actions: list[Action] = []
    for test, known in sorted(tracked.items()):
        mine = sorted(failures.get(test, ()), key=lambda f: f.at)
        new = tuple(f for f in mine if f.at > known.said_until)
        last = max([known.said_until, *(f.at for f in mine)])
        if new:
            actions.append(Action("comment", test, new, known.issue))
        elif now - last >= rule.close_days * DAY:
            actions.append(Action("close", test, (), known.issue, last))
    for test, listed in sorted(failures.items()):
        if test in tracked:
            continue
        recent = tuple(
            f
            for f in sorted(listed, key=lambda f: f.at)
            if f.rules and now - f.at <= rule.days * DAY
        )
        if recognitions(recent) >= rule.times:
            actions.append(Action("open", test, recent))
    return actions


def recognitions(failures: Sequence[Failure]) -> int:
    """How many times these failures recognise a test flaky: each one green
    again at its commit is one; red on N pull requests that do not touch it
    is N - 1 — the first one recognises nothing alone, the second makes the
    first episode (#2224's review: one episode is not two)."""
    same = sum(1 for f in failures if SAME_COMMIT in f.rules)
    pulls = sum(
        1 for f in failures if SAME_COMMIT not in f.rules and TWO_PULLS in f.rules
    )
    return same + max(0, pulls - 1)


def tracked_of(db: sqlite3.Connection, project: str) -> dict[str, Tracked]:
    return {
        test: Tracked(int(issue), float(said))
        for test, issue, said in db.execute(
            "SELECT test, issue, said_until FROM flaky_bug WHERE project = ?",
            (project,),
        )
    }


class Issues(Protocol):
    def open_bugs(self) -> list[tuple[int, str]]: ...
    def open_bug(self, title: str, body: str) -> int: ...
    def comment_issue(self, number: int, body: str) -> None: ...
    def close_issue(self, number: int, body: str) -> None: ...


def _when(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%d/%m %H:%M UTC")


def runs(failures: Sequence[Failure]) -> str:
    return "\n".join(
        texts.FLAKY_SELF_RUN.format(
            when=_when(f.at), rules=", ".join(f.rules) or "red", url=f.url
        )
        for f in failures
    )


def title_names(test: str, title: str) -> bool:
    """Whether a bug's title names `test`: its id, or its function's name."""
    words = {w.strip("`'\"(),.:;!?") for w in title.split()} | set(title.split())
    return bool({test, test.split("::")[-1].split("[", 1)[0]} & words)


def tend(
    gh: Issues, settings: Settings, now: float, rule: Thresholds | None = None
) -> list[str]:
    """One pass: the rule's actions done on GitHub and recorded. A refusal
    of GitHub is said, and the next pass tries again (nothing recorded)."""
    queue = settings.merge_queue
    rule = rule or Thresholds(
        queue.flaky_bug_times, queue.flaky_bug_days, queue.flaky_bug_close_days
    )
    project = settings.github_repo
    with closing(store.connect(settings.history_db)) as db:
        failures = flaky_failures(db, project, settings.base_branch)
        tracked = tracked_of(db, project)
        actions = decide(failures, tracked, now, rule)
        said: list[str] = []
        bugs: list[tuple[int, str]] | None = None
        for action in actions:
            try:
                if bugs is None:
                    bugs = gh.open_bugs()
                if action.issue is not None and action.issue not in {
                    number for number, _ in bugs
                }:
                    # CLOSED BY SOMEBODY ELSE (the operator, a fix's
                    # « Closes »): forgotten, never commented nor closed again;
                    # a new flaky failure opens another.
                    _forget(db, project, action.test)
                    said.append(
                        texts.FLAKY_SELF_CLOSED_ELSEWHERE.format(
                            test=action.test, bug=action.issue
                        )
                    )
                    continue
                if action.kind == "open":
                    issue = _open(gh, bugs, action, rule)
                    last = max(f.at for f in failures[action.test])
                    _record(db, project, action.test, issue, now, last, insert=True)
                    said.append(
                        texts.FLAKY_SELF_OPENED.format(
                            test=action.test,
                            bug=issue,
                            n=len(action.failures),
                            days=rule.days,
                        )
                    )
                elif action.kind == "comment":
                    assert action.issue is not None
                    gh.comment_issue(
                        action.issue,
                        texts.FLAKY_SELF_AGAIN.format(
                            test=action.test, runs=runs(action.failures)
                        ),
                    )
                    last = max(f.at for f in action.failures)
                    _record(db, project, action.test, action.issue, now, last)
                    said.append(
                        texts.FLAKY_SELF_COMMENTED.format(
                            test=action.test,
                            bug=action.issue,
                            n=len(action.failures),
                        )
                    )
                else:
                    assert action.issue is not None and action.last is not None
                    gh.close_issue(
                        action.issue,
                        texts.FLAKY_SELF_CLOSED.format(
                            test=action.test,
                            days=rule.close_days,
                            last=_when(action.last),
                        ),
                    )
                    _forget(db, project, action.test)
                    said.append(
                        texts.FLAKY_SELF_CLOSED_SAID.format(
                            test=action.test, bug=action.issue, days=rule.close_days
                        )
                    )
            except CommandError as exc:
                said.append(texts.FLAKY_SELF_FAILED.format(test=action.test, error=exc))
    return said


def _open(
    gh: Issues, bugs: list[tuple[int, str]], action: Action, rule: Thresholds
) -> int:
    """The open `bug` naming the test, adopted and commented; else a new one."""
    found = next((n for n, title in bugs if title_names(action.test, title)), None)
    if found is not None:
        gh.comment_issue(
            found,
            texts.FLAKY_SELF_AGAIN.format(test=action.test, runs=runs(action.failures)),
        )
        return found
    title = texts.FLAKY_SELF_TITLE.format(test=action.test)
    body = texts.FLAKY_SELF_BODY.format(
        test=action.test,
        n=len(action.failures),
        days=rule.days,
        runs=runs(action.failures),
        close_days=rule.close_days,
    )
    issue = gh.open_bug(title, body)
    bugs.append((issue, title))
    return issue


def _forget(db: sqlite3.Connection, project: str, test: str) -> None:
    with transaction(db):
        db.execute(
            "DELETE FROM flaky_bug WHERE project = ? AND test = ?", (project, test)
        )


def _record(
    db: sqlite3.Connection,
    project: str,
    test: str,
    issue: int,
    now: float,
    said_until: float,
    *,
    insert: bool = False,
) -> None:
    with transaction(db):
        if insert:
            db.execute(
                "INSERT INTO flaky_bug (project, test, issue, opened_at, said_until)"
                " VALUES (?, ?, ?, ?, ?)",
                (project, test, issue, int(now), int(said_until)),
            )
        else:
            db.execute(
                "UPDATE flaky_bug SET said_until = ? WHERE project = ? AND test = ?",
                (int(said_until), project, test),
            )


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    """`recorder.transaction`'s twin: importing `recorder` would bring the
    whole snapshot along."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
