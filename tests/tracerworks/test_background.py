"""A worker at rest that waits on a job of its own in the background (#2175).

31/10: #2131's worker launched a 17-min replay of a CI slice in the
background and went to rest. A park closes the session, the job with it. Its
scope's shells say such a job: kept, up to `[workers] background_max_minutes`.
Read from a /proc laid out as the VPS's (`tests/tracerworks/test_overdue.py`).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.tracerworks.fakes import TMUX, FakeRunner, fixture, target
from tests.tracerworks.test_clean import (
    HEAD,
    RED,
    RUNNING,
    answer_state,
    clean_world,
    waiting_501,
)
from tests.tracerworks.test_clean import cleaned as clean
from tests.tracerworks.test_overdue import (
    AGENTS,
    CONVENTIONS,
    PAGE_KB,
    TICKS,
    UPTIME,
    WORKER,
    process,
)
from tests.tracerworks.test_parking import records, resumed_501_for_red, shells
from tests.tracerworks.test_parking import transcripts as transcripts
from tests.tracerworks.test_start_stop import make_env
from tracerworks import background, parking
from tracerworks.cli import EXIT_OK, run

#: #501's scope, as `clean`'s settings name it (`tracerworks-worker-<n>-<time>`)
SCOPE_501 = f"{AGENTS}/tracerworks-worker-501-1793300000.scope"
#: Claude Code's shell for one command (#2175: read on the VPS, 31/10)
SNAPSHOT_SHELL = ["/bin/bash", "-c", "source /home/dev/.claude/shell-snapshots/x.sh"]


def lay_out(root: Path, session_minutes: float = 600) -> Path:
    root.mkdir()
    root.joinpath("uptime").write_text(f"{UPTIME} 9000000.00\n")
    process(root, 100, ["claude", "--permission-mode", "auto"], minutes=session_minutes)
    return root


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    """#1958's scope (`test_overdue.WORKER`): its session, pid 100."""
    return lay_out(tmp_path / "proc-1958")


def jobs(proc: Path, spec: int = 1958) -> list[float] | None:
    return background.jobs(proc, CONVENTIONS, spec, ticks=TICKS, page_kb=PAGE_KB)


# --- what a background job is ------------------------------------------------------


def test_claude_s_shell_is_a_job_from_its_first_second(proc: Path) -> None:
    """31/10: the worker went to rest 4 s after launching its replay."""
    process(proc, 200, SNAPSHOT_SHELL, minutes=4 / 60, parent=100)
    assert jobs(proc) == [4]


def test_the_31_10_replay_is_a_background_job(proc: Path) -> None:
    process(proc, 200, SNAPSHOT_SHELL, minutes=14, parent=100)
    process(proc, 201, ["uv", "run", "pytest", "-q"], minutes=14, parent=200)
    assert jobs(proc) == [14 * 60]


@pytest.mark.parametrize(
    ("argv", "minutes", "parent", "cgroup"),
    [
        (["npm", "exec", "@acme/mcp-server"], 300, 100, WORKER),  # an MCP server
        (["sh", "-c", "acme-mcp-server"], 300, 150, WORKER),  # its own shell
        (["/bin/sh", "-c", "tracerworks statusline"], 0.2, 100, WORKER),  # a moment
        (SNAPSHOT_SHELL, 30, 100, f"{AGENTS}/tracerworks-worker-1959-1793300000.scope"),
    ],
    ids=["mcp server", "a grandchild shell", "a status line", "another worker's"],
)  # fmt: skip
def test_what_is_no_background_job(
    proc: Path, argv: list[str], minutes: float, parent: int, cgroup: str
) -> None:
    process(proc, 150, ["npm", "exec", "@acme/mcp-server"], minutes=300, parent=100)
    process(proc, 200, argv, cgroup, minutes=minutes, parent=parent)
    assert jobs(proc) == []


def test_without_its_scope_nothing_is_known(proc: Path) -> None:
    """A Mac, a worker launched without a scope, an unreadable /proc: the
    pane's footer decides, as before."""
    assert jobs(proc, spec=1959) is None
    assert jobs(proc.parent / "absent") is None


def test_an_older_scope_of_the_same_spec_is_not_its_session(proc: Path) -> None:
    """31/10: #2131's scope of 03:18 outlived its session until 08:12, beside
    the one of 08:07 — the live session is the newest scope's."""
    old = f"{AGENTS}/tracerworks-worker-1958-1793200000.scope"
    process(proc, 50, ["bash", "-c", "sleep 1d"], old, minutes=900)
    process(proc, 51, SNAPSHOT_SHELL, old, minutes=800, parent=50)
    process(proc, 200, SNAPSHOT_SHELL, minutes=5, parent=100)
    assert jobs(proc) == [5 * 60]


# --- in clean ----------------------------------------------------------------------


def at_rest_on_its_red(
    tmp_path: Path, job_minutes: float | None, pane: str = ""
) -> FakeRunner:
    """#501 at rest, its PR #619 red, resumed for it 20 min ago (past the
    grace); a background job of `job_minutes` in its scope, or none."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=pane or None)
    answer_state(fake, 619, RED)
    resumed_501_for_red(tmp_path, fake, ago=20 * 60)
    root = lay_out(tmp_path / "proc")
    (root / "100" / "cgroup").write_text(f"0::{SCOPE_501}\n")
    if job_minutes is not None:
        process(root, 200, SNAPSHOT_SHELL, SCOPE_501, minutes=job_minutes, parent=100)
    return fake


def test_a_worker_waiting_on_its_replay_is_not_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = at_rest_on_its_red(tmp_path, job_minutes=15)
    code, env, out, _ = clean(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert (
        "#501 kept — PR #619 open, its worker waits on its background job"
        " (15 min, parked past 60 min)" in out
    )
    assert records(env)[501].resumed_for == f"{HEAD}:red"


def test_past_the_cap_it_is_parked_as_before_and_said(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = at_rest_on_its_red(tmp_path, job_minutes=61)
    code, _, out, _ = clean(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert "#501: its background job runs for 61 min, past 60 min" in out


def test_the_cap_is_the_setting(tmp_path: Path, transcripts: Path) -> None:
    fake = at_rest_on_its_red(tmp_path, job_minutes=15)
    env, _, _ = make_env(fake, tmp_path)
    env = dataclasses.replace(
        env,
        settings=dataclasses.replace(env.settings, worker_background_max_minutes=10),
    )
    assert run(env, ["clean"]) == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))


def test_its_scope_read_the_footer_is_not_asked(
    tmp_path: Path, transcripts: Path
) -> None:
    """The scope says no job: a footer « 1 shell » (a stale line) keeps
    nothing — and a job the footer does not name keeps it."""
    fake = at_rest_on_its_red(
        tmp_path, job_minutes=None, pane=shells(fixture("pane-claude-idle.txt"))
    )
    code, _, _, _ = clean(tmp_path, fake)
    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))


def test_while_its_ci_runs_a_background_watch_is_still_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    """08/10, #1016: its `gh run watch` in a shell only waits for the CI."""
    fake = at_rest_on_its_red(tmp_path, job_minutes=5)
    answer_state(fake, 619, RUNNING)
    code, env, _, _ = clean(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert 501 in parking.parked(env.settings)
