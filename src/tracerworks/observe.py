"""The state of the workers, rebuilt from the world at each call (architecture §6.3).

A worker is the union of what the conventions find: a window `worker-<n>` in
the tmux session and/or a worktree `<root>/<n>`. Its state is deduced, never
stored:

    working   window present, provider alive, the interface says it is busy
    waiting   window present, provider alive, input field at rest
    asking    window present, provider alive, stopped for someone: a
              permission prompt, a question, an expired login, a limit
    stopped   worktree present, no window
    dead      window present, provider gone

A CLAUDE WORKER SAYS IT ITSELF (#1728): its hooks write its last word to its
state file (`hook_state`), and that word is read first. The screen is read
only without one — a worker launched before its hooks, a provider without
them, a session that has just ended — and to say what a permission prompt
asks, which the screen says better than the notification.

So after a crash, a tmux restart or a reboot there is nothing to reconcile:
the next `status` sees the new reality.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from tracerworks import texts
from tracerworks.git import (
    Saved,
    Worktree,
    last_commit_time,
    parse_worktree_list,
    saved_state,
)
from tracerworks.hook_state import HookEvent, hook_file, last_word, turn_end
from tracerworks.process import Runner
from tracerworks.providers import CLAUDE, PROVIDERS, Provider, provider_of_process
from tracerworks.runtime.tmux import Tmux, Window
from tracerworks.settings import Settings

#: How many lines above the input field the spinner may sit (tips and the
#: effort line come between).
SPINNER_LINES = 8
#: Without a visible input field, the status bar is looked for in the last
#: lines only: the conversation may quote « esc to interrupt » (this module does).
STATUS_LINES = 4


class WorkerState(enum.Enum):
    WORKING = "working"
    WAITING = "waiting"
    ASKING = "asking"  # a permission prompt waits for an answer (#984)
    STOPPED = "stopped"
    DEAD = "dead"


@dataclass(frozen=True)
class Worker:
    spec: int
    state: WorkerState
    provider: str | None
    window: Window | None
    worktree: Worktree | None
    saved: Saved | None  # None without a worktree, or when git cannot read it
    last_activity: int | None  # epoch seconds
    slug: str | None = None  # from the branch name, when it follows the convention
    asking: str | None = None  # what it waits for, while ASKING
    asking_kind: str | None = None  # permission, question, login or limit
    #: at rest, its last turn ended on the memory marker (#1730)
    waits_for_memory: bool = False
    #: at rest, the end of its last turn — its words, its time (#1789)
    rest: HookEvent | None = None
    #: at rest, a text left in its input field, never submitted (#2003)
    pending: str | None = None

    @property
    def running(self) -> bool:
        """Occupies a slot: its window exists (a dead one still needs a look)."""
        return self.window is not None


def bottom_lines(pane: str, count: int = STATUS_LINES) -> list[str]:
    return [line for line in pane.splitlines() if line.strip()][-count:]


def is_busy(pane: str, provider: Provider) -> bool:
    lines = [line for line in pane.splitlines() if line.strip()]
    prompt = max(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip().startswith(provider.prompt_glyph)
        ),
        default=None,
    )
    if prompt is None:
        below, above = lines[-STATUS_LINES:], []
    else:
        below, above = (
            lines[prompt + 1 :],
            lines[max(0, prompt - SPINNER_LINES) : prompt],
        )
    if any(provider.status_bar_busy in line for line in below):
        return True
    spinner = provider.spinner_busy
    return spinner is not None and any(re.search(spinner, line) for line in above)


#: A permission prompt sits at the bottom of the window: its question no
#: further up than this many lines (its options and footer come under it).
PROMPT_LINES = 12
_QUESTION = re.compile(r"^\s*Do you want to .+\?\s*$")
_FIRST_OPTION = re.compile(r"^\s*(?:\S\s+)?1\.\s")
_INPUT_FIELD = re.compile(rf"^\s*{re.escape(CLAUDE.prompt_glyph)}(?!\s*\d+\.)")
_RULE = re.compile(r"^\s*─{8,}\s*$")
#: Claude Code 2.1.310 (24/10, #1728): the command sits between dashed rules,
#: under a tip and a description of it.
_DASHED = re.compile(r"^\s*╌{8,}\s*$")
_TIP = re.compile(r"^\s*Tip:")


def permission_request(pane: str) -> str | None:
    """What a Claude Code permission prompt asks — « Bash command: rm x » —
    or None when the window shows none (#984).

    Seen on 07/10 on worker-976 (no capture kept; the test screen follows
    Claude Code's layout): below a rule, a title (« Bash command »), the
    request, then « Do you want to proceed? » and numbered options. Its first
    option, behind the input glyph, looks like a field at rest: without this
    reading the worker passed for « waiting ». Only the bottom of the window
    counts, and never above an input field — a conversation may quote one.
    Since 2.1.310 a tip and a description come first, the command framed by
    dashed rules: the command is the request (screen of 24/10, kept).
    """
    lines = [line for line in pane.splitlines() if line.strip()]
    bottom = len(lines) - PROMPT_LINES
    question = next(
        (
            i
            for i in range(len(lines) - 1, max(bottom, 0) - 1, -1)
            if _QUESTION.match(lines[i])
        ),
        None,
    )
    if question is None or not any(
        _FIRST_OPTION.match(line) for line in lines[question + 1 : question + 3]
    ):
        return None
    if any(_INPUT_FIELD.match(line) for line in lines[question + 1 :]):
        return None  # an input field under it: the prompt is only quoted
    top = max(question - PROMPT_LINES, 0)
    rule = next(
        (i for i in range(question - 1, top - 1, -1) if _RULE.match(lines[i])), None
    )
    if rule is None:  # no frame: its nearest line
        return lines[question - 1].strip() if question else ""
    body = [line.strip() for line in lines[rule + 1 : question] if not _TIP.match(line)]
    framed = [i for i, line in enumerate(body) if _DASHED.match(line)]
    if framed and framed[0] + 1 < len(body):
        return f"{body[0]}: {body[framed[0] + 1]}"
    if len(body) < 2:
        return body[0] if body else ""
    return f"{body[0]}: {body[1]}"


_NBSP = "\xa0"


_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def plain(pane: str) -> str:
    """A pane captured with `-e`, without its escape sequences."""
    return _OTHER_ESCAPES.sub("", _SGR.sub("", pane))


def undimmed(line: str) -> str:
    """The line without its DIM spans (SGR 2 until 0 or 22), escapes removed.

    Claude Code draws a SUGGESTION in an empty input field, dimmed (measured on
    26/09: « fais un commit wip: et pousse », `ESC[2m`). It is not text anyone
    typed: read as such, it made an empty field look full.
    """
    kept, dim, position = [], False, 0
    for match in _SGR.finditer(line):
        if not dim:
            kept.append(line[position : match.start()])
        codes = [c for c in match.group(1).split(";") if c] or ["0"]
        for code in codes:
            if code == "2":
                dim = True
            elif code in ("0", "22"):
                dim = False
        position = match.end()
    if not dim:
        kept.append(line[position:])
    return _OTHER_ESCAPES.sub("", "".join(kept))


def pending_field(pane: str, provider: Provider) -> str | None:
    """The text sitting in the input field, line by line; "" when empty.

    `pane` may carry escape sequences (`capture -e`): dimmed text — the
    interface's own suggestion — does not count. None when no input line is
    visible at all (the interface is not at its prompt), which is neither
    « received » nor « still there ». A text of several lines goes on under
    the glyph's line, indented, down to the field's rule (Claude Code 2.1.312,
    28/10); a long paste is folded into « [Pasted text #1 +30 lines] », its
    words not on the screen. A glyph's line whose block no rule closes — a
    past prompt in the thread, under a dialog — is no field: None.
    """
    lines = pane.splitlines()
    starts = [
        i
        for i, line in enumerate(lines)
        if plain(line).lstrip().startswith(provider.prompt_glyph)
    ]
    if not starts:
        return None
    first = undimmed(lines[starts[-1]]).lstrip()[len(provider.prompt_glyph) :]
    field = [first]
    for line in lines[starts[-1] + 1 :]:
        if _RULE.match(plain(line)):
            text = "\n".join(line.replace(_NBSP, " ").strip() for line in field)
            return text.strip()
        if not plain(line).startswith(" "):
            break
        field.append(undimmed(line))
    return None


def input_field(pane: str, provider: Provider) -> str | None:
    """The text still sitting in the input field on one line, "" when it is
    empty, None when no input line is visible at all.

    Read from the last glyph's line alone when no rule closes it (the
    screens recorded before #2003); `pending_field`'s lines when one does.
    """
    text = pending_field(pane, provider)
    if text is not None:
        return re.sub(r"\s+", " ", text).strip()
    lines = [
        undimmed(line)
        for line in pane.splitlines()
        if plain(line).lstrip().startswith(provider.prompt_glyph)
    ]
    if not lines:
        return None
    text = lines[-1].lstrip()[len(provider.prompt_glyph) :]
    return re.sub(r"\s+", " ", text.replace(_NBSP, " ")).strip()


#: How Claude Code folds a long paste in its field: its words are not shown.
FOLDED_PASTE = re.compile(r"\[Pasted text #\d+")


def worker_provider(window: Window) -> Provider | None:
    """The provider the window was started with, else the one running in it."""
    return PROVIDERS.get(window.provider) or provider_of_process(window.command)


#: The hooks' events after which the worker works.
_WORKING_EVENTS = ("UserPromptSubmit", "PostToolUse", "SessionStart")
_QUESTIONS = ("elicitation_dialog", "agent_needs_input")


class Reading(NamedTuple):
    """A worker's state; while ASKING, what it waits for in words and its
    kind — permission, question, login, limit — which no translation of the
    words changes (#1730: the gesture told on GitHub follows the kind)."""

    state: WorkerState
    waits: str | None = None
    kind: str | None = None


def _permission(request: str) -> Reading:
    words = texts.WAITS_PERMISSION.format(request=request)
    return Reading(WorkerState.ASKING, words, "permission")


def hook_reading(event: HookEvent | None) -> Reading | None:
    """What a worker's last hook event says of it, and what it waits for;
    None when it says nothing the screen should not say instead."""
    if event is None:
        return None
    text = event.text or "?"
    asking = WorkerState.ASKING
    if event.event == "Notification" and event.kind == "permission_prompt":
        return _permission(text)
    if event.event == "Notification" and event.kind in _QUESTIONS:
        return Reading(asking, texts.WAITS_QUESTION.format(question=text), "question")
    if event.event == "Notification" and event.kind == "idle_prompt":
        return Reading(WorkerState.WAITING)
    if event.event == "StopFailure" and event.kind == "authentication_failed":
        return Reading(asking, texts.WAITS_LOGIN, "login")
    if event.event == "StopFailure":
        limit = texts.WAITS_LIMIT.format(kind=event.kind or "?")
        return Reading(asking, limit, "limit")
    if event.event == "Stop":
        return Reading(WorkerState.WAITING)
    if event.event in _WORKING_EVENTS:
        return Reading(WorkerState.WORKING)
    return None  # SessionEnd, or an event this reading does not know


def _alive(window: Window) -> Provider | None:
    provider = worker_provider(window)
    if provider is None or window.command not in provider.processes:
        return None
    return provider


def read_worker(
    window: Window | None, pane: str | None, event: HookEvent | None = None
) -> Reading:
    """Its state and, while ASKING, what it waits for: its hooks' last word
    first, the screen without one."""
    if window is None:
        return Reading(WorkerState.STOPPED)
    provider = _alive(window)
    if provider is None:
        return Reading(WorkerState.DEAD)
    on_screen = permission_request(pane) if provider is CLAUDE and pane else None
    heard = hook_reading(event) if provider is CLAUDE else None
    if heard is not None:
        if heard.kind == "permission" and on_screen:
            return _permission(on_screen)  # the screen says it better
        return heard
    if on_screen is not None:
        return _permission(on_screen)
    busy = pane and is_busy(pane, provider)
    return Reading(WorkerState.WORKING if busy else WorkerState.WAITING)


def classify(
    window: Window | None, pane: str | None, event: HookEvent | None = None
) -> WorkerState:
    return read_worker(window, pane, event).state


def list_worktrees(runner: Runner, settings: Settings) -> dict[int, Worktree]:
    return read_worktrees(runner, settings) or {}


def read_worktrees(runner: Runner, settings: Settings) -> dict[int, Worktree] | None:
    """The chantiers' worktrees git lists; None when git could not list them."""
    result = runner(
        ["git", "-C", str(settings.repo_dir), "worktree", "list", "--porcelain"]
    )
    return (
        parse_worktree_list(result.stdout, settings.conventions) if result.ok else None
    )


#: Stopped in its window, waiting for someone: at rest, or on a permission
#: prompt (#984) — a `needs-human` question stays open under a prompt.
HALTED = (WorkerState.WAITING, WorkerState.ASKING)


def resting_specs(settings: Settings, tmux: Tmux) -> set[int]:
    """The specs whose worker is halted in its window (`HALTED`) — tmux
    alone, cheap enough for every minute (the alerts' « blocked », #853)."""
    return {
        spec
        for window in tmux.list_windows()
        if (spec := settings.conventions.spec_of_window(window.name)) is not None
        and classify(
            window,
            tmux.try_capture(window.name),
            last_word(hook_file(settings.cache_dir, spec)),
        )
        in HALTED
    }


def asking_specs(settings: Settings, tmux: Tmux) -> dict[int, str]:
    """The specs whose worker is stopped for someone, with what it waits for
    — tmux and the hooks' files, read each minute by the alerts (#984, #1728)."""
    asking = {}
    for window in tmux.list_windows():
        spec = settings.conventions.spec_of_window(window.name)
        if spec is None:
            continue
        pane = tmux.try_capture(window.name)
        event = last_word(hook_file(settings.cache_dir, spec))
        reading = read_worker(window, pane, event)
        if reading.state is WorkerState.ASKING:
            asking[spec] = reading.waits or ""
    return asking


def observe_workers(
    runner: Runner,
    settings: Settings,
    tmux: Tmux,
    worktrees: dict[int, Worktree] | None = None,
    known_windows: list[Window] | None = None,
) -> list[Worker]:
    """`worktrees`: git's list when the caller already read it."""
    windows = {
        spec: window
        for window in (tmux.list_windows() if known_windows is None else known_windows)
        if (spec := settings.conventions.spec_of_window(window.name)) is not None
    }
    if worktrees is None:
        worktrees = list_worktrees(runner, settings)
    workers = []
    for spec in sorted(windows.keys() | worktrees.keys()):
        window = windows.get(spec)
        worktree = worktrees.get(spec)
        pane = tmux.try_capture(window.name) if window else None
        provider = worker_provider(window) if window else None
        provider_name = (
            provider.name if provider else recorded_provider(settings.cache_dir, spec)
        )
        saved = (
            saved_state(runner, worktree.path, settings.base_ref) if worktree else None
        )
        parsed = (
            settings.conventions.parse_branch(worktree.branch)
            if worktree and worktree.branch
            else None
        )
        if window is not None:
            activity: int | None = window.activity or None
        else:
            activity = last_commit_time(runner, worktree.path) if worktree else None
        path = hook_file(settings.cache_dir, spec)
        event = last_word(path)
        reading = read_worker(window, pane, event)
        rest = turn_end(path) if reading.state is WorkerState.WAITING else None
        pending = (
            _pending(tmux, window, provider)
            if window is not None and reading.state is WorkerState.WAITING
            else None
        )
        workers.append(
            Worker(
                spec=spec,
                state=reading.state,
                provider=provider_name,
                window=window,
                worktree=worktree,
                saved=saved,
                last_activity=activity,
                slug=parsed[1] if parsed else None,
                asking=reading.waits,
                asking_kind=reading.kind,
                waits_for_memory=rest is not None and rest.waits_for_memory,
                rest=rest,
                pending=pending,
            )
        )
    return workers


def _pending(tmux: Tmux, window: Window, provider: Provider | None) -> str | None:
    """A text left in the field of a Claude worker at rest; None: none, or
    unreadable. Read with its colours: a dimmed suggestion is no one's text."""
    if provider is not CLAUDE:
        return None
    pane = tmux.try_capture(window.name, escapes=True)
    return (pending_field(pane, provider) or None) if pane is not None else None


_PRIMER_PROVIDER = re.compile(r"— provider: (\w+)$", re.MULTILINE)


def recorded_provider(cache_dir: Path, spec: int) -> str | None:
    """The provider named in the primer `start` wrote — for a stopped worker,
    which has no window left to ask."""
    try:
        text = (cache_dir / f"primer-{spec}.md").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _PRIMER_PROVIDER.search(text)
    return match.group(1) if match else None


def available_memory_mb(meminfo: Path) -> int | None:
    try:
        text = meminfo.read_text()
    except OSError:
        return None
    match = re.search(r"^MemAvailable:\s+(\d+) kB", text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None
