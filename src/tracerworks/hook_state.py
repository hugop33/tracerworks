"""The state file a Claude worker's hooks write, what tracerworks reads in it (#1728).

`worker_hooks` is what a worker's `--settings` carries: each event of the
spec, each matcher on its own entry so that the kind is an argument of the
script, certain whatever the payload calls it. Two events more than the
spec's list, both cheap: `PostToolUse` says the worker works again once a
prompt or a question is answered — without it, the last word would stay
« permission » until the end of the turn — and `SessionStart` that a session
opened (a resumed worker after its `SessionEnd`).

`last_event` reads only the end of the file: one line per tool call makes it
grow with the chantier, and only its last word counts.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

#: The hooks' script, beside this module in the copy tracerworks runs from.
SCRIPT = Path(__file__).with_name("worker_hook.py")
#: The end of the file read for the last event, in bytes.
TAIL_BYTES = 16 * 1024
HOOK_TIMEOUT_SECONDS = 10

NOTIFICATIONS = (
    "permission_prompt",
    "idle_prompt",
    "elicitation_dialog",
    "agent_needs_input",
)
FAILURES = ("authentication_failed", "rate_limit", "billing_error", "overloaded")
#: Events without a matcher, recorded as they come.
PLAIN = ("Stop", "SessionEnd", "UserPromptSubmit", "PostToolUse", "SessionStart")


#: The line a worker ends its turn with while it waits for memory (#1730):
#: its primer asks for it, its `Stop` hook records it, the relauncher reads it.
MEMORY_MARKER = "TRACERWORKS-WAITS-FOR-MEMORY"


@dataclass(frozen=True)
class HookEvent:
    event: str
    kind: str | None
    text: str | None
    at: float

    @property
    def waits_for_memory(self) -> bool:
        return self.event == "Stop" and MEMORY_MARKER in (self.text or "")


def hook_file(cache_dir: Path, spec: int) -> Path:
    """Under tracerworks's folder, never in the worktree: the worker's state is
    not its code, and `clean` must not count it as unsaved work."""
    return cache_dir / "hooks" / f"{spec}.jsonl"


def worker_hooks(cache_dir: Path, spec: int) -> dict[str, object]:
    """The `hooks` of a worker's launch settings."""
    target = shlex.quote(str(hook_file(cache_dir, spec)))
    base = f"{shlex.quote(sys.executable)} -I {shlex.quote(str(SCRIPT))} {target}"

    def entry(command: str, matcher: str | None = None) -> dict[str, object]:
        handler = {"type": "command", "command": command}
        group: dict[str, object] = {
            "hooks": [{**handler, "timeout": HOOK_TIMEOUT_SECONDS}]
        }
        return group if matcher is None else {"matcher": matcher, **group}

    hooks: dict[str, object] = {
        "Notification": [entry(f"{base} {k}", k) for k in NOTIFICATIONS],
        "StopFailure": [entry(f"{base} {k}", k) for k in FAILURES],
    }
    hooks.update({event: [entry(base)] for event in PLAIN})
    return hooks


def _event_of(line: str) -> HookEvent | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    event, at = data.get("event"), data.get("at")
    if not isinstance(event, str) or isinstance(at, bool):
        return None
    if not isinstance(at, int | float):
        return None
    kind, text = data.get("kind"), data.get("text")
    return HookEvent(
        event,
        kind if isinstance(kind, str) else None,
        text if isinstance(text, str) else None,
        float(at),
    )


#: How far back a message's submission is looked for (#1731): a busy worker
#: writes one line per tool call between the send and its reading.
HEARD_TAIL_BYTES = 256 * 1024


def _tail(path: Path, size: int) -> str:
    """The end of the state file; raised like `open`."""
    with path.open("rb") as file:
        end = file.seek(0, 2)
        file.seek(max(end - size, 0))
        return file.read().decode("utf-8", errors="replace")


def prompt_heard(path: Path, marker: str) -> bool | None:
    """Whether the worker's `UserPromptSubmit` hook saw a prompt carrying
    `marker` (#1731) — the proof a message arrived. None: no state file, the
    worker runs without hooks (launched before them), the screen says."""
    try:
        tail = _tail(path, HEARD_TAIL_BYTES)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    for line in tail.splitlines():
        event = _event_of(line)
        if (
            event is not None
            and event.event == "UserPromptSubmit"
            and marker in (event.text or "")
        ):
            return True
    return False


def last_event(path: Path) -> HookEvent | None:
    """The worker's last word, None without one — no file, an unreadable
    one, or no readable line in its end (a line cut by the tail is skipped)."""
    try:
        tail = _tail(path, TAIL_BYTES)
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if (event := _event_of(line)) is not None:
            return event
    return None


#: What comes after a turn's end while the worker stays at rest: Claude Code
#: says, a minute later, that it waits for input, and compacts an idle session
#: hours later (#1810) — the turn's word stays. Only a prompt or a tool, or an
#: event this reading does not know, say the turn is over.
_AT_REST_AFTER = ("Notification", "SessionStart")


def _events(path: Path) -> list[HookEvent] | None:
    """The readable events of the file's end, None when it cannot be read."""
    try:
        tail = _tail(path, TAIL_BYTES)
    except OSError:
        return None
    return [e for line in tail.splitlines() if (e := _event_of(line)) is not None]


def _turn_end(events: list[HookEvent]) -> HookEvent | None:
    for event in reversed(events):
        if event.event == "Stop":
            return event
        if event.event not in _AT_REST_AFTER:
            return None
    return None


def turn_end(path: Path) -> HookEvent | None:
    """The `Stop` that ended the worker's last turn, while nothing came after
    it but what Claude Code says at rest (#1789, #1810): its text is the end
    of its last message, its time when it came to rest. None: no such turn
    end, or none in the file's end."""
    events = _events(path)
    return None if events is None else _turn_end(events)


def last_word(path: Path) -> HookEvent | None:
    """The event that says where the worker is: its last one, except a
    `SessionStart` after a turn's end — a compaction at rest (#1810), which
    would read « works » — where the turn's `Stop` keeps the word."""
    events = _events(path)
    if not events:
        return None
    if events[-1].event == "SessionStart":
        return _turn_end(events) or events[-1]
    return events[-1]
