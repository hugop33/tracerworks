"""A worker at rest that waits on a job of its own in the background (#2175).

Claude Code runs each of its worker's shell commands as a shell (`bash -c
source …/shell-snapshots/…`), a child of its session; one launched « in the
background » stays while the worker, at rest, waits for its notification.
On 31/10 #2131's worker launched a 17-min replay so and went to rest: a
worker at rest with its CI over is parked (`cli._give_slot_back`), and a
park closes its session — the replay with it. So such a job keeps its
worker: up to `[workers] background_max_minutes`, then parked as before.

Read from /proc, as `overdue` reads a worker's tests: the processes of its
systemd scope (`<unit prefix><spec>-<time>.scope`, #1724), the session being
the scope's first. A shell the session started is a background job: Claude
Code's own (its command line sources a `shell-snapshots` file) at any age —
the 31/10 worker went to rest 4 s after its launch —, any other older than
`MIN_AGE_S` (a hook's or the status line's shell lives a moment); an MCP
server (`npm exec …`, `node …`) is none. No scope (a Mac, a worker
launched without one) or /proc unreadable: unknown, and the pane's footer
says it (`cleanup.shells_running`). A command line is matched, never kept:
it can carry a secret.
"""

from __future__ import annotations

from pathlib import Path

from tracerworks import procfs
from tracerworks.conventions import Conventions

#: what Claude Code runs a command in
SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
#: younger, a shell is a hook's or the status line's, not a job
MIN_AGE_S = 30
#: in the command line of the shell Claude Code runs a command in
CLAUDE_SHELL = "shell-snapshots"


def jobs(
    proc: Path,
    conventions: Conventions,
    spec: int,
    *,
    ticks: int | None = None,
    page_kb: int | None = None,
) -> list[float] | None:
    """How long each background job of #spec's worker has run, in seconds;
    None when unknown — /proc unreadable, or no scope of #spec in it."""
    read = procfs.processes(proc, ticks=ticks, page_kb=page_kb)
    if read is None:
        return None
    uptime, seen = read
    scopes: dict[str, list[procfs.Process]] = {}
    for one in seen:
        found_in = conventions.spec_of_scope(one.cgroup)
        if found_in is not None and found_in[1] == spec:
            scopes.setdefault(found_in[0], []).append(one)
    if not scopes:
        return None
    # the live one: an older scope of #spec may outlive its session (31/10:
    # #2131's, until 08:12, beside the one launched at 08:07)
    sessions = [min(m, key=lambda one: (one.started, one.pid)) for m in scopes.values()]
    session = max(sessions, key=lambda one: (one.started, one.pid))
    members = next(m for m in scopes.values() if session in m)
    return sorted(
        age
        for one in members
        if one.parent == session.pid
        and one.name in SHELLS
        and (
            (age := uptime - one.started) >= MIN_AGE_S
            or any(CLAUDE_SHELL in word for word in procfs.argv(proc, one.pid))
        )
    )
