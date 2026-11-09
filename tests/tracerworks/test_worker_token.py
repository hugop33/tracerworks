"""The workers' forbidden rules and their own GitHub token (#1529, ticket #1531).

A worker is denied what reaches a secret, production or the factory; it
uses the workers' fine-grained token when its file is there, says so when it
is not, and is never launched with a token GitHub refuses. The token's value
is never an argument, nor a word of what tracerworks says.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from pathlib import Path

import pytest

from tests.tracerworks.fakes import LEVEL, REPO
from tests.tracerworks.test_start_stop import (
    _script,
    _shared_runtime,
    _words,
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
    world,
)
from tracerworks import worker_token
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, run
from tracerworks.forbidden import denied, label_rules
from tracerworks.providers import CLAUDE
from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime

ROOT = Path(__file__).parents[2]
SECRET = "github_pat_11FAKEVALUE0000never-said"


def token_file(tmp_path: Path, mode: int = 0o600, value: str = SECRET) -> Path:
    path = tmp_path / "secrets" / "worker-github.token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


# --- the rules -----------------------------------------------------------------------


def test_the_label_rules_are_derived_from_the_project_s_labels(tmp_path: Path) -> None:
    """A fine-grained token acts under the operator's login: only this keeps
    a worker from launching a chantier. Renaming a label moves its rules."""
    _, settings, _ = _shared_runtime(tmp_path, ())
    rules = denied(settings)
    assert rules[: len(settings.disallowed_tools)] == list(settings.disallowed_tools)
    for label in ("to-launch", "priority", "starred"):
        assert set(label_rules(label)) <= set(rules)
    assert "Bash(gh *--add-label*to-launch*)" in rules
    assert "Bash(gh *--remove-label*priority*)" in rules
    # the operator's star (#1875): putting, removing, creating with, the label
    assert {
        "Bash(gh *--add-label*starred*)",
        "Bash(gh *--remove-label*starred*)",
        "Bash(gh issue create*starred*)",
        "Bash(gh label *starred*)",
    } <= set(rules)
    # the merge queue's full mode is tracerworks's (#1716); its own label is not
    assert "Bash(gh *--add-label*merge-queue-testing*)" in rules
    assert "Bash(gh *--remove-label*merge-queue-testing*)" in rules
    assert not any(r.endswith("*merge-queue*)") for r in rules)
    assert not any("create*merge-queue" in r for r in rules), "a PR may name it"

    renamed = dataclasses.replace(
        settings, launch_label="go", priority_label="first", star_label="pick"
    )
    rules = denied(renamed)
    assert "Bash(gh *--add-label*go*)" in rules
    assert "Bash(gh issue create*first*)" in rules
    assert "Bash(gh *--remove-label*pick*)" in rules
    assert not any("to-launch" in r or "priority" in r or "starred" in r for r in rules)


@pytest.mark.parametrize("resumed", [False, True])
def test_the_label_rules_reach_the_worker_at_launch_and_resume(
    tmp_path: Path, resumed: bool
) -> None:
    fake, settings, runtime = _shared_runtime(tmp_path, ())
    settings.conventions.worktree_path(600).mkdir(parents=True)
    if resumed:
        runtime.resume(600, "abc-session", "raison", LEVEL)
    else:
        runtime.spawn(600, CLAUDE, "primer", LEVEL)
    words = _words(fake)
    deny = json.loads(words[words.index("--settings") + 1])["permissions"]["deny"]
    assert deny == denied(settings)


# --- the token -----------------------------------------------------------------------


def _with_token(runtime: TmuxWorktreeRuntime, path: Path) -> TmuxWorktreeRuntime:
    settings = dataclasses.replace(runtime.settings, worker_token_file=path)
    return dataclasses.replace(runtime, settings=settings)


@pytest.mark.parametrize("resumed", [False, True])
def test_a_present_token_is_read_by_the_window_s_shell_never_an_argument(
    tmp_path: Path, resumed: bool
) -> None:
    """`gh` and `git push` (gh's credential helper) follow `GH_TOKEN`; the
    configuration folder is the tool's, empty of any token — unsetting one
    variable never falls back to the owner's."""
    fake, settings, runtime = _shared_runtime(tmp_path, ())
    settings.conventions.worktree_path(600).mkdir(parents=True)
    path = token_file(tmp_path)
    runtime = _with_token(runtime, path)

    if resumed:
        runtime.resume(600, "abc-session", "raison", LEVEL)
    else:
        runtime.spawn(600, CLAUDE, "primer", LEVEL)

    script = _script(fake)
    assert SECRET not in " ".join(c for call in fake.calls for c in call.argv)
    exports = script.split("; exec ", 1)[0].split("; ")
    assert f'export GH_TOKEN="$(cat {shlex.quote(str(path))})"' in exports
    config = settings.cache_dir / "gh-worker"
    assert f"export GH_CONFIG_DIR={shlex.quote(str(config))}" in exports
    assert config.is_dir() and not any(config.iterdir())
    assert runtime.github_auth(600) == (
        "#600: the worker uses the workers' fine-grained GitHub token"
    )


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda p: p / "absent.token", "no workers' GitHub token"),
        (lambda p: token_file(p, value=""), "is empty"),
        (lambda p: token_file(p, mode=0o644), "readable by others: chmod 600"),
    ],
)
def test_without_a_usable_token_the_worker_launches_as_today_and_it_is_said(
    tmp_path: Path, make: object, why: str
) -> None:
    fake, settings, runtime = _shared_runtime(tmp_path, ())
    settings.conventions.worktree_path(600).mkdir(parents=True)
    runtime = _with_token(runtime, make(tmp_path))  # type: ignore[operator]

    assert runtime.launch_blocker(CLAUDE) is None  # never asked GitHub
    runtime.spawn(600, CLAUDE, "primer", LEVEL)

    assert "GH_TOKEN" not in _script(fake) and "GH_CONFIG_DIR" not in _script(fake)
    said = runtime.github_auth(600)
    assert said.startswith("#600: WARNING") and why in said
    assert "the machine's GitHub authentication" in said


def test_start_says_which_token_the_worker_holds(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """The relauncher's journal is start-next's output: the state is in it."""
    fake = world(tmp_path)
    issue_answer(fake, 600, "Spec — Console opérateur")
    git_repo_answers(fake)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)

    assert run(env, ["start", "600"]) == EXIT_OK, err.getvalue()
    assert "#600: WARNING — no workers' GitHub token" in out.getvalue()


@pytest.mark.parametrize("status", [401, 403, 0])
def test_a_token_github_refuses_refuses_the_launch_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Expired, revoked, or GitHub unreachable: « unknown » is never
    « accepted », and an expired token never hands back the owner's."""
    asked: list[tuple[str, str]] = []

    def fetch(url: str, token: str) -> int:
        asked.append((url, token))
        return status

    monkeypatch.setattr(worker_token, "fetch_status", fetch)
    fake = world(tmp_path)
    issue_answer(fake, 600, "Console")
    git_repo_answers(fake)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    path = token_file(tmp_path)
    env = dataclasses.replace(
        env, settings=dataclasses.replace(env.settings, worker_token_file=path)
    )

    assert run(env, ["start", "600"]) == EXIT_REFUSED
    assert asked == [("https://api.github.com/repos/owner/repo", SECRET)]
    said = out.getvalue() + err.getvalue()
    assert "refused by GitHub" in said and SECRET not in said
    assert not fake.ran("git", "-C", str(REPO), "worktree", "add")
    assert not any("new-window" in c.argv for c in fake.calls)
    assert not (tmp_path / "cache").exists()


def test_an_accepted_token_lets_the_launch_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_token, "fetch_status", lambda url, token: 200)
    path = token_file(tmp_path)
    assert worker_token.refused(path, "owner/repo") is None
    assert worker_token.missing(path) is None


def test_the_refusal_names_the_file_and_the_status_never_the_value(
    tmp_path: Path,
) -> None:
    path = token_file(tmp_path)
    reason = worker_token.refused(path, "owner/repo", lambda url, token: 401)
    assert reason is not None and str(path) in reason and "HTTP 401" in reason
    assert SECRET not in reason
