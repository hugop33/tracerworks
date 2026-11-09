"""The one door to the outside world: running a program with an argument list.

Every call to `gh`, `git` and `tmux` goes through a `Runner`, as an argument
LIST — never a shell line. That is the whole defence of spec #565 against the
incident of #514 (V33): no text handed to `tracerworks` is ever parsed by a shell,
so `$(…)`, backticks, apostrophes and `$HOME` are just bytes.

Tests pass a fake runner that answers from recorded outputs, so every state is
reachable without tmux, git or the network.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(RuntimeError):
    """A command that had to succeed did not; the message carries its stderr."""


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> Result: ...


def run_process(
    argv: Sequence[str], *, cwd: Path | None = None, stdin: bytes | None = None
) -> Result:
    """The real runner. A missing program is an answer (127), not a crash."""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return Result(127, "", str(exc))
    return Result(
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def one_at_a_time(runner: Runner, program: str) -> Runner:
    """`runner`, with `program`'s calls one after the other (#2195): one
    `gh` costs 45 to 55 MB, and `serve`'s threads ran up to five at once
    against its 200 MB ceiling. The other programs run freely."""
    gate = threading.Lock()

    def run(
        argv: Sequence[str], *, cwd: Path | None = None, stdin: bytes | None = None
    ) -> Result:
        if argv and argv[0] == program:
            with gate:
                return runner(argv, cwd=cwd, stdin=stdin)
        return runner(argv, cwd=cwd, stdin=stdin)

    return run


#: What makes a `gh api` call something other than a read.
_WRITING = frozenset(
    {"-X", "--method", "-f", "--raw-field", "-F", "--field", "--input"}
)


def _gh_api_read(argv: Sequence[str]) -> bool:
    return (
        len(argv) > 2
        and argv[0] == "gh"
        and argv[1] == "api"
        and not any(a in _WRITING or a.startswith(("-X", "--method=")) for a in argv)
    )


#: `gh <noun> <verb>` that only read (`gh issue list`, `gh pr view`…).
_READING_VERBS = frozenset({"list", "view", "status", "checks", "diff"})


def _gh_read(argv: Sequence[str]) -> bool:
    return _gh_api_read(argv) or (
        len(argv) > 2 and argv[1] != "api" and argv[2] in _READING_VERBS
    )


def read_once(runner: Runner, clock: Callable[[], float], window: float) -> Runner:
    """`runner`, with each `gh api` read answered once per `window` seconds,
    whichever thread asks (#2196): `serve`'s snapshot read the latest runs
    twice and the runners twice, its history the runs again, every minute.
    A failed read is not kept; the same read under way in another thread is
    waited for, not asked again. Other `gh` reads (`gh issue list`…) go
    through; any other `gh` call — a write, or one this cannot tell from a
    write — goes through and forgets every read kept."""
    kept: dict[tuple[str, ...], tuple[float, Result]] = {}
    asking: dict[tuple[str, ...], threading.Event] = {}  # a read under way
    lock = threading.Lock()

    def run(
        argv: Sequence[str], *, cwd: Path | None = None, stdin: bytes | None = None
    ) -> Result:
        if not argv or argv[0] != "gh":
            return runner(argv, cwd=cwd, stdin=stdin)
        if not _gh_api_read(argv) or stdin:
            if not _gh_read(argv) or stdin:
                with lock:
                    kept.clear()
            return runner(argv, cwd=cwd, stdin=stdin)
        key = tuple(argv)
        while True:
            with lock:
                now = clock()
                for old in [k for k, (at, _) in kept.items() if now - at >= window]:
                    del kept[old]  # bounded by the reads of one window
                if key in kept:
                    return kept[key][1]
                under_way = asking.get(key)
                if under_way is None:
                    done = asking[key] = threading.Event()
                    break
            under_way.wait()  # the same read, asked by another thread
            with lock:
                if key not in kept:  # it failed: this one asks for itself
                    if key in asking:
                        continue
                    done = asking[key] = threading.Event()
                    break
        try:
            result = runner(argv, cwd=cwd, stdin=stdin)
            if result.ok:
                with lock:
                    kept[key] = (now, result)
            return result
        finally:
            with lock:
                del asking[key]
            done.set()

    return run


def checked(result: Result, what: str) -> str:
    """The stdout of a command that must succeed, or a loud `CommandError`."""
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()
        raise CommandError(f"{what} failed (exit {result.returncode}): {detail}")
    return result.stdout
