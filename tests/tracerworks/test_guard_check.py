"""Before a launch or a resume, the worktree's session guard answers (#1529).

Real processes: a worktree with the session guard copied in, a wiring and a
rulebook of the tests' own (`fixtures/tracerworks/guard`) — healthy, absent, or
broken. And the operator's escape never reaches a worker. This repository's
own guard passes the same check in `tests/tracerworks_project`.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

import session_guard
from tests.tracerworks.fakes import FIXTURES, LEVEL
from tests.tracerworks.test_start_stop import (
    _exports,
    _shared_runtime,
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
    world,
)
from tracerworks.cli import EXIT_REFUSED, run
from tracerworks.guard_check import guard_blocker
from tracerworks.process import run_process
from tracerworks.providers import CLAUDE

GUARD = FIXTURES / "guard"
PROBE = "mcp__claude_ai_tracerworks_probe"
ESCAPE = "SESSION_GUARD_REPAIR"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


def worktree(tmp_path: Path, *, rulebook: str | None = "fixture") -> Path:
    """A worktree carrying the guard, wired; `rulebook`: the fixture's, a
    text of the test's own, or None for a worktree older than the guard."""
    return guarded_worktree(tmp_path, rulebook, GUARD / "settings.json", "fixture")


def guarded_worktree(
    tmp_path: Path, rulebook: str | None, settings: Path, named: str
) -> Path:
    tree = tmp_path / "wt" / "600"
    (tree / ".claude").mkdir(parents=True)
    if rulebook is None:
        return tree
    shutil.copytree(
        Path(session_guard.__file__).parent,
        tree / "tools" / "session_guard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(settings, tree / ".claude" / "settings.json")
    book = tree / ".claude" / "session_guard.json"
    if rulebook == named:
        shutil.copy(settings.with_name("session_guard.json"), book)
    else:
        book.write_text(rulebook, encoding="utf-8")
    return tree


def test_a_healthy_guard_passes_the_check(tmp_path: Path) -> None:
    tree = worktree(tmp_path)
    assert guard_blocker(run_process, tree, PROBE, (ESCAPE,), "origin/main") is None


@pytest.mark.parametrize(
    ("rulebook", "why"),
    [
        (None, "no PreToolUse command hook"),
        ("{", "a Bash call got exit 2, 0 expected"),
        ('{"read_only_dirs": ["x"]}', "a Bash call got exit 2, 0 expected"),
    ],
    ids=["older than the guard", "rulebook not JSON", "key missing"],
)
def test_an_absent_or_broken_guard_refuses_and_says_how_to_repair(
    tmp_path: Path, rulebook: str | None, why: str
) -> None:
    tree = worktree(tmp_path, rulebook=rulebook)
    reason = guard_blocker(run_process, tree, PROBE, (ESCAPE,), "origin/main")
    assert reason is not None and why in reason
    assert "merge origin/main into the branch, then launch again" in reason


def test_a_guard_that_lets_everything_through_is_broken_too(tmp_path: Path) -> None:
    tree = worktree(tmp_path)
    (tree / ".claude" / "settings.json").write_text(
        '{"hooks": {"PreToolUse": [{"matcher": ".*", "hooks":'
        ' [{"type": "command", "command": "exit 0"}]}]}}',
        encoding="utf-8",
    )
    reason = guard_blocker(run_process, tree, PROBE, (ESCAPE,), "origin/main")
    assert reason is not None and f"a {PROBE} call got exit 0, 2 expected" in reason


def test_the_escape_of_tracerworks_s_environment_never_reaches_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the escape, a broken rulebook answers 1: the check strips it, and
    judges the guard as the worker will meet it."""
    monkeypatch.setenv(ESCAPE, "1")
    tree = worktree(tmp_path, rulebook="{")
    reason = guard_blocker(run_process, tree, PROBE, (ESCAPE,), "origin/main")
    assert reason is not None and "got exit 2, 0 expected" in reason


def test_start_refuses_a_worktree_with_no_guard_before_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime

    monkeypatch.setattr(TmuxWorktreeRuntime, "launch_blocker", lambda s, p: None)
    fake = world(tmp_path)
    issue_answer(fake, 600, "Console")
    git_repo_answers(fake)
    env, _, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)

    assert run(env, ["start", "600"]) == EXIT_REFUSED
    assert "session guard absent or broken" in err.getvalue()
    assert "merge origin/main into the branch" in err.getvalue()
    assert not any("new-window" in c.argv for c in fake.calls)
    assert not (tmp_path / "wt" / ".primers" / "600.md").exists()


@pytest.mark.parametrize("resumed", [False, True])
def test_a_worker_never_inherits_the_escape(tmp_path: Path, resumed: bool) -> None:
    """A tmux server started with it would hand it to every worker: the
    launch script unsets what the project file lists, first."""
    fake, settings, runtime = _shared_runtime(tmp_path, ())
    settings.conventions.worktree_path(600).mkdir(parents=True)
    runtime = dataclasses.replace(
        runtime, settings=dataclasses.replace(settings, stripped_env=(ESCAPE,))
    )
    if resumed:
        runtime.resume(600, "abc-session", "raison", LEVEL)
    else:
        runtime.spawn(600, CLAUDE, "primer", LEVEL)
    exports = _exports(fake)
    assert exports[0] == ["unset", "-v", ESCAPE]
    assert not any(ESCAPE in " ".join(e) for e in exports[1:])


def test_a_red_whose_worktree_has_no_guard_is_not_resumed_and_keeps_its_place(
    tmp_path: Path,
    launched: list[tuple[int, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing new passes before it: the queue's chantier is not launched."""
    from tests.tracerworks.test_clean import RED, answer_state
    from tests.tracerworks.test_parking import resume_world, resumed_argv
    from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime

    monkeypatch.setattr(
        TmuxWorktreeRuntime,
        "guard_blocker",
        lambda self, spec: "session guard absent or broken in x (y)",
    )
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_REFUSED
    said = out.getvalue() + err.getvalue()
    assert "session guard absent or broken" in said
    assert resumed_argv(fake) == [] and launched == []


def test_a_guard_wired_for_other_tools_only_is_not_a_guard(tmp_path: Path) -> None:
    """#1575 review: Claude Code runs a hook only for the tools its matcher
    names — one wired for MCP tools never sees a Bash call."""
    tree = worktree(tmp_path)
    settings = tree / ".claude" / "settings.json"
    text = settings.read_text(encoding="utf-8")
    matcher = '"^(Bash|Edit|Write|MultiEdit|NotebookEdit|mcp__.*)$"'
    assert matcher in text
    settings.write_text(text.replace(matcher, '"^mcp__.*$"'), encoding="utf-8")
    reason = guard_blocker(run_process, tree, PROBE, (ESCAPE,), "origin/main")
    assert reason is not None and "no PreToolUse hook's matcher covers Bash" in reason
