"""A machine's red in a light lane is re-run once, without waking anyone (#1792).

No failed job names a failed test — a lost runner (no log), a killed
process (`Killed`, an `oom-kill`) — and the parked worker is not woken: its
failed runs are re-run once for that head, recorded and said. Red again on
that head: its worker is resumed, told it was re-run once. A red that names
a test resumes it at once, as before. A pull request without a worker gets
the same one re-run.

A log GitHub does not give yet is unknown, read again at the next pass, a
machine's red only `LOG_WAIT_S` after the job (#1919, the reds of #1852,
#1885 and #1886); `status` says a re-run requested until GitHub shows it.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FakeRunner, answer_job_log, recorded_settings
from tests.tracerworks.test_clean import HEAD, answer_state
from tests.tracerworks.test_parking import (
    RED_WITH_JOB,
    records,
    resume_world,
    resumed_argv,
)
from tests.tracerworks.test_rerun import RERUN_77, RUNS, answer_runs, reruns
from tracerworks import ci_revival, machine_red, parking, store
from tracerworks.cleanup import Checks
from tracerworks.cli import EXIT_OK, run
from tracerworks.machine_red import FailedRun
from tracerworks.settings import Settings

KILLED = (
    "2026-10-24T19:16:00.0000000Z tests/integration/test_widgets.py ........\n"
    "2026-10-24T19:16:02.0000000Z Killed\n"
    "2026-10-24T19:16:02.1000000Z ##[error]Process completed with exit code 137.\n"
)
TEST_RED = (
    "2026-10-24T19:16:00.0000000Z FAILED tests/test_widgets.py::test_reply - assert 1\n"
    "2026-10-24T19:16:01.0000000Z 1 failed, 40 passed\n"
)
# The red of #1973 (run 40730728470, job 122972183909, 28/10): coloured, as
# GitHub's logs nearly always are — `gh` 2.101 refused it, read as no log.
COLORED_TEST_RED = (
    "2026-10-28T17:19:40.0000000Z \x1b[36;1muv run pytest -m browser\x1b[0m\n"
    "2026-10-28T17:19:50.0000000Z \x1b[31mFAILED\x1b[0m"
    " tests/integration/test_browser_widget_queue.py::test_a_user_sorts_their_list"
    " - \x1b[31mAssertionError\x1b[0m\n"
    "2026-10-28T17:19:51.0000000Z \x1b[31m1 failed\x1b[0m, 12 passed\n"
    "2026-10-28T17:19:52.0000000Z ##[error]Process completed with exit code 1.\n"
)
LOST = None  # « The self-hosted runner lost communication »: GitHub gives no log


def red(*names: str, jobs: dict[str, int] | None = None) -> Checks:
    return Checks([], list(names), dict.fromkeys(names, 4242) if jobs is None else jobs)


def at(stamp: str) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=UTC).timestamp()


# The real reds of 25-27/10 (#1919), each naming its test, each read within
# the minute of the end of its job — when GitHub did not give its log yet.
PR_1852 = (  # run 40227210103: `tests (pytest)` ended 19:50:35, read 19:51
    "2026-10-25T19:50:28.8865452Z FAILED tests/test_integration_budgets.py"
    "::test_the_verb_is_red_on_a_module_above_its_budget_and_green_with_its_reason"
    " - KeyError: 'tools/test_impact/night.toml'\n"
)
PR_1885 = (  # run 40414218274: the last tranche ended 23:21:01, re-run 23:21:57
    "2026-10-26T23:08:13.9196654Z ERROR    app.logistics_send:logistics_send.py:195"
    " logistics resend: the store's mail text is not set — nothing leaves\n"
    "2026-10-26T23:08:13.9214723Z FAILED tests/integration/test_logistics_resend.py"
    "::test_after_the_mail_the_next_sync_writes_sent - AssertionError: assert\n"
)
PR_1886 = (  # run 40450647645 (the queue): ended 05:06:56, re-run 05:08:10
    "2026-10-27T05:06:14.7842263Z ERROR tests/integration/test_storage_bascule.py"
    "::test_rehearsed_then_applied_an_old_attachment_opens_on_s3_then_comes_back"
    " - app.bucket_store.BucketStoreError: create rail-0a0a59a6: ConnectError\n"
)
REAL = [
    (PR_1852, at("2026-10-25T19:50:35"), at("2026-10-25T19:51:00")),
    (PR_1885, at("2026-10-26T23:21:01"), at("2026-10-26T23:21:57")),
    (PR_1886, at("2026-10-27T05:06:56"), at("2026-10-27T05:08:10")),
]


@pytest.mark.parametrize(("log", "ended", "read"), REAL, ids=["1852", "1885", "1886"])
def test_a_real_red_read_before_github_gives_its_log_is_unknown_then_a_test_s(
    log: str, ended: float, read: float
) -> None:
    assert machine_red.verdict([None], ended, read) is None  # read again, not re-run
    assert machine_red.verdict([""], ended, read) is None  # an empty answer alike
    later = read + 60
    assert machine_red.verdict([log], ended, later) is False  # given: a test's
    # one log naming a test is enough, whatever the others
    assert machine_red.verdict([None, log], ended, read) is False


@pytest.mark.parametrize(
    ("log", "machine"),
    [(LOST, True), (KILLED, True), ("", True), (TEST_RED, False)],
    ids=["lost-runner", "killed", "empty-log", "a-test-s-red"],
)
def test_a_red_is_a_machine_s_when_no_log_names_a_test(
    log: str | None, machine: bool
) -> None:
    """Long over (or its end unknown): no log is a machine's red."""
    checks = red("integration (1/2)")
    assert machine_red.is_machine_red(checks, lambda job: log, 1_790_000_000) is machine
    ended = replace(checks, failed_at=1_790_000_000 - machine_red.LOG_WAIT_S)
    assert machine_red.is_machine_red(ended, lambda job: log, 1_790_000_000) is machine


def test_a_log_is_waited_for_until_the_bound_then_the_red_is_a_machine_s() -> None:
    ended = 1_790_000_000.0
    bound = ended + machine_red.LOG_WAIT_S
    assert machine_red.verdict([LOST], ended, bound - 1) is None
    assert machine_red.verdict([LOST], ended, bound) is True
    # a log given that names nothing (killed) needs no wait
    assert machine_red.verdict([KILLED], ended, ended + 1) is True


def test_a_check_that_is_no_job_or_still_running_is_never_a_machine_s() -> None:
    now = 1_790_000_000
    assert not machine_red.is_machine_red(red("deploy", jobs={}), lambda j: None, now)
    running = replace(red("tests"), running=["browser"])
    assert not machine_red.is_machine_red(running, lambda job: None, now)


# --- a parked worker -------------------------------------------------------------


def answer_log(fake: FakeRunner, log: str | None) -> None:
    if log is None:
        answer_job_log(fake, "owner/repo", 4242, returncode=1, stdout="HTTP 404")
    else:
        answer_job_log(fake, "owner/repo", 4242, stdout=log)


def machine_rows(env_settings: Settings) -> list[tuple[int, str, str]]:
    with closing(store.connect(env_settings.history_db)) as db:
        return [
            (int(p), str(h), str(b))
            for p, h, b in db.execute("SELECT pull, head, by FROM machine_rerun")
        ]


@pytest.mark.parametrize("log", [LOST, KILLED], ids=["lost-runner", "killed"])
def test_a_machine_s_red_is_re_run_once_without_waking_the_worker(
    tmp_path: Path, launched: list[tuple[int, str, str]], log: str | None
) -> None:
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, log)
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [RERUN_77]
    assert resumed_argv(fake) == [] and launched == []  # the pass's action
    assert records(env)[520].rerun_for == HEAD
    assert machine_rows(env.settings) == [(720, HEAD, "parked")]
    assert (
        f"#520: PR #720 red without a failed test (a machine's) — failed runs of"
        f" {HEAD[:9]} re-run once, its worker left asleep (77)" in out.getvalue()
    )

    # red again on that head, the re-run settled: its worker is resumed, told
    parking.mark_rerun(env.settings, 520, HEAD, 0)
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [RERUN_77]  # never a second re-run
    assert len(resumed_argv(fake)) == 1
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "ne nommait aucun test" in reason
    assert "relancé une fois ses jobs en échec sans te réveiller" in reason


LOW_MEMINFO = Path(__file__).parents[1] / "fixtures" / "tracerworks" / "meminfo-low.txt"


def test_a_machine_s_red_waits_for_memory_before_its_re_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#2096: on 29/10, 39 processes of the CI were killed, each re-run killed
    again. Under the alert threshold, the re-run waits — neither done nor
    counted as the head's one re-run — and goes once the memory is back."""
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, LOST)
    answer_runs(fake, RUNS)
    roomy = env.settings
    env.settings = replace(roomy, meminfo=LOW_MEMINFO)

    run(env, ["start-next"])  # then refuses a new worker: the memory guard's
    assert reruns(fake) == [] and resumed_argv(fake) == []
    assert machine_rows(env.settings) == []
    assert "#520: PR #720 machine red, rerun waits for memory (1005 MB available)" in (
        out.getvalue()
    )

    env.settings = roomy
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [RERUN_77]
    assert machine_rows(env.settings) == [(720, HEAD, "parked")]


@pytest.mark.parametrize("log", [TEST_RED, COLORED_TEST_RED], ids=["plain", "coloured"])
def test_a_red_that_names_a_test_resumes_the_worker_at_once(
    tmp_path: Path, launched: list[tuple[int, str, str]], log: str
) -> None:
    """Coloured (#1999): `gh` 2.101 gives it only to a read with the flag."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, log)
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert len(resumed_argv(fake)) == 1
    assert machine_rows(env.settings) == [(720, HEAD, "test_red")]
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "ne nommait aucun test" not in reason


def ended(seconds_ago: float, now: float) -> list[dict[str, str]]:
    """`RED_WITH_JOB`, its failed job ended `seconds_ago`."""
    stamp = datetime.fromtimestamp(now - seconds_ago, UTC).isoformat()
    return [
        {**check, "completedAt": stamp} if check.get("name") == "browser" else check
        for check in RED_WITH_JOB
    ]


def test_a_log_not_given_yet_neither_re_runs_nor_resumes_then_is_read(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#1852: read 25 s after the job, the log was not given yet — it was
    re-run as a machine's red. Now: nothing decided, read again; given at
    the next pass, it names a test — its worker resumed, nothing re-run."""
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, ended(25, env.now()))
    answer_log(fake, LOST)
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [] and resumed_argv(fake) == []
    assert machine_rows(env.settings) == []  # nothing decided: read again
    assert "#520: PR #720 red, a failed job's log not given by GitHub yet" in (
        out.getvalue()
    )

    answer_log(fake, PR_1852)  # GitHub gives it at the next pass
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert len(resumed_argv(fake)) == 1
    assert machine_rows(env.settings) == [(720, HEAD, "test_red")]


def test_a_log_still_absent_after_the_bound_is_a_machine_s_red_re_run_once(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, ended(machine_red.LOG_WAIT_S, env.now()))
    answer_log(fake, LOST)
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [RERUN_77] and resumed_argv(fake) == []
    assert machine_rows(env.settings) == [(720, HEAD, "parked")]


def test_a_run_cancelled_by_its_red_job_is_the_one_re_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#1852's run concluded `cancelled` (a red job cancels the rest): it
    was never re-run while `status` said it was."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, KILLED)
    answer_runs(
        fake, [{"databaseId": 77, "status": "completed", "conclusion": "cancelled"}]
    )

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [RERUN_77]


def test_a_machine_s_red_with_no_run_to_re_run_is_never_said_re_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, KILLED)
    answer_runs(
        fake, [{"databaseId": 78, "status": "completed", "conclusion": "success"}]
    )

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert machine_rows(env.settings) == [(720, HEAD, "test_red")]
    assert "no failed run of" in out.getvalue()
    assert "re-run once" not in out.getvalue()
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1  # its worker, at the next pass


def test_a_conflict_is_never_re_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB, mergeable="CONFLICTING")
    answer_log(fake, LOST)
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [] and len(resumed_argv(fake)) == 1


# --- a pull request without a worker ---------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return recorded_settings(tmp_path)


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(settings.history_db)) as base:
        yield base


def unworked(**over: object) -> ci_revival.Open:
    found = ci_revival.Open(
        number=1784, head=HEAD, branch="hotfix-runner", draft=False,
        author="operator-login", mergeable="MERGEABLE", labels=frozenset(),
        nothing=False, checks=red("integration (1/2)"),
    )  # fmt: skip
    return replace(found, **over)  # type: ignore[arg-type]


def tend(
    db: sqlite3.Connection,
    settings: Settings,
    pull: ci_revival.Open,
    log: str | None,
    rerun: list[list[int]],
    *,
    windows: tuple[int, ...] = (),
) -> list[str]:
    return machine_red.tend_unworked(
        db, settings, [pull], 1_790_000_000,
        windows=windows, parked=(), log=lambda job: log,
        failed_runs=lambda sha: [FailedRun(77, None)],
        rerun=lambda runs: rerun.append(list(runs)),
    )  # fmt: skip


def test_without_a_worker_a_machine_s_red_is_re_run_once(
    db: sqlite3.Connection, settings: Settings
) -> None:
    done: list[list[int]] = []
    said = tend(db, settings, unworked(), LOST, done)
    assert done == [[77]]
    assert said == [
        f"PR #1784: red without a failed test (a machine's), no worker — failed runs"
        f" of {HEAD[:9]} re-run once (77)"
    ]
    assert tend(db, settings, unworked(), LOST, done) == []  # once per head
    assert done == [[77]]
    [shown] = machine_red.recent(
        db, settings.github_repo, 1_790_000_000, lambda h, at: 2
    )
    assert shown.pull == 1784
    assert shown.text.startswith("PR #1784: a machine's red re-run once")
    assert "no worker" in shown.text


def test_without_a_worker_a_machine_s_red_waits_for_memory(
    db: sqlite3.Connection, settings: Settings
) -> None:
    done: list[list[int]] = []
    said = machine_red.tend_unworked(
        db, settings, [unworked()], 1_790_000_000,
        windows=(), parked=(), log=lambda job: LOST,
        failed_runs=lambda sha: [FailedRun(77, None)],
        rerun=lambda runs: done.append(list(runs)),
        memory_short=1005,
    )  # fmt: skip
    assert done == []
    assert said == ["PR #1784: machine red, rerun waits for memory (1005 MB available)"]
    assert tend(db, settings, unworked(), LOST, done) != []  # back: re-run, once
    assert done == [[77]]


def test_without_a_worker_a_test_s_red_is_read_once_never_re_run(
    db: sqlite3.Connection, settings: Settings
) -> None:
    done: list[list[int]] = []
    reads: list[int] = []

    def log(job: int) -> str:
        reads.append(job)
        return TEST_RED

    for _ in range(3):
        machine_red.tend_unworked(
            db, settings, [unworked()], 1_790_000_000,
            windows=(), parked=(), log=log,
            failed_runs=lambda sha: [FailedRun(77, None)],
        rerun=lambda runs: done.append(list(runs)),
        )  # fmt: skip
    assert done == [] and reads == [4242]
    assert machine_red.recent(db, settings.github_repo, 1_790_000_000, never) == []


@pytest.mark.parametrize(
    "over",
    [
        {"branch": "agent/1784-x"},  # its worker in its window: the parking's
        {"author": "someone-else"},
        {"draft": True},
        {"mergeable": "CONFLICTING"},
        {"labels": frozenset({"merge-queue-testing"})},  # the queue's own rule
    ],
    ids=["worker-in-its-window", "untrusted", "draft", "conflict", "queue-test"],
)
def test_outside_the_rule_nothing_is_re_run(
    db: sqlite3.Connection, settings: Settings, over: dict[str, object]
) -> None:
    done: list[list[int]] = []
    assert tend(db, settings, unworked(**over), LOST, done, windows=(1784,)) == []
    assert done == []


def never(head: str, since: float) -> int | None:
    raise AssertionError("GitHub asked for a re-run never made")


#: The machine's re-run requested in these tests (`machine_red.record`).
ASKED = 1_790_000_000


def answer_attempt(
    fake: FakeRunner, attempts: list[int] | None, started: float = ASKED + 10
) -> None:
    """The head's runs on GitHub: their attempts, each (re)started `started`."""
    argv = (
        "gh", "run", "list", "--repo", "owner/repo", "--commit", HEAD,
        "--json", "attempt,startedAt", "--limit", "100",
    )  # fmt: skip
    stamp = datetime.fromtimestamp(started, UTC).isoformat()
    if attempts is None:
        fake.answer(*argv, returncode=1, stdout="HTTP 502")
    else:
        runs = [{"attempt": a, "startedAt": stamp} for a in attempts]
        fake.answer(*argv, stdout=json.dumps(runs))


def test_status_says_requested_until_github_shows_the_re_run(tmp_path: Path) -> None:
    """#1852: `status` said « re-run » at 19:51; GitHub showed no second
    attempt before 20:01. Requested, then re-run — each read on GitHub."""
    from tracerworks import cli
    from tracerworks.cli import Env

    settings = recorded_settings(tmp_path)
    with closing(store.connect(settings.history_db)) as db:
        machine_red.record(
            db, settings.github_repo, 1784, HEAD, 1_790_000_000, "parked"
        )
    fake = FakeRunner()
    env = Env(
        settings, runner=fake, out=io.StringIO(), err=io.StringIO(),
        now=lambda: 1_790_000_000 + 60,
    )  # fmt: skip

    answer_attempt(fake, [1, 1])
    [line] = cli.conflict_lines(env)
    assert "re-run requested" in line and "no second attempt yet" in line
    assert "its worker parked" in line and "re-run once" not in line

    # a second attempt from before the request: an older re-run, not this one
    answer_attempt(fake, [1, 2], started=ASKED - 3600)
    [line] = cli.conflict_lines(env)
    assert "re-run requested" in line and "re-run once" not in line

    answer_attempt(fake, None)  # GitHub unread: never said re-run
    [line] = cli.conflict_lines(env)
    assert "re-run requested" in line and "unread" in line

    answer_attempt(fake, [1, 2])
    [line] = cli.conflict_lines(env)
    assert line.startswith("PR #1784: a machine's red re-run once")
    assert "attempt 2 on GitHub" in line and "its worker parked" in line


def test_status_says_a_machine_s_red_waits_for_memory(tmp_path: Path) -> None:
    from tracerworks import cli
    from tracerworks.cli import Env

    settings = replace(recorded_settings(tmp_path), meminfo=LOW_MEMINFO)
    env = Env(settings, runner=FakeRunner(), out=io.StringIO(), err=io.StringIO())
    assert cli.conflict_lines(env) == [
        "memory low (1005 MB available, under the alert threshold): any machine"
        " red, rerun waits for memory"
    ]
    env.settings = recorded_settings(tmp_path)
    assert cli.conflict_lines(env) == []


def test_the_workshop_says_the_re_run_as_status_does(tmp_path: Path) -> None:
    from tracerworks import snapshot

    settings = recorded_settings(tmp_path)
    fake = FakeRunner()
    assert snapshot._machine_reruns(fake, settings, 1_790_000_060) == []
    with closing(store.connect(settings.history_db)) as db:
        machine_red.record(
            db, settings.github_repo, 1784, HEAD, 1_790_000_000, "no_worker"
        )
    answer_attempt(fake, [1])
    [shown] = snapshot._machine_reruns(fake, settings, 1_790_000_060) or []
    assert shown["pull"] == 1784 and "re-run requested" in shown["text"]
    answer_attempt(fake, [2])
    [shown] = snapshot._machine_reruns(fake, settings, 1_790_000_060) or []
    assert "re-run once" in shown["text"]


# --- a code red is never a machine's; one re-run per head in all (#2080) --------

CODE_STEPS = ("ruff check", "mypy", "front: build")


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ("mypy", True),
        ("MyPy ", True),
        ("ruff check", True),
        ("front: build, and weigh what is served", True),
        ("pytest (this tranche)", False),
        ("Set up job", False),
        (None, False),
        ("", False),
    ],
)
def test_a_step_of_the_code_s_is_named_by_the_start_of_its_name(
    step: str | None, code: bool
) -> None:
    assert machine_red.code_step(step, CODE_STEPS) is code


def test_a_code_red_needs_a_list_and_one_job_on_a_code_step() -> None:
    steps = {1: "pytest (this tranche)", 2: "mypy"}
    assert machine_red.code_red([1, 2], steps.get, CODE_STEPS) == "mypy"
    assert machine_red.code_red([1], steps.get, CODE_STEPS) is None
    asked: list[int] = []
    assert machine_red.code_red([2], asked.append, ()) is None
    assert asked == [], "no list: GitHub is never asked"


def test_a_code_step_whose_process_was_killed_stays_a_machine_s() -> None:
    """mypy or the front's build killed for want of memory (exit code 137)
    proves nothing of the code."""
    logs = {1: KILLED, 2: "error: Missing named argument\n"}
    steps = {1: "mypy", 2: "mypy"}
    assert machine_red.code_red([1], steps.get, CODE_STEPS, logs.get) is None
    assert machine_red.code_red([1, 2], steps.get, CODE_STEPS, logs.get) == "mypy"


def coded(env_settings: Settings) -> Settings:
    return replace(
        env_settings,
        merge_queue=replace(env_settings.merge_queue, code_steps=CODE_STEPS),
    )


def answer_step(fake: FakeRunner, step: str) -> None:
    fake.answer(
        "gh", "api", "repos/owner/repo/actions/jobs/4242", "--jq",
        '[.steps[]? | select(.conclusion == "failure") | .name][0]', stdout=f"{step}\n",
    )  # fmt: skip


def test_a_mypy_red_of_a_parked_worker_wakes_it_at_once_never_re_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#2065 (29/10): no failed test named, a mypy error — a code red."""
    fake, env, out, err = resume_world(tmp_path)
    env = replace(env, settings=coded(env.settings))
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, LOST)
    answer_step(fake, "mypy")
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert len(resumed_argv(fake)) == 1
    assert machine_rows(env.settings) == [(720, HEAD, "test_red")]
    assert "PR #720: a code red (mypy), not a machine's" in out.getvalue()
    with closing(store.connect(env.settings.history_db)) as db:
        [shown] = machine_red.recent(db, "owner/repo", env.now(), never)
    assert shown.text.startswith("PR #720: a code red") and "mypy" in shown.text


def test_a_red_on_no_code_step_stays_a_machine_s(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    env = replace(env, settings=coded(env.settings))
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, LOST)
    answer_step(fake, "null")  # the runner lost: no step failed
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == [RERUN_77]


def queue_reran(settings: Settings, head: str = HEAD, pull: int = 720) -> None:
    """The queue tested `head` and re-ran its machine's red (#1715)."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state, tested_head,"
            " machine_rerun_at, left_at, outcome, reason) VALUES ('owner/repo', ?, 0,"
            " 'out', ?, 10, 20, 'sent_back', 'the full suite is red again')",
            (pull, head),
        )


def test_a_head_the_queue_re_ran_is_never_re_run_again_its_worker_woken(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#2065: re-run by the queue at 18:51, then by this re-run at 18:58."""
    fake, env, _, err = resume_world(tmp_path)
    queue_reran(env.settings)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, LOST)
    answer_runs(fake, RUNS)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert len(resumed_argv(fake)) == 1


def test_without_a_worker_a_head_the_queue_re_ran_or_a_code_red_is_not_re_run(
    db: sqlite3.Connection, settings: Settings
) -> None:
    done: list[list[int]] = []
    queue_reran(settings, pull=1784)
    assert tend(db, settings, unworked(), LOST, done) == []
    assert done == []

    other = unworked(number=1785)
    said = machine_red.tend_unworked(
        db, coded(settings), [other], 1_790_000_000,
        windows=(), parked=(), log=lambda job: LOST,
        failed_runs=lambda sha: [FailedRun(77, None)],
        rerun=lambda runs: done.append(list(runs)),
        step=lambda job: "ruff check",
    )  # fmt: skip
    assert done == []
    assert said == [
        "PR #1785: a code red (ruff check), not a machine's — nothing re-run"
    ]


# --- a run of the queue's is never rerun outside it (#2110) ----------------- #


def queue_tested(settings: Settings, pull: int) -> None:
    """A stay of `pull` was in test from 5 to 20 (tracerworks's clock), then
    left without its verdict (`gone`)."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state, tested_head,"
            " started_at, left_at, outcome, reason) VALUES ('owner/repo',"
            " ?, 0, 'out', ?, 5, 20, 'gone', 'no longer the queue''s')",
            (pull, HEAD),
        )


def test_a_machine_s_red_on_a_run_of_the_queue_s_wakes_the_worker_never_re_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = resume_world(tmp_path)
    queue_tested(env.settings, 720)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_log(fake, LOST)
    in_test = {**RUNS[0], "startedAt": "1970-01-01T00:00:10Z"}
    answer_runs(fake, [in_test, RUNS[1]])

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert "run 77 is the merge queue's: never rerun outside it" in out.getvalue()
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert reruns(fake) == []
    assert len(resumed_argv(fake)) == 1


def test_without_a_worker_a_run_of_the_queue_s_is_not_re_run(
    db: sqlite3.Connection, settings: Settings
) -> None:
    done: list[list[int]] = []
    queue_tested(settings, 1784)
    said = machine_red.tend_unworked(
        db, settings, [unworked()], 1_790_000_000,
        windows=(), parked=(), log=lambda job: LOST,
        failed_runs=lambda sha: [FailedRun(77, 10.0)],
        rerun=lambda runs: done.append(list(runs)),
    )  # fmt: skip
    assert done == []
    assert said == ["PR #1784: run 77 is the merge queue's — never rerun outside it"]


def test_without_a_worker_a_light_lane_s_run_beside_the_queue_s_is_re_run(
    db: sqlite3.Connection, settings: Settings
) -> None:
    done: list[list[int]] = []
    queue_tested(settings, 1784)
    machine_red.tend_unworked(
        db, settings, [unworked()], 1_790_000_000,
        windows=(), parked=(), log=lambda job: LOST,
        failed_runs=lambda sha: [FailedRun(77, 10.0), FailedRun(79, 600.0)],
        rerun=lambda runs: done.append(list(runs)),
    )  # fmt: skip
    assert done == [[79]]


def test_without_a_worker_a_run_the_queue_cancelled_is_not_re_run(
    db: sqlite3.Connection, settings: Settings
) -> None:
    """Its aggregate reads FAILURE once cancelled (#2109): no machine's red,
    nothing decided for the head either (#2139's review)."""
    with closing(store.connect(settings.history_db)) as base:
        base.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state)"
            " VALUES ('owner/repo', 1784, 0, 'waiting')"
        )
        base.execute(
            "INSERT INTO merge_queue_cancel (project, pull, head, run, attempt, at,"
            " url) VALUES ('owner/repo', 1784, ?, 77, 2, 1, 'u')",
            (HEAD,),
        )
    done: list[list[int]] = []
    said = machine_red.tend_unworked(
        db, settings, [unworked()], 1_790_000_000,
        windows=(), parked=(), log=lambda job: LOST,
        failed_runs=lambda sha: [FailedRun(77, None)],
        rerun=lambda runs: done.append(list(runs)),
    )  # fmt: skip
    assert done == [] and said == []
    assert machine_red.decided(db, settings.github_repo, 1784, HEAD) is None
