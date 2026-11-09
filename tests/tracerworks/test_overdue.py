"""A worker's test that runs on (#2096): on 29/10 a worker's `node --test` grew
for 9 h and its worker waited all along. Read from a /proc laid out as the
VPS's: a worker's scope `tracerworks-worker-<n>-<time>.scope` in `agents.slice`."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from tracerworks.conventions import Conventions
from tracerworks.overdue import Overdue, command_of, overdue, stop

TICKS = 100
PAGE_KB = 4
UPTIME = 2_045_000.0
CONVENTIONS = Conventions("worker-", "agent/", Path("/w"))  # tracerworks-worker-<n>-…
COMMANDS = ("node --test", "pytest", "vitest", "playwright")
AGENTS = "/user.slice/user-1001.slice/user@1001.service/agents.slice"
WORKER = f"{AGENTS}/tracerworks-worker-1958-1793300000.scope"
SESSION = "/user.slice/user-1001.slice/session-4.scope"
CI = "/user.slice/user-1001.slice/user@1001.service/ci.slice/runner-1.service"


def process(
    proc: Path,
    pid: int,
    argv: list[str],
    cgroup: str = WORKER,
    *,
    minutes: float,
    group: int | None = None,
    parent: int = 1,
    rss_mb: int = 100,
) -> None:
    """A /proc/<pid> started `minutes` ago, in process group `group`."""
    here = proc / str(pid)
    here.mkdir()
    start = round((UPTIME - minutes * 60) * TICKS)
    pages = rss_mb * 1024 // PAGE_KB
    # after the name: state ppid pgrp … starttime (20th) vsize rss (22nd)
    fields = (
        ["S", str(parent), str(group or pid)]
        + ["0"] * 16
        + [str(start), "0", str(pages)]
    )
    name = Path(argv[0]).name[:15]
    here.joinpath("stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
    here.joinpath("cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    here.joinpath("cgroup").write_text(f"0::{cgroup}\n")


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    root.joinpath("uptime").write_text(f"{UPTIME} 9000000.00\n")
    root.joinpath("self").mkdir()
    # the worker's session: the scope's first process, older than anything
    process(root, 100, ["claude", "--permission-mode", "auto"], minutes=600)
    return root


def found(proc: Path, max_minutes: int = 30) -> list[Overdue] | None:
    return overdue(
        proc, CONVENTIONS, COMMANDS, max_minutes, ticks=TICKS, page_kb=PAGE_KB
    )


def test_the_9_hour_node_test_of_29_10_is_overdue(proc: Path) -> None:
    process(proc, 200, ["npm", "test"], minutes=556, group=200)
    process(
        proc,
        201,
        ["node", "--test", "src/components/dashboard/StatInfo.test.ts"],
        minutes=556,
        group=200,
        rss_mb=7100,
    )

    assert found(proc) == [
        Overdue(
            spec=1958,
            group=200,
            pids=(200, 201),
            command="node --test",
            minutes=556,
            memory_mb=7200,
        )
    ]


def test_a_young_test_is_left(proc: Path) -> None:
    process(
        proc, 300, ["/w/.venv/bin/python3", "/w/.venv/bin/pytest", "-q"], minutes=29
    )
    assert found(proc) == []


def test_the_limit_is_the_setting(proc: Path) -> None:
    process(proc, 300, ["/w/.venv/bin/python3", "-m", "pytest", "-q"], minutes=29)
    [one] = found(proc, max_minutes=20) or []
    assert (one.command, one.pids, one.group) == ("pytest", (300,), 300)


def test_neither_the_session_nor_a_process_out_of_a_worker_s_scope_is_touched(
    proc: Path,
) -> None:
    # the session's prompt may name a test command: never a word to match
    process(proc, 101, ["claude", "--resume", "x", "lance pytest"], minutes=600)
    process(proc, 400, ["pytest", "-q"], SESSION, minutes=120)
    process(proc, 401, ["pytest", "-q"], CI, minutes=120)
    process(proc, 402, ["pytest", "-q"], f"{AGENTS}/other-1.scope", minutes=120)
    assert found(proc) == []


def test_a_test_in_the_session_s_own_group_is_stopped_with_its_children_alone(
    proc: Path,
) -> None:
    vitest = ["node", "/w/node_modules/.bin/vitest", "run"]
    process(proc, 500, vitest, minutes=45, group=100, parent=100)
    process(proc, 502, ["node", "worker.js"], minutes=44, group=100, parent=500)
    process(proc, 501, ["bash", "-c", "sleep 1"], minutes=45, group=100, parent=100)

    [one] = found(proc) or []
    assert (one.group, one.pids, one.command) == (None, (500, 502), "vitest")
    assert one.memory_mb == 200


def test_a_group_that_leaves_the_scope_is_never_ended_whole(proc: Path) -> None:
    # a nested `systemd-run --scope` moves a process, its group kept
    process(proc, 600, ["pytest", "-q"], minutes=40, group=600)
    process(proc, 601, ["sleep", "999"], CI, minutes=40, group=600)

    [one] = found(proc) or []
    assert (one.group, one.pids) == (None, (600,))


@pytest.mark.parametrize(
    "argv",
    [
        ["grep", "-rn", "pytest", "."],
        ["rg", "vitest", "front"],
        ["bash", "-c", "pytest -q"],  # its child runs it, matched there
        ["claude", "lance pytest"],
    ],
)
def test_an_argument_is_never_a_program(argv: list[str]) -> None:
    assert command_of(argv, COMMANDS) is None


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["/w/.venv/bin/pytest", "-q"], "pytest"),
        (["/w/.venv/bin/python3", "/w/.venv/bin/pytest", "-q"], "pytest"),
        (["python3.12", "-X", "dev", "-m", "pytest"], "pytest"),
        (["node", "--test", "x.test.ts"], "node --test"),
        (["/usr/bin/node", "/w/node_modules/.bin/playwright", "test"], "playwright"),
        (["node", "x.js"], None),
    ],
)
def test_a_program_is_its_first_word_an_interpreter_s_script_or_a_module(
    argv: list[str], command: str | None
) -> None:
    assert command_of(argv, COMMANDS) == command


def test_stop_ends_the_group_then_kills_what_is_left() -> None:
    sent: list[tuple[str, int, int]] = []
    slept: list[float] = []

    def kill(pid: int, sig: int) -> None:
        sent.append(("pid", pid, sig))
        if sig == signal.SIGKILL:
            raise ProcessLookupError  # gone at the SIGTERM: nothing to say

    def killpg(group: int, sig: int) -> None:
        sent.append(("group", group, sig))

    found = [
        Overdue(1958, 200, (201,), "node --test", 556, 7200),
        Overdue(77, None, (500,), "vitest", 45, 300),
    ]
    stop(
        found,
        lambda: found,
        kill=kill,
        killpg=killpg,
        sleep=slept.append,
    )

    assert sent == [
        ("group", 200, signal.SIGTERM),
        ("pid", 500, signal.SIGTERM),
        ("group", 200, signal.SIGKILL),
        ("pid", 500, signal.SIGKILL),
    ]
    assert slept == [10]


def test_only_what_is_still_overdue_gets_the_sigkill() -> None:
    """Between the SIGTERM and the SIGKILL, a pid may be used again: /proc is
    read again, and only what is still overdue — the same spec, command and
    group or pid — is killed."""
    sent: list[tuple[int, int]] = []
    found = [
        Overdue(1958, 200, (201,), "node --test", 556, 7200),
        Overdue(77, None, (500, 502), "vitest", 45, 300),
    ]
    still = [Overdue(77, None, (502,), "vitest", 45, 100)]  # 200 and 500 gone
    stop(
        found,
        lambda: still,
        kill=lambda p, sig: sent.append((p, sig)),
        killpg=lambda g, sig: sent.append((-g, sig)),
        sleep=lambda s: None,
    )
    assert [x for x in sent if x[1] == signal.SIGKILL] == [(502, signal.SIGKILL)]
    sent.clear()
    stop(found, lambda: None, kill=lambda p, s: sent.append((p, s)),
         killpg=lambda g, s: sent.append((-g, s)), sleep=lambda s: None)  # fmt: skip
    assert signal.SIGKILL not in [sig for _, sig in sent], "unreadable: no SIGKILL"


def test_an_unreadable_proc_is_unknown(tmp_path: Path) -> None:
    assert found(tmp_path / "absent") is None


def test_the_relauncher_s_pass_stops_it_records_it_and_tells_its_worker(
    proc: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses
    import io
    import sqlite3

    from tracerworks import cli
    from tracerworks import overdue as module
    from tracerworks.cli import Env

    from .fakes import FakeRunner, recorded_settings

    process(proc, 201, ["node", "--test", "x.test.ts"], minutes=556, rss_mb=7100)
    settings = dataclasses.replace(recorded_settings(tmp_path), proc=proc)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(module, "SIGNAL_GROUP", lambda g, s: sent.append((g, s)))
    told: list[tuple[int, str]] = []
    monkeypatch.setattr(cli, "deliver", lambda env, n, text: told.append((n, text)))
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_CLK_TCK": TICKS}.get(name, PAGE_KB * 1024),
    )
    out = io.StringIO()
    env = Env(
        settings,
        runner=FakeRunner(),
        now=lambda: 1_793_340_000.0,
        sleep=lambda s: None,
        out=out,
        err=io.StringIO(),
    )

    cli._stop_overdue(env)

    assert sent == [(201, signal.SIGTERM), (201, signal.SIGKILL)]
    [(spec, text)] = told
    assert spec == 1958
    assert "`node --test`" in text and "556 min" in text and "7100 Mo" in text
    assert "#1958" in out.getvalue() and "node --test" in out.getvalue()
    with sqlite3.connect(settings.history_db) as db:
        assert db.execute(
            "SELECT spec, pid, command, minutes, memory_mb FROM test_kill"
        ).fetchall() == [(1958, 201, "node --test", 556, 7100)]
