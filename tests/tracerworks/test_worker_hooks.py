"""The workers say themselves where they are (#1728).

A Claude worker's hooks append its last word to its state file; `status`
reads that word before the screen. The script runs here as Claude Code runs
it — a process, its payload on stdin — and the launch settings' own command
line is executed, so that what is tested is what a worker gets.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import LEVEL, fixture, recorded_settings, recorded_world
from tests.tracerworks.test_status import NOW, env_for
from tracerworks.cli import EXIT_OK, run
from tracerworks.hook_state import (
    FAILURES,
    NOTIFICATIONS,
    SCRIPT,
    TAIL_BYTES,
    HookEvent,
    hook_file,
    last_event,
    worker_hooks,
)
from tracerworks.observe import WorkerState, read_worker
from tracerworks.runtime.tmux import Window

CLAUDE_WINDOW = Window("worker-510", "claude", 0, "claude")


def hook(path: Path, payload: str, *kind: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(path), *kind],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# --- the script --------------------------------------------------------------


def test_each_event_is_one_line_written_even_without_its_folder(
    tmp_path: Path,
) -> None:
    state = tmp_path / "not" / "yet" / "510.jsonl"
    prompt = {
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs your permission to use Bash",
        "session_id": "abc",
    }
    done = hook(state, json.dumps(prompt), "permission_prompt")
    stop = hook(state, json.dumps({"hook_event_name": "Stop", "session_id": "abc"}))

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert (stop.returncode, stop.stdout) == (0, "")
    first, second = lines(state)
    assert first["event"] == "Notification"
    assert first["kind"] == "permission_prompt"
    assert first["text"] == "Claude needs your permission to use Bash"
    assert first["session"] == "abc"
    assert isinstance(first["at"], float)
    assert (second["event"], second["kind"], second["text"]) == ("Stop", None, None)


def test_a_prompt_is_recorded_cut_and_never_echoed(tmp_path: Path) -> None:
    """A `UserPromptSubmit` hook's output would enter the conversation."""
    state = tmp_path / "510.jsonl"
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "x" * 5000}
    done = hook(state, json.dumps(payload))
    assert (done.returncode, done.stdout) == (0, "")
    [line] = lines(state)
    assert line["event"] == "UserPromptSubmit"
    assert line["text"] == "x" * 2000


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2]", '{"a": 1}'])
def test_an_unreadable_payload_still_says_the_hook_ran(
    tmp_path: Path, payload: str
) -> None:
    state = tmp_path / "510.jsonl"
    done = hook(state, payload, "rate_limit")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    [line] = lines(state)
    assert (line["event"], line["kind"]) == (None, "rate_limit")


def test_the_session_never_fails_even_when_nothing_can_be_written(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    done = hook(blocker / "510.jsonl", '{"hook_event_name": "Stop"}')
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    no_path = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        input=b"{}",
        capture_output=True,
        check=False,
    )
    assert no_path.returncode == 0


# --- the launch settings -----------------------------------------------------


def test_every_event_of_the_spec_is_hooked_with_its_kind(tmp_path: Path) -> None:
    hooks: Any = worker_hooks(tmp_path, 510)
    assert [g["matcher"] for g in hooks["Notification"]] == list(NOTIFICATIONS)
    assert [g["matcher"] for g in hooks["StopFailure"]] == list(FAILURES)
    for event in ("Stop", "SessionEnd", "UserPromptSubmit"):
        [group] = hooks[event]
        assert "matcher" not in group
    for group in hooks["StopFailure"]:
        [handler] = group["hooks"]
        assert handler["command"].endswith(f" {group['matcher']}")


def test_the_hook_command_a_worker_gets_writes_its_state_file(tmp_path: Path) -> None:
    """The command as written in the settings, run by a shell as Claude Code
    runs it — the matcher's kind included."""
    hooks: Any = worker_hooks(tmp_path / "cache", 510)
    [handler] = hooks["StopFailure"][0]["hooks"]
    payload = '{"hook_event_name": "StopFailure"}'
    done = subprocess.run(
        ["sh", "-c", handler["command"]], input=payload, text=True, check=False
    )
    assert done.returncode == 0
    event = last_event(hook_file(tmp_path / "cache", 510))
    assert event is not None
    assert (event.event, event.kind) == ("StopFailure", "authentication_failed")


@pytest.mark.parametrize("resumed", [False, True])
def test_a_worker_keeps_its_status_line_and_its_denials_beside_its_hooks(
    tmp_path: Path, resumed: bool
) -> None:
    import dataclasses

    from tests.tracerworks.test_start_stop import (
        _launch_settings,
        _shared_runtime,
        _words,
    )
    from tracerworks.providers import CLAUDE

    fake, settings, runtime = _shared_runtime(tmp_path, ())
    settings = dataclasses.replace(
        settings, claude_statusline=True, disallowed_tools=("mcp__database",)
    )
    runtime = dataclasses.replace(runtime, settings=settings)
    settings.conventions.worktree_path(600).mkdir(parents=True)

    if resumed:
        runtime.resume(600, "abc-session", "raison", LEVEL)
    else:
        runtime.spawn(600, CLAUDE, "primer", LEVEL)

    words = _words(fake)
    launch = _launch_settings(words)
    assert launch["hooks"] == worker_hooks(settings.cache_dir, 600)
    assert launch["statusLine"]["command"].endswith("tracerworks statusline")
    assert launch["permissions"]["deny"][0] == "mcp__database"
    assert words[-1].endswith("Exécute-la.")


# --- the reading -------------------------------------------------------------


def write(path: Path, *events: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_the_last_word_is_the_last_readable_line_of_the_end(tmp_path: Path) -> None:
    state = tmp_path / "510.jsonl"
    work = {"event": "PostToolUse", "kind": None, "text": None, "at": 1.0}
    write(state, *[work] * (TAIL_BYTES // 40), {"event": "Stop", "at": 2.0})
    with state.open("a", encoding="utf-8") as out:
        out.write('{"event": "Notifi')  # a line being written
    assert last_event(state) == HookEvent("Stop", None, None, 2.0)
    assert last_event(tmp_path / "none.jsonl") is None


IDLE = fixture("pane-claude-idle.txt")
PERMISSION = fixture("pane-claude-permission.txt")


def event(name: str, kind: str | None = None, text: str | None = None) -> HookEvent:
    return HookEvent(name, kind, text, NOW)


@pytest.mark.parametrize(
    ("heard", "pane", "state", "waits"),
    [
        # the invite of 12/10: a prompt the screen may not show as one
        (
            event("Notification", "permission_prompt", "Claude needs your permission"),
            IDLE,
            WorkerState.ASKING,
            "permission: Claude needs your permission",
        ),
        # the screen says better what the prompt asks
        (
            event("Notification", "permission_prompt", "Claude needs permission"),
            PERMISSION,
            WorkerState.ASKING,
            "permission: Bash command: rm node_modules",
        ),
        # the question of 19/10
        (
            event("Notification", "elicitation_dialog", "Which branch?"),
            IDLE,
            WorkerState.ASKING,
            "question: Which branch?",
        ),
        (
            event("Notification", "agent_needs_input", "Shall I push?"),
            IDLE,
            WorkerState.ASKING,
            "question: Shall I push?",
        ),
        # the login of 22/10
        (
            event("StopFailure", "authentication_failed"),
            IDLE,
            WorkerState.ASKING,
            "login expired — /login in the worker's window",
        ),
        (
            event("StopFailure", "rate_limit"),
            IDLE,
            WorkerState.ASKING,
            "limit reached: rate_limit",
        ),
        (event("Stop"), PERMISSION, WorkerState.WAITING, None),
        (event("Notification", "idle_prompt"), IDLE, WorkerState.WAITING, None),
        # the prompt answered: its tool ran
        (event("PostToolUse"), IDLE, WorkerState.WORKING, None),
        (event("UserPromptSubmit", None, "go"), IDLE, WorkerState.WORKING, None),
        # a session that ended says nothing: the screen does
        (event("SessionEnd", "prompt_input_exit"), PERMISSION, WorkerState.ASKING,
         "permission: Bash command: rm node_modules"),
        (None, IDLE, WorkerState.WAITING, None),
    ],
)  # fmt: skip
def test_a_worker_is_read_from_its_last_word_before_its_screen(
    heard: HookEvent | None, pane: str, state: WorkerState, waits: str | None
) -> None:
    reading = read_worker(CLAUDE_WINDOW, pane, heard)
    assert (reading.state, reading.waits) == (state, waits)
    # its kind, never read back from the words: the words may be translated
    assert (reading.kind is None) == (waits is None)
    if waits is not None:
        assert waits.startswith(
            {"permission": "permission", "question": "question", "login": "login",
             "limit": "limit"}[str(reading.kind)]
        )  # fmt: skip


def test_a_dead_or_closed_window_is_never_read_from_its_hooks() -> None:
    gone = Window("worker-510", "bash", 0, "claude")
    prompt = event("Notification", "permission_prompt", "x")
    assert read_worker(gone, IDLE, prompt).state is WorkerState.DEAD
    assert read_worker(None, None, prompt).state is WorkerState.STOPPED
    assert read_worker(gone, IDLE, prompt).waits is None


def test_status_shows_a_prompt_with_its_text_from_the_state_file(
    tmp_path: Path,
) -> None:
    """The screen at rest, the hooks' file saying a prompt waits."""
    settings = recorded_settings(tmp_path)
    write(
        hook_file(settings.cache_dir, 510),
        {"event": "UserPromptSubmit", "text": "go", "at": NOW - 60},
        {
            "event": "Notification",
            "kind": "permission_prompt",
            "text": "Claude needs your permission to use Bash",
            "at": NOW - 30,
        },
    )
    env, out, _ = env_for(recorded_world(), tmp_path)

    assert run(env, ["status"]) == EXIT_OK
    [line] = [x for x in out.getvalue().splitlines() if x.startswith("#510")]
    assert (
        "waits for a human (permission: Claude needs your permission to use Bash)"
        in line
    )

    out.truncate(0), out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    [w] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 510]
    assert w["state"] == "asking"
