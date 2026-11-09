"""A message received is proven by the worker's hook (#1731).

The pane is a script of captures (as in `test_message`); the worker's state
file is written as its `UserPromptSubmit` hook would, when the test says the
prompt is submitted. « received » is the hook's word; « queued », the
screen's while no submission happened; « lost », neither.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from tests.tracerworks.fakes import TMUX, FakeRunner, fixture, recorded_settings
from tests.tracerworks.test_message import (
    W510,
    Clock,
    make_env,
    pane,
    queued,
    world_with_paste,
)
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, run
from tracerworks.hook_state import hook_file, prompt_heard
from tracerworks.messaging import RECEIPT_SECONDS
from tracerworks.process import Result


def state_file(tmp_path: Path) -> Path:
    """The worker runs with its hooks: its state file exists."""
    path = hook_file(recorded_settings(tmp_path).cache_dir, 510)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event": "Stop", "at": 1.0}) + "\n", encoding="utf-8")
    return path


def submitted(path: Path, fake: FakeRunner) -> None:
    """The hook saw the last pasted envelope submitted."""
    data = fake.ran(*TMUX, "load-buffer")[-1].stdin
    assert data is not None
    line = {"event": "UserPromptSubmit", "text": data.decode(), "at": 2.0}
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(line) + "\n")


def panes(fake: FakeRunner, before: str, after: str | None = None) -> None:
    """`before` until the first paste, then `after` (default: `before`)."""

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510 or not fake.ran(*TMUX, "load-buffer"):
            return Result(0, before)
        return Result(0, before if after is None else after)

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)


def on_enter(fake: FakeRunner, then: object) -> None:
    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] == "Enter" and callable(then):
            then()
        return Result(0, "")

    fake.on(*TMUX, "send-keys", prefix=True)(answer)


def test_the_hook_s_word_is_received_whatever_the_screen_shows(
    tmp_path: Path,
) -> None:
    """The screen shows nothing of it (scrolled, folded): the hook saw it."""
    path = state_file(tmp_path)
    fake = world_with_paste()
    panes(fake, pane())
    on_enter(fake, lambda: submitted(path, fake))
    clock = Clock()
    env, out, err = make_env(fake, tmp_path, clock, b"push your branch")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert "received: its submission seen by the worker's hook" in out.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 1


def test_waiting_on_the_screen_without_a_submission_is_queued(
    tmp_path: Path,
) -> None:
    state_file(tmp_path)
    fake = world_with_paste()
    busy = fixture("pane-claude-working-empty-field.txt")

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510 or not fake.ran(*TMUX, "load-buffer"):
            return Result(0, busy)
        return Result(
            0, queued(fake, "pane-claude-working-message-queued.txt", "m-abc123")
        )

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)
    clock = Clock()
    env, out, err = make_env(fake, tmp_path, clock, b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert "queued" in out.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 1


def test_seen_by_neither_is_lost_pasted_again_once_then_said(
    tmp_path: Path,
) -> None:
    """The worker at rest, the marker even in its thread — the old reading
    said « received » — but no submission: lost, one new paste, refused."""
    state_file(tmp_path)
    fake = world_with_paste()
    at_rest = pane()

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510 or not fake.ran(*TMUX, "load-buffer"):
            return Result(0, at_rest)
        data = fake.ran(*TMUX, "load-buffer")[-1].stdin or b""
        return Result(0, pane(thread=data.decode()))

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)
    clock = Clock()
    env, _, err = make_env(fake, tmp_path, clock, b"push")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    said = err.getvalue()
    assert "nor waiting, the worker at rest: message lost" in said
    assert "not seen by the worker's hook, nor waiting on its screen" in said
    assert len(fake.ran(*TMUX, "load-buffer")) == 2  # once more, never twice more
    markers = {c.stdin.decode().split(", ")[1].split(")")[0]
               for c in fake.ran(*TMUX, "load-buffer") if c.stdin}  # fmt: skip
    assert len(markers) == 2  # under a new marker
    assert sum(clock.sleeps) >= 2 * RECEIPT_SECONDS


def test_the_false_refusals_of_14_and_19_10_are_received(tmp_path: Path) -> None:
    """worker-1369 (14/10) took the message while it thought, its marker out
    of the screen; worker-1499 (19/10) had it queued while starting. The
    screen showed neither in time: the submission, a few seconds later, is
    seen by the hook — received, never lost, never sent twice."""
    path = state_file(tmp_path)
    fake = world_with_paste()
    thinking = fixture("pane-claude-working-empty-field.txt")
    panes(fake, thinking)
    clock = Clock()
    start = clock.now
    done: list[bool] = []

    def later(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        sent = bool(fake.ran(*TMUX, "load-buffer"))
        if clock.now - start >= 20 and sent and not done:  # taken 20 s later
            submitted(path, fake)
            done.append(True)

    clock.sleep = later  # type: ignore[method-assign]
    env, out, err = make_env(fake, tmp_path, clock, b"merge main first")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert "received: its submission seen by the worker's hook" in out.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 1


def test_a_worker_without_hooks_is_read_from_its_screen(tmp_path: Path) -> None:
    """Launched before its hooks: no state file, the screen's reading."""
    fake = world_with_paste()

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510 or not fake.ran(*TMUX, "load-buffer"):
            return Result(0, pane())
        data = fake.ran(*TMUX, "load-buffer")[-1].stdin or b""
        return Result(0, pane(thread=data.decode(), busy=True))

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)
    env, out, err = make_env(fake, tmp_path, Clock(), b"go")
    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert "received" in out.getvalue()
    assert "hook" not in out.getvalue()


def test_a_prompt_is_heard_by_its_marker_only(tmp_path: Path) -> None:
    path = tmp_path / "510.jsonl"
    assert prompt_heard(path, "m-abc123") is None  # no hooks
    prompt = {"event": "UserPromptSubmit", "text": "Message (x, m-def456) :", "at": 1}
    quoted = {"event": "Stop", "text": "m-abc123 quoted", "at": 2}
    path.write_text(f"{json.dumps(prompt)}\n{json.dumps(quoted)}\n")
    assert prompt_heard(path, "m-abc123") is False  # a Stop quoting it is no proof
    assert prompt_heard(path, "m-def456") is True
