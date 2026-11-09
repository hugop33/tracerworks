"""Provider, declared quota, 80 % cap and resource guards (#569).

The cap is an ADMISSION guard: it decides which provider a new worker gets,
never stops a running one. The resource guards refuse a new worker while two
run, while the runner is busy (or unreadable) and under the memory threshold.
"""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest

from tests.tracerworks.fakes import REPO, FakeRunner, recorded_settings, recorded_world
from tests.tracerworks.test_start_stop import (
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
    world,
)
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, Env, run
from tracerworks.github import RunnerStatus
from tracerworks.guards import refusals
from tracerworks.observe import Worker, observe_workers
from tracerworks.routing import route
from tracerworks.runtime.tmux import Tmux
from tracerworks.state_file import State, StateFileError, load, parse, render

IDLE = RunnerStatus("vps-1", "online", False)


# --- routing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "over_cap", "expected"),
    [
        (State(provider="claude", claude_weekly_used=79), False, "claude"),
        (State(provider="claude", claude_weekly_used=80), False, None),
        (
            State(provider="claude", claude_weekly_used=80, codex_available=True),
            False,
            "codex",
        ),
        (
            State(provider="claude", claude_weekly_used=95, codex_available=True),
            False,
            "codex",
        ),
        (State(provider="claude", claude_weekly_used=80), True, "claude"),
        (State(provider="claude", claude_weekly_used=None), False, "claude"),
        (State(provider="codex", codex_available=True), False, "codex"),
        (State(provider="codex", codex_available=False), False, None),
        # The weekly reset does not bring Claude back: the operator's « Codex » holds.
        (
            State(provider="codex", claude_weekly_used=0, codex_available=True),
            False,
            "codex",
        ),
    ],
)
def test_the_provider_of_a_new_worker(
    state: State, over_cap: bool, expected: str | None
) -> None:
    assert route(state, over_cap=over_cap).provider == expected


def test_a_refusal_at_the_cap_says_why() -> None:
    reason = route(State(claude_weekly_used=80)).reason
    assert "cap reached" in reason and "codex.available: false" in reason


def test_the_cap_reads_the_measured_week_before_the_declared_one() -> None:
    """#822: the weekly percent Claude Code reports wins; the declared one
    (`tracerworks quota`) is only the fallback when nothing was measured."""
    declared_low = State(claude_weekly_used=10)
    measured = route(declared_low, measured=85.4)
    assert measured.provider is None
    assert "85/80% (measured)" in measured.reason
    assert route(State(claude_weekly_used=90), measured=12.0).provider == "claude"
    fallback = route(State(claude_weekly_used=90), measured=None)
    assert fallback.provider is None and "(declared)" in fallback.reason


# --- guards ------------------------------------------------------------------


def workers(tmp_path: Path) -> list[Worker]:
    fake = recorded_world()
    return observe_workers(
        fake, recorded_settings(tmp_path), Tmux(fake, "widgets", None)
    )


def test_no_guard_says_no_in_a_calm_world() -> None:
    assert refusals(IDLE, 4000, State()) == []


@pytest.mark.parametrize(
    ("runner", "memory", "fragment"),
    [
        (
            RunnerStatus("vps-1", "online", True),
            3499,
            "< threshold 3500 MB (runner",
        ),
        (RunnerStatus("vps-1", "unknown", None), 4000, "unreadable"),
        (IDLE, 2499, "2499 MB < threshold 2500 MB"),
        (IDLE, None, "available memory unreadable"),
    ],
)
def test_each_resource_guard_refuses(
    runner: RunnerStatus, memory: int | None, fragment: str
) -> None:
    [reason] = refusals(runner, memory, State())
    assert fragment in reason


def test_an_offline_runner_does_not_block() -> None:
    assert refusals(RunnerStatus("vps-1", "offline", False), 4000, State()) == []


def test_start_is_refused_by_the_guards_before_touching_git(tmp_path: Path) -> None:
    fake = world(tmp_path)  # 3 windows, runner busy, 2,2 Go
    issue_answer(fake, 600, "Console")
    env, _, err = make_env(fake, tmp_path)

    assert run(env, ["start", "600"]) == EXIT_REFUSED
    assert "3/2 sessions busy: #501 (1st) · #510 (2nd) · #530 (3rd);" in err.getvalue()
    assert "busy" in err.getvalue() and "< threshold 3500 MB" in err.getvalue()
    assert not fake.ran("git", "-C", str(REPO), "worktree", "add")


def test_start_at_the_cap_refuses_claude_unless_operator_says_over_cap(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake = world(tmp_path)
    issue_answer(fake, 600, "Console")
    git_repo_answers(fake)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    env.settings.state_file.write_text("claude:\n  weekly_used: 80\n")

    assert run(env, ["start", "600"]) == EXIT_REFUSED
    assert "cap reached" in err.getvalue()
    assert not fake.ran("git", "-C", str(REPO), "worktree", "add")

    run(env, ["start", "600", "--over-cap"])
    assert "cap exceeded by explicit decision" in out.getvalue()
    assert fake.ran("git", "-C", str(REPO), "worktree", "add")


# --- the commands and the file -------------------------------------------------


def cli_env(tmp_path: Path) -> tuple[Env, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    env = Env(
        recorded_settings(tmp_path),
        runner=FakeRunner(),
        stdin=io.BytesIO(),
        out=out,
        err=err,
    )
    return env, out, err


def test_quota_is_declared_and_kept(tmp_path: Path) -> None:
    env, out, _ = cli_env(tmp_path)

    assert run(env, ["quota", "78"]) == EXIT_OK
    assert load(env.settings.state_file).claude_weekly_used == 78
    assert run(env, ["quota", "80"]) == EXIT_OK
    assert "cap reached" in out.getvalue()
    assert run(env, ["quota", "101"]) == EXIT_REFUSED
    assert load(env.settings.state_file).claude_weekly_used == 80


def test_quota_and_provider_keep_what_the_settings_tab_set(tmp_path: Path) -> None:
    """#2190: the command line writes through the same module as the tab, and
    keeps its keys — and adds none it was not asked for."""
    env, _, _ = cli_env(tmp_path)
    env.settings.state_file.write_text(
        "max_workers: 1\nmin_available_memory_mb: 4000\n"
    )
    assert run(env, ["quota", "42"]) == EXIT_OK
    assert run(env, ["provider", "claude"]) == EXIT_OK
    state = load(env.settings.state_file)
    assert (state.max_workers, state.min_available_memory_mb) == (1, 4000)
    assert (state.claude_weekly_used, state.provider) == (42, "claude")
    assert "weekly_cap" not in env.settings.state_file.read_text()


def test_provider_codex_is_refused_while_its_route_is_closed(tmp_path: Path) -> None:
    env, out, err = cli_env(tmp_path)

    assert run(env, ["provider", "codex"]) == EXIT_REFUSED
    assert "codex.available: false" in err.getvalue()

    env.settings.state_file.write_text("codex:\n  available: true\n")
    assert run(env, ["provider", "codex"]) == EXIT_OK
    assert load(env.settings.state_file).provider == "codex"
    assert "NEXT workers" in out.getvalue()
    assert run(env, ["provider", "claude"]) == EXIT_OK
    assert load(env.settings.state_file) == State(
        provider="claude", codex_available=True
    )


def test_the_state_file_round_trips_and_refuses_what_it_cannot_trust() -> None:
    state = State(
        provider="codex", claude_weekly_used=63, codex_available=True, max_workers=3
    )
    assert parse(render(state)) == state
    assert parse("") == State()
    for broken in [
        "provider: gpt\n",
        "claude:\n  weekly_cap: 0\n",
        "claude:\n  weekly_used: 120\n",
        "claude:\n  weekly_capp: 80\n",
        "max_workers: -1\n",
        "codex:\n  available: yes\n",
        "   provider: claude\n",
    ]:
        with pytest.raises(StateFileError):
            parse(broken)


def test_the_documented_example_parses() -> None:
    """The block of architecture §6.4, verbatim (plus the codex route)."""
    example = (
        "provider: claude          # claude | codex — pour les prochains workers\n"
        "claude:\n"
        "  weekly_used: 63         # déclaré par `tracerworks quota 63`\n"
        "  weekly_cap: 80\n"
        "max_workers: 2\n"
        "min_available_memory_mb: 2500\n"
    )
    assert parse(example) == dataclasses.replace(State(), claude_weekly_used=63)


def test_a_busy_runner_alone_no_longer_blocks_with_enough_memory() -> None:
    """26/09: a CI on the runner kept every chantier waiting for an hour with
    6 Go free. Above the busy-runner bar, a worker may start."""
    busy = RunnerStatus("vps-1", "online", True)
    assert refusals(busy, 3500, State()) == []
