"""A thin, exact-target wrapper over the `tmux` command line.

Targets are always written `=session:=window`: without the `=`, tmux falls back
to PREFIX matching, and `worker-50` would silently address `worker-501`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tracerworks.process import Result, Runner, checked

WINDOW_FORMAT = "\t".join(
    (
        "#{window_name}",
        "#{pane_current_command}",
        "#{window_activity}",
        "#{@tracerworks_provider}",
    )
)

#: What a tmux client says when its server exits during the exchange (#2221):
#: the server is gone, the workers read as stopped — not a broken factory.
DYING_SERVER = frozenset({"server exited unexpectedly", "server exited"})


@dataclass(frozen=True)
class Window:
    name: str
    command: str  # the pane's foreground process
    activity: int  # epoch seconds of the last output
    provider: str  # the `@tracerworks_provider` option set at spawn ("" if none)


def parse_windows(output: str) -> list[Window]:
    windows = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, command, activity, provider = parts
        windows.append(
            Window(name, command, int(activity) if activity.isdigit() else 0, provider)
        )
    return windows


class Tmux:
    def __init__(self, runner: Runner, session: str, socket: str | None) -> None:
        self._run = runner
        self.session = session
        self._base = ["tmux", "-L", socket] if socket else ["tmux"]

    def target(self, window: str) -> str:
        return f"={self.session}:={window}"

    def _tmux(self, *args: str, stdin: bytes | None = None) -> Result:
        return self._run([*self._base, *args], stdin=stdin)

    def list_windows(self) -> list[Window]:
        """The session's windows; none at all when the server or session is gone.

        After a tmux restart or a reboot this is simply empty — there is
        nothing to reconcile, the workers read as stopped.
        """
        result = self._tmux(
            "list-windows", "-t", f"={self.session}", "-F", WINDOW_FORMAT
        )
        if result.ok:
            return parse_windows(result.stdout)
        error = (result.stderr or result.stdout).strip()
        missing_socket = error.startswith("error connecting to ") and error.endswith(
            " (No such file or directory)"
        )
        # A SERVER DYING WHILE READ (#2221): `kill-server` returns before the
        # server is gone; it accepts the client, then exits under it.
        if (
            missing_socket
            or error in DYING_SERVER
            or error.startswith(("no server running on ", "can't find session:"))
        ):
            return []
        # An unreadable socket is not an empty factory: admission must stop.
        checked(result, "tmux list-windows")
        return []  # checked raised on the failed result

    def capture(self, window: str, history: int = 0, escapes: bool = False) -> str:
        """The pane's text; with `escapes`, its colours and attributes too (`-e`)."""
        args = ["capture-pane", "-p", "-t", self.target(window)]
        if history:
            args[2:2] = ["-S", f"-{history}"]
        if escapes:
            args[2:2] = ["-e"]
        return checked(self._tmux(*args), f"tmux capture-pane {window}")

    def try_capture(self, window: str, escapes: bool = False) -> str | None:
        """The visible pane, or None if the window vanished between two calls;
        with `escapes`, its colours and attributes too (`-e`)."""
        flags = ("-p", "-e") if escapes else ("-p",)
        result = self._tmux("capture-pane", *flags, "-t", self.target(window))
        return result.stdout if result.ok else None

    def paste(self, window: str, data: bytes, buffer: str) -> None:
        """Load bytes from stdin into a named buffer and paste them, bracketed.

        `load-buffer -` reads stdin: the text never becomes an argument, let
        alone a shell word. `paste-buffer -p` wraps it in bracketed paste so
        its newlines do not submit it in pieces; `-d` deletes the buffer.
        """
        checked(
            self._tmux("load-buffer", "-b", buffer, "-", stdin=data), "tmux load-buffer"
        )
        checked(
            self._tmux(
                "paste-buffer", "-p", "-d", "-b", buffer, "-t", self.target(window)
            ),
            "tmux paste-buffer",
        )

    def send_keys(self, window: str, *keys: str, literal: bool = False) -> None:
        flags = ["-l"] if literal else []
        checked(
            self._tmux("send-keys", "-t", self.target(window), *flags, *keys),
            "tmux send-keys",
        )

    def new_window(self, window: str, cwd: Path, argv: list[str]) -> None:
        """Open a detached window running `argv` directly (no `sh -c`).

        `-d` so that an attached client (the operator's) never jumps to it.
        """
        checked(
            self._tmux(
                "new-window",
                "-d",
                "-t",
                f"={self.session}:",
                "-n",
                window,
                "-c",
                str(cwd),
                "--",
                *argv,
            ),
            "tmux new-window",
        )

    def kill_window(self, window: str) -> None:
        checked(
            self._tmux("kill-window", "-t", self.target(window)), "tmux kill-window"
        )
