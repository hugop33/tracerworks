"""Each worker runs in its own systemd scope, in `agents.slice` (#1724).

On 23/10 a worker's test tmux servers and stub `claude` ran on two hours after
its suite, and on 16/10 an orphan `python3` held a core for twenty minutes:
stopping a worker closed its window, not what its window had started. A scope
is a cgroup: stopping it stops every process in it, whatever its parent.

`systemd-run --scope` executes the command itself (same PID, measured on the
VPS on 23/10), so the pane's process is still the provider, which is how
`status` recognises a live worker.
"""

from __future__ import annotations

import dataclasses
import shlex
from pathlib import Path

from tracerworks.providers import CLAUDE
from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime

from .fakes import LEVEL, FakeRunner
from .test_start_stop import _script, _shared_runtime


def scoped(
    tmp_path: Path, slice_name: str | None = "agents.slice", memory_max: int = 3
) -> tuple[FakeRunner, TmuxWorktreeRuntime]:
    fake, settings, runtime = _shared_runtime(tmp_path, ())
    settings = dataclasses.replace(
        settings, worker_slice=slice_name, worker_memory_max_gb=memory_max
    )
    fake.answer("systemd-run", prefix=True)
    return fake, dataclasses.replace(
        runtime, settings=settings, clock=lambda: 1761250000.0
    )


def launched_words(fake: FakeRunner) -> list[str]:
    return shlex.split("exec " + _script(fake).split("; exec ", 1)[1])


def test_a_worker_is_launched_in_its_own_scope_of_the_slice(tmp_path: Path) -> None:
    fake, runtime = scoped(tmp_path)

    runtime.spawn(600, CLAUDE, "primer", LEVEL)

    words = launched_words(fake)
    assert words[:8] == [
        "exec",
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice=agents.slice",
        "--unit=tracerworks-worker-600-1761250000",
    ]
    assert words[8:11] == [
        "--property=MemoryMax=3G",
        "--property=MemorySwapMax=0",
        "--property=OOMPolicy=continue",
    ]
    assert words[11] == "--"
    assert words[12:15] == ["claude", "--permission-mode", "auto"]
    assert words[-1].startswith("Lis le fichier ")


def test_a_resumed_worker_gets_a_scope_too(tmp_path: Path) -> None:
    fake, runtime = scoped(tmp_path)

    runtime.resume(600, "abc-session", "raison", LEVEL)

    assert "--slice=agents.slice" in launched_words(fake)


def test_a_worker_s_scope_is_capped_at_launch_and_resume(tmp_path: Path) -> None:
    """#2095: on 29/10 a worker's `node --test` grew to 7.1 GB for 9 h and the
    kernel killed the runners. The cap is the worker's, its swap included (the
    4 GB of swap took a runaway on, measured 30/10); `OOMPolicy=continue`, or
    systemd stops the whole scope — Claude with it — at the first kill."""
    fake, runtime = scoped(tmp_path, memory_max=5)

    runtime.resume(600, "abc-session", "raison", LEVEL)

    words = launched_words(fake)
    assert "--property=MemoryMax=5G" in words
    assert "--property=MemorySwapMax=0" in words
    assert "--property=OOMPolicy=continue" in words
    assert words.index("--property=OOMPolicy=continue") < words.index("--")


def test_without_a_slice_the_launch_is_as_before(tmp_path: Path) -> None:
    # A Mac has no systemd: `[workers] systemd_slice` left out.
    fake, runtime = scoped(tmp_path, None)

    runtime.spawn(600, CLAUDE, "primer", LEVEL)

    assert launched_words(fake)[1] == "claude"


def test_stopping_a_worker_stops_its_scope_and_everything_in_it(tmp_path: Path) -> None:
    fake, runtime = scoped(tmp_path)
    fake.answer("tmux", "list-windows", prefix=True)
    fake.answer("systemctl", prefix=True)

    runtime.stop(600)

    [stop] = fake.ran("systemctl")
    assert stop.argv == (
        "systemctl",
        "--user",
        "stop",
        "tracerworks-worker-600-*.scope",
    )


def test_stopping_a_worker_without_a_slice_touches_no_unit(tmp_path: Path) -> None:
    fake, runtime = scoped(tmp_path, None)
    fake.answer("tmux", "list-windows", prefix=True)

    runtime.stop(600)

    assert fake.ran("systemctl") == []


def test_without_a_reachable_systemd_the_worker_starts_unscoped(tmp_path: Path) -> None:
    # No user bus (a login without lingering): `exec systemd-run` would end
    # the pane before the provider ever started.
    fake, runtime = scoped(tmp_path)
    fake.answer("systemd-run", prefix=True, returncode=1, stdout="no bus")

    runtime.spawn(600, CLAUDE, "primer", LEVEL)

    assert launched_words(fake)[1] == "claude"
