"""A machine's red in a light lane is re-run once, without waking anyone (#1792).

The merge queue already does it (#1715): when no failed job names a failed
test — a lost runner, a killed process, no log at all — its failed jobs are
re-run once, the worker left asleep. A pull request parked on its CI outside
the queue, or one no worker of tracerworks carries, used to wake its worker
(or wait for the operator) on the same red (09/10; #1784 on 24/10).

So: a red over (nothing running), on a head not re-run yet, no conflict,
whose every failed check is a job whose log names no failed test, is a
MACHINE'S RED; its failed runs are re-run once for that head (table
`machine_rerun`, what `status` says). A second red on the same head, of a
machine or not, goes on as before: the parked worker is resumed (told it
was re-run once already), the pull request without a worker stays red for
its author. A check that is no job (a status), and its log, are read as a
test's red: never re-run blind.

A failed job's log GitHub does not give yet (none, empty, a 404) is
UNKNOWN, not « no test named » (#1919): nothing is decided, its logs are
read again at the next pass; still none `LOG_WAIT_S` after the end of the
job, it is a machine's red. Read within the minute of the end, the reds of
#1852, #1885 and #1886 — each naming its test — were re-run as a machine's.

A CODE RED IS NEVER A MACHINE'S (#2080, 29/10: #2065's `checks` red on a
mypy error, rerun by the queue at 18:51, then again by this re-run at 18:58,
the worker woken at 19:07). A failed job whose failed step — GitHub's jobs
API — is one of `[merge_queue] code_steps` (mypy, ruff, lint, types, the
front's build…) is a code red, as a failed test is: the worker at once,
nothing re-run. A job with no failed step, a lost runner, a process killed
(exit code 137, even on such a step) stays a machine's; a step GitHub does
not give is not decided — the next pass asks again.

ONE RE-RUN OF A MACHINE'S RED PER HEAD, IN ALL: the queue's (the stay's
`machine_rerun_at` on its tested head) and this one (`machine_rerun`) are
read together — one done, the other is not. The queue's re-run of a short
test's red (`FLAKY`, #2130) is written here too, with its tests: the same
one re-run per head.

`status` says a re-run REQUESTED while GitHub shows no second attempt of
the head, RE-RUN once it does (#1919: #1852 said « re-run » for a run
GitHub never re-ran).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import inf
from typing import TYPE_CHECKING, NamedTuple

from tracerworks import texts
from tracerworks.cleanup import Checks
from tracerworks.process import CommandError
from tracerworks.pytest_log import failed_tests
from tracerworks.settings import Settings

if TYPE_CHECKING:
    from tracerworks.ci_revival import Open

#: Who the pull request belonged to when its machine's red was re-run; or
#: its red read as a test's, once: never re-run, its logs not read again.
PARKED, NO_WORKER, TEST_RED = "parked", "no_worker", "test_red"
#: A short test's red the merge queue re-ran once, its tests kept for the
#: bugs a green re-run opens (#2130, spec #2127).
FLAKY = "flaky"

#: How long `status` says a re-run.
SHOWN_SECONDS = 12 * 60 * 60

#: How long after the end of its job a log GitHub does not give is waited
#: for (#1919): it took more than a minute on 25-27/10.
LOG_WAIT_S = 5 * 60

Log = Callable[[int], str | None]  # a job's log, None when GitHub gives none
#: A red's verdict: a machine's (True), a test's (False), or not known yet
#: (None: a log GitHub does not give yet — read again at the next pass).
Verdict = bool | None


def verdict(logs: Iterable[str | None], ended_at: float | None, now: float) -> Verdict:
    """The failed jobs' logs: one naming a failed test, a test's red; one
    not given yet, unknown until `LOG_WAIT_S` after the end of the jobs
    (`ended_at`; None: long over); else a machine's."""
    read = list(logs)
    if any(failed_tests(log or "") for log in read):
        return False
    missing = any(log is None or not log.strip() for log in read)
    if missing and ended_at is not None and now - ended_at < LOG_WAIT_S:
        return None
    return True


#: A failed job's failed step, from GitHub's jobs API; None: none failed,
#: or GitHub does not say.
Step = Callable[[int], str | None]


def code_step(name: str | None, code_steps: Collection[str]) -> bool:
    """A step of the code's (#2080): its name starts with one listed."""
    if not name:
        return False
    folded = name.strip().casefold()
    return any(folded.startswith(step.strip().casefold()) for step in code_steps)


#: A process the kernel killed (out of memory), in a job's log: a machine's.
_KILLED = re.compile(r"exit code 137\b|^\S*\s*Killed\s*$", re.MULTILINE)


def code_red(
    jobs: Iterable[int],
    step: Step,
    code_steps: Collection[str],
    log: Log | None = None,
) -> str | None:
    """The first failed job's step of the code's (#2080), None: none —
    never asked without a list. A job on such a step whose process was
    killed (`log`: exit code 137) stays a machine's: mypy or the front's
    build killed for want of memory proves nothing of the code."""
    if not code_steps:
        return None
    for job in jobs:
        name = step(job)
        if not code_step(name, code_steps):
            continue
        if log is not None and _KILLED.search(log(job) or ""):
            continue
        return name
    return None


def is_machine_red(checks: Checks, log: Log, now: float) -> Verdict:
    """Red and over, and no failed job's log names a failed test; None
    while a log is not given yet (`verdict`)."""
    if not checks.failed or checks.running:
        return False
    if any(name not in checks.failed_jobs for name in checks.failed):
        return False  # a status, no job: its red is read as a test's
    jobs = checks.failed_jobs.values()
    return verdict((log(job) for job in jobs), checks.failed_at, now)


def code_red_of(
    checks: Checks, step: Step, code_steps: Collection[str], log: Log | None = None
) -> str | None:
    """A red over whose failed jobs include one on a step of the code's."""
    if not checks.failed or checks.running:
        return None
    return code_red(checks.failed_jobs.values(), step, code_steps, log)


def record_code(
    db: sqlite3.Connection, project: str, pull: int, head: str, now: float, step: str
) -> None:
    """A code red, once per head (#2080): its test's red recorded too, so
    its logs are never read again nor re-run."""
    db.execute(
        "INSERT OR IGNORE INTO code_red (project, pull, head, at, step)"
        " VALUES (?, ?, ?, ?, ?)",
        (project, pull, head, int(now), step),
    )
    record(db, project, pull, head, now, TEST_RED)


def decided(db: sqlite3.Connection, project: str, pull: int, head: str) -> str | None:
    """What was decided for this head: re-run (by whom), a test's red, None."""
    row = db.execute(
        "SELECT by FROM machine_rerun WHERE project = ? AND pull = ? AND head = ?",
        (project, pull, head),
    ).fetchone()
    return None if row is None else str(row[0])


def rerun_for(db: sqlite3.Connection, project: str, pull: int, head: str) -> bool:
    """This head's one re-run is spent: a light lane's, or the queue's for a
    short test's red (#2130)."""
    return decided(db, project, pull, head) in (PARKED, NO_WORKER, FLAKY)


def record_flaky(
    db: sqlite3.Connection,
    project: str,
    pull: int,
    head: str,
    now: float,
    tests: Collection[str],
) -> None:
    """The queue re-ran a short test's red of this head (#2130): its tests
    kept. Over a test's red read before — the light lane's, then re-run by
    hand to green — never over a re-run: the caller asked `rerun_for`."""
    db.execute(
        "INSERT INTO machine_rerun (project, pull, head, at, by, tests)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (project, pull, head) DO UPDATE"
        " SET at = excluded.at, by = excluded.by, tests = excluded.tests",
        (project, pull, head, int(now), FLAKY, "\n".join(sorted(tests))),
    )


def flaky_tests(
    db: sqlite3.Connection, project: str, pull: int, head: str
) -> list[str]:
    """The tests of this head's short test's red the queue re-ran; none
    when it re-ran none."""
    row = db.execute(
        "SELECT tests FROM machine_rerun WHERE project = ? AND pull = ?"
        " AND head = ? AND by = ?",
        (project, pull, head, FLAKY),
    ).fetchone()
    return [] if row is None or not row[0] else str(row[0]).split("\n")


def queue_reran(db: sqlite3.Connection, project: str, pull: int, head: str) -> bool:
    """The merge queue re-ran a machine's red of this head (#2080): a stay
    of the pull request tested it, its `machine_rerun_at` set."""
    row = db.execute(
        "SELECT 1 FROM merge_queue WHERE project = ? AND pull = ?"
        " AND tested_head = ? AND machine_rerun_at IS NOT NULL",
        (project, pull, head),
    ).fetchone()
    return row is not None


class FailedRun(NamedTuple):
    """A failed run of a head, and when its latest attempt started (None:
    GitHub does not say)."""

    id: int
    started_at: float | None


#: GitHub's clock and tracerworks's: an attempt started this long before a
#: test's start is still that test's (`merge_queue.RUN_SKEW_S`).
QUEUE_SKEW_S = 120


def in_queue_test(
    db: sqlite3.Connection, project: str, pull: int, run: FailedRun
) -> bool:
    """`run`'s latest attempt started while a stay of `pull` was in test —
    under the queue's testing label (#2110): the queue's, never rerun outside
    it, by `parking`, here, or the operator. Its id says nothing: the queue
    reruns a light lane's own run under its label (#1844). A start GitHub
    does not give: not the queue's."""
    if run.started_at is None:
        return False
    windows = db.execute(
        "SELECT started_at, left_at FROM merge_queue WHERE project = ? AND pull = ?"
        " AND started_at IS NOT NULL",
        (project, pull),
    ).fetchall()
    return any(
        start - QUEUE_SKEW_S <= run.started_at <= (left if left is not None else inf)
        for start, left in windows
    )


def record(
    db: sqlite3.Connection, project: str, pull: int, head: str, now: float, by: str
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO machine_rerun (project, pull, head, at, by)"
        " VALUES (?, ?, ?, ?, ?)",
        (project, pull, head, int(now), by),
    )


def rerun_times(db: sqlite3.Connection, project: str) -> dict[int, int]:
    """When each pull request's last machine's red was re-run (#2057): a red
    ended before then is the one being re-run."""
    rows = db.execute(
        "SELECT pull, MAX(at) FROM machine_rerun WHERE project = ? AND by IN (?, ?)"
        " GROUP BY pull",
        (project, PARKED, NO_WORKER),
    ).fetchall()
    return {int(pull): int(at) for pull, at in rows}


@dataclass(frozen=True)
class Shown:
    """A re-run of the last hours, as `status` and the workshop say it."""

    pull: int
    text: str


#: The attempt GitHub shows of a head's runs re-run since a time (1: none
#: yet); None: unread.
Attempt = Callable[[str, float], "int | None"]


def recent(
    db: sqlite3.Connection, project: str, now: float, attempt: Attempt
) -> list[Shown]:
    """The re-runs of the last hours: requested while GitHub shows no
    second attempt of the head (`attempt`), re-run once it does; then the
    code reds, never re-run (#2080)."""
    rows = db.execute(
        "SELECT pull, head, at, by, tests FROM machine_rerun WHERE project = ?"
        " AND at >= ? AND by != ? ORDER BY at",
        (project, int(now) - SHOWN_SECONDS, TEST_RED),
    ).fetchall()
    shown = []
    for pull, head, at, by, tests in rows:
        seen = attempt(str(head), int(at))
        said = {
            "pull": pull,
            "head": str(head)[:7],
            "when": datetime.fromtimestamp(int(at)).strftime("%d/%m %H:%M"),
            "by": texts.MACHINE_RERUN_BY[str(by)],
            "attempt": seen,
        }
        if by == FLAKY:  # the queue's, for a short test's red (#2130)
            n = len(str(tests or "").split("\n"))
            state = texts.FLAKY_RERUN_SEEN.get(seen, texts.FLAKY_RERUN_ATTEMPT)
            text = texts.FLAKY_RERUN_STATUS.format(
                **said, n=n, seen=state.format(attempt=seen)
            )
        elif seen is None:
            text = texts.MACHINE_RERUN_UNREAD.format(**said)
        elif seen < 2:
            text = texts.MACHINE_RERUN_ASKED.format(**said)
        else:
            text = texts.MACHINE_RERUN_STATUS.format(**said)
        shown.append(Shown(int(pull), text))
    for pull, head, at, step in db.execute(
        "SELECT pull, head, at, step FROM code_red WHERE project = ? AND at >= ?"
        " ORDER BY at",
        (project, int(now) - SHOWN_SECONDS),
    ).fetchall():
        when = datetime.fromtimestamp(int(at)).strftime("%d/%m %H:%M")
        text = texts.CODE_RED_STATUS.format(
            pull=pull, head=str(head)[:7], when=when, step=step
        )
        shown.append(Shown(int(pull), text))
    return shown


FailedRuns = Callable[[str], "list[FailedRun] | None"]  # None: a run not over
Rerun = Callable[[Sequence[int]], None]  # raises `CommandError`


def tend_unworked(
    db: sqlite3.Connection,
    settings: Settings,
    pulls: Sequence[Open],
    now: float,
    *,
    windows: Collection[int],
    parked: Collection[int],
    log: Log,
    failed_runs: FailedRuns,
    rerun: Rerun,
    step: Step | None = None,
    memory_short: int | None = None,
) -> list[str]:
    """The pull requests no worker of tracerworks carries (#1792): a machine's
    red re-run once per head, said. Its worker's in a window or parked:
    not here — the parking's resume reads them. The memory short (its MB,
    #2096): the re-run waits, said, not recorded."""
    project, testing = settings.github_repo, settings.merge_queue.testing_label
    said: list[str] = []
    for pull in pulls:
        parsed = settings.conventions.parse_branch(pull.branch)
        spec = parsed[0] if parsed else None
        if (
            pull.draft
            or pull.author not in settings.trusted_logins
            or pull.mergeable == "CONFLICTING"
            or testing in pull.labels  # the queue's own rule
            or (spec is not None and (spec in windows or spec in parked))
            or not pull.checks.failed
            or pull.checks.running
            or decided(db, project, pull.number, pull.head) is not None
        ):
            continue
        try:
            code = None
            if step is not None:
                code_steps = settings.merge_queue.code_steps
                code = code_red_of(pull.checks, step, code_steps, log)
            if code is not None:
                record_code(db, project, pull.number, pull.head, now, code)
                said.append(texts.CODE_RED.format(pull=pull.number, step=code))
                continue
            if queue_reran(db, project, pull.number, pull.head):
                record(db, project, pull.number, pull.head, now, TEST_RED)
                continue  # the queue's re-run was the one (#2080)
            machine = is_machine_red(pull.checks, log, now)
            if machine is None:
                continue  # a log not given yet: read again at the next pass
            if not machine:
                record(db, project, pull.number, pull.head, now, TEST_RED)
                continue
            runs = failed_runs(pull.head)
            if runs is None:
                continue  # a run not over yet: the next pass
            # call time: merge_queue imports this module
            from tracerworks.merge_queue import cancelled_runs

            neutral = cancelled_runs(db, project, pull.number)
            if runs and all(r.id in neutral for r in runs):
                continue  # cancelled by the queue it waits in (#2109): no red
            runs = [r for r in runs if r.id not in neutral]
            queue = [r for r in runs if in_queue_test(db, project, pull.number, r)]
            if runs and len(queue) == len(runs):  # the queue's (#2110)
                record(db, project, pull.number, pull.head, now, TEST_RED)
                said.append(
                    texts.MACHINE_QUEUE_RUN.format(
                        pull=pull.number, runs=", ".join(str(r.id) for r in runs)
                    )
                )
                continue
            ids = [r.id for r in runs if r not in queue]
            if not ids:  # no failed run to re-run: decided, logs not read again
                record(db, project, pull.number, pull.head, now, TEST_RED)
                continue
            if memory_short is not None:
                said.append(
                    texts.UNWORKED_RERUN_WAITS_MEMORY.format(
                        pull=pull.number, mb=memory_short
                    )
                )
                continue
            rerun(ids)
        except CommandError as exc:
            said.append(texts.MACHINE_RERUN_FAILED.format(pull=pull.number, error=exc))
            continue
        record(db, project, pull.number, pull.head, now, NO_WORKER)
        said.append(
            texts.MACHINE_RERUN_NO_WORKER.format(
                pull=pull.number,
                sha=pull.head[:9],
                runs=", ".join(map(str, ids)),
            )
        )
    return said
