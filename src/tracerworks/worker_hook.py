"""The workers' own word on where they are (#1728): a Claude Code hook.

tracerworks launches each Claude worker with hooks (`--settings`, beside its
status line) that run this file: `python3 -I worker_hook.py <state file>
[<kind>]`. It appends ONE JSON line to the worker's state file — the event,
its kind (the hook's matcher: `permission_prompt`, `authentication_failed`…),
its text, the time — and nothing else. tracerworks reads that file instead of
guessing from the screen, which is only a diagnosis now.

A RUN BY THE PROVIDER, NOT BY TRACERWORKS. So the standard library only, no
import of `tracerworks` (`-I`: nothing from the current folder either), and it
never makes the session fail: whatever goes wrong, it says nothing and exits
0. It prints nothing: a `UserPromptSubmit` or `SessionStart` hook's output
would enter the conversation, and a `Stop` hook's exit 2 would refuse to stop.
It only observes; the session guard stays the guard of the tools.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

#: A text kept longer than this is cut: a prompt or a last message is only
#: needed for what it starts with (the marker of a message, a question).
TEXT_LIMIT = 2000
#: Past this size the state file starts again, the old one kept as `.1`:
#: tracerworks reads only its end (`hook_state`).
ROTATE_BYTES = 1024 * 1024
#: The payload's fields that say what an event is about, first found wins.
_KIND_FIELDS = ("notification_type", "error_type", "error", "reason", "source")
_TEXT_FIELDS = ("message", "prompt", "error_details", "last_assistant_message")


def _first_string(payload: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for name in fields:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def line_of(payload: Any, kind: str | None, now: float) -> dict[str, Any]:
    """The line recorded for one hook call — an unreadable payload included:
    the hook ran, which already says the session lives."""
    data = payload if isinstance(payload, dict) else {}
    event = data.get("hook_event_name")
    text = _first_string(data, _TEXT_FIELDS)
    session = data.get("session_id")
    if text is not None:
        # a turn says what it waits for at its END (the memory marker, #1730):
        # a last message keeps its end; a prompt, its start
        text = text[-TEXT_LIMIT:] if event == "Stop" else text[:TEXT_LIMIT]
    return {
        "event": event if isinstance(event, str) else None,
        "kind": kind or _first_string(data, _KIND_FIELDS),
        "text": text,
        "session": session if isinstance(session, str) else None,
        "at": now,
    }


def record(path: Path, raw: str, kind: str | None, now: float) -> None:
    try:
        payload: Any = json.loads(raw)
    except ValueError:
        payload = None
    line = json.dumps(line_of(payload, kind, now), ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > ROTATE_BYTES:  # one line per tool call: bounded
            path.replace(path.with_name(f"{path.name}.1"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(line + "\n")  # one write: lines of two hooks never mix


def main(argv: list[str]) -> int:
    with contextlib.suppress(Exception):
        if len(argv) >= 2:
            raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            kind = argv[2] if len(argv) > 2 and argv[2] else None
            record(Path(argv[1]), raw, kind, time.time())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
