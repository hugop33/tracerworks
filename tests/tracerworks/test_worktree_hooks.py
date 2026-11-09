"""The repository's hooks, wired into each worker's worktree (#1703).

tracerworks sets `core.hooksPath` in the worktree's OWN configuration
(`extensions.worktreeConfig`): the pre-push hook then runs the quick checks
before any push of a worker, and the main clone — where the operator works —
is left as it is.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from tests.tracerworks.fakes import REPO, FakeRunner, recorded_settings
from tracerworks.git import Worktree
from tracerworks.process import Result
from tracerworks.worktrees import prepare


def wiring(path: Path) -> list[tuple[str, ...]]:
    return [
        ("git", "-C", str(path), "config", "extensions.worktreeConfig", "true"),
        ("git", "-C", str(path), "config", "--worktree", "core.hooksPath", ".githooks"),
    ]


def new_chantier(fake: FakeRunner) -> None:
    repo = str(REPO)
    fake.answer("git", "-C", repo, "fetch", "--quiet", "origin")
    fake.answer("git", "-C", repo, "ls-remote", "--heads", "origin", prefix=True)
    fake.answer("git", "-C", repo, "branch", "--list", prefix=True)
    fake.answer("git", "-C", repo, "worktree", "add", prefix=True)
    fake.answer("git", "-C", prefix=True)  # the wiring, whatever the worktree


def test_a_new_worktree_gets_the_repository_s_hooks(tmp_path: Path) -> None:
    settings = dataclasses.replace(recorded_settings(tmp_path), hooks_dir=".githooks")
    settings = dataclasses.replace(
        settings,
        conventions=dataclasses.replace(settings.conventions, worktree_root=tmp_path),
    )
    fake = FakeRunner()
    new_chantier(fake)

    prepared = prepare(fake, settings, 600, "console", None)

    added = next(i for i, c in enumerate(fake.calls) if "add" in c.argv)
    assert [c.argv for c in fake.calls[added + 1 :]] == wiring(prepared.path)


def test_a_resumed_worktree_gets_them_too(tmp_path: Path) -> None:
    """A worktree older than the hook: a stopped chantier resumes wired."""
    settings = dataclasses.replace(recorded_settings(tmp_path), hooks_dir=".githooks")
    fake = FakeRunner()
    fake.answer("git", "-C", prefix=True)
    path = tmp_path / "wt" / "520"

    prepare(fake, settings, 520, "x", Worktree(520, path, "agent/520-x"))

    assert [c.argv for c in fake.calls] == wiring(path)


def test_without_a_hooks_folder_nothing_is_wired(tmp_path: Path) -> None:
    settings = dataclasses.replace(recorded_settings(tmp_path), hooks_dir=None)
    fake = FakeRunner()
    path = tmp_path / "wt" / "520"

    prepare(fake, settings, 520, "x", Worktree(520, path, "agent/520-x"))

    assert fake.calls == []


def test_a_wiring_that_fails_refuses_the_launch(tmp_path: Path) -> None:
    """A worker launched unwired would push its cheap reds unseen."""
    from tracerworks.process import CommandError

    settings = dataclasses.replace(recorded_settings(tmp_path), hooks_dir=".githooks")
    fake = FakeRunner()
    fake.on("git", "-C", prefix=True)(Result(1, "", "fatal: not a git repository"))
    path = tmp_path / "wt" / "520"

    with pytest.raises(CommandError, match="hooks"):
        prepare(fake, settings, 520, "x", Worktree(520, path, "agent/520-x"))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_wired_worker_s_push_runs_the_hook_and_the_main_clone_s_does_not(
    tmp_path: Path,
) -> None:
    from tracerworks.process import run_process as real_runner

    main = tmp_path / "main"
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote))
    git("init", "-q", "-b", "main", str(main))
    (main / ".githooks").mkdir()
    hook = main / ".githooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho refused by the hook >&2\nexit 1\n")
    hook.chmod(0o755)
    who = ("-c", "user.name=t", "-c", "user.email=t@t")
    git("-C", str(main), "add", ".")
    git("-C", str(main), *who, "commit", "-qm", "hook")
    git("-C", str(main), "remote", "add", "origin", str(remote))
    worktree = tmp_path / "wt"
    git("-C", str(main), "worktree", "add", "-q", "-b", "work", str(worktree))
    settings = dataclasses.replace(recorded_settings(tmp_path), hooks_dir=".githooks")

    prepare(real_runner, settings, 1, "x", Worktree(1, worktree, "work"))

    refused = subprocess.run(
        ["git", "-C", str(worktree), "push", "-q", "origin", "work"],
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0 and "refused by the hook" in refused.stderr
    git("-C", str(main), "push", "-q", "origin", "main")  # unwired: it goes
