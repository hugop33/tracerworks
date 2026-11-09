"""Fixtures shared by the `tracerworks` tests."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator

import pytest

from tests.tracerworks.fakes import TEST_TMUX, reap_stale_tmux, tmux_dir
from tracerworks.level import Level
from tracerworks.providers import Provider
from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, str]]:
    """A runtime able to launch, recording what `spawn` was given.

    The real launch line is not wired (arbitrage on #565); these tests cover
    everything around it.
    """
    calls: list[tuple[int, str, str]] = []
    monkeypatch.setattr(
        TmuxWorktreeRuntime, "launch_blocker", lambda self, provider: None
    )
    # the worktree's session guard answers (#1529; tried in test_guard_check)
    monkeypatch.setattr(TmuxWorktreeRuntime, "guard_blocker", lambda self, spec: None)

    def spawn(
        self: TmuxWorktreeRuntime,
        spec: int,
        provider: Provider,
        primer: str,
        level: Level,
    ) -> None:
        calls.append((spec, provider.name, primer))

    monkeypatch.setattr(TmuxWorktreeRuntime, "spawn", spawn)
    return calls


@pytest.fixture(autouse=True)
def no_real_session_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test reads the machine's Claude Code logs (#2252): `status` and the
    snapshot read each worker's model and effort there, and the recorded
    world's worktrees are the VPS's real paths. A test with logs sets its
    own `CLAUDE_CONFIG_DIR`."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude")))


@pytest.fixture(autouse=True)
def no_real_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test signals a real process (#2096): the overdue pass's pids are a
    fake /proc's, and the VPS's workers run their tests beside the suite."""
    from tracerworks import overdue

    def refused(target: int, sig: int) -> None:
        raise AssertionError(f"a test sent signal {sig} to {target}")

    monkeypatch.setattr(overdue, "SIGNAL_PID", refused)
    monkeypatch.setattr(overdue, "SIGNAL_GROUP", refused)


@pytest.fixture(autouse=True)
def no_github_with_the_workers_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test asks GitHub about a token (#1529): one that needs an answer
    monkeypatches `fetch_status` itself."""
    from tracerworks import worker_token

    def offline(url: str, token: str) -> int:
        raise AssertionError(f"a test asked GitHub about a token: {url}")

    monkeypatch.setattr(worker_token, "fetch_status", offline)


def own_tmux_servers() -> list[str]:
    """The test tmux servers of THIS pytest process still answering."""
    try:
        names = sorted(entry.name for entry in tmux_dir().iterdir())
    except OSError:
        return []
    mine = [
        name
        for name in names
        if (owner := TEST_TMUX.fullmatch(name)) and int(owner.group(1)) == os.getpid()
    ]
    return [
        name
        for name in mine
        if subprocess.run(
            ["tmux", "-L", name, "has-session"], capture_output=True, check=False
        ).returncode
        == 0
    ]


@pytest.fixture(scope="session", autouse=True)
def no_tmux_server_outlives_the_suite() -> Iterator[None]:
    """On 23/10, test tmux servers and their stub `claude` ran on two hours
    after a worker's suite (#1724). Before: stop what a killed run left. After:
    whatever server of this run still answers is stopped, then reported — a
    test that leaks one fails the suite, not the machine."""
    reap_stale_tmux()
    yield
    leaked = own_tmux_servers()
    for name in leaked:
        subprocess.run(["tmux", "-L", name, "kill-server"], check=False)
        (tmux_dir() / name).unlink(missing_ok=True)
    assert not leaked, f"tmux servers left running by this suite: {leaked}"
