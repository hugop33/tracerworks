"""A worker's test that runs on (#2096), stopped by the relauncher's pass.

On 29/10 a worker's `node --test` grew to 7.1 GB for 9 h: its worker waited
for it all night, and the kernel killed the runners. A process of a worker's
scope (`<unit prefix><spec>-<time>.scope`, #1724) whose command line is one of
`[workers] test_commands`, older than `[workers] test_max_minutes`, is
overdue: its process group is ended, SIGTERM then SIGKILL.

Never the worker's session: the scope's first process is. Never a process out
of a worker's scope: a group that holds the session, or a process outside the
scope, is not ended whole — only the test's processes and what they started,
inside the scope. Before the SIGKILL, /proc is read again: only what is still
overdue is killed, never a pid used again since. Read from /proc; only the
command's entry is kept, never the command line, which can carry a secret.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from tracerworks import procfs
from tracerworks.conventions import Conventions

#: SIGTERM, then SIGKILL this long after for what is left
GRACE_S = 10
#: the signals' senders, a process's and a group's: a test never sends one
#: for real (tests/tracerworks/conftest.py)
SIGNAL_PID: Callable[[int, int], None] = os.kill
SIGNAL_GROUP: Callable[[int, int], None] = os.killpg


#: French, as every instruction to a worker (waits.MEMORY_RESUME).
TOLD = (
    "tracerworks a tué ta commande de test `{command}` (pid {pids}) : elle tournait"
    " depuis {minutes} min et tenait {mb} Mo, au-delà de la limite de {limit} min"
    " (`[workers] test_max_minutes`). Ne la relance pas telle quelle : cherche"
    " pourquoi elle ne finit pas (un minuteur, un serveur ou une promesse jamais"
    " fermés, un test qui attend), corrige, puis relance-la sous un `timeout`."
)


@dataclass(frozen=True)
class Overdue:
    spec: int
    group: int | None  # the process group ended; None: its pids alone
    pids: tuple[int, ...]  # its test processes
    command: str  # the entry of `test_commands` it runs
    minutes: int  # since its oldest test process started
    memory_mb: int  # resident, the group's (or its pids')


#: a program run by one of these is named by the word after it
_INTERPRETER = re.compile(r"python[0-9.]*|node|bun")


def _programs(argv: Sequence[str]) -> list[int]:
    """Where a program stands in a command line: its first word, the script
    an interpreter runs (`python3 /w/.venv/bin/pytest`), a module (`-m
    pytest`). An argument (`grep -r pytest`) is none."""
    if not argv:
        return []
    at = [0]
    if _INTERPRETER.fullmatch(Path(argv[0]).name) and len(argv) > 1:
        at.append(1)
    at += [i + 1 for i, word in enumerate(argv[:-1]) if word == "-m"]
    return at


def command_of(argv: Sequence[str], commands: Sequence[str]) -> str | None:
    """The entry of `commands` the command line runs: its first word as a
    program, the next ones right after it."""
    for entry in commands:
        first, *rest = entry.split()
        for i in _programs(argv):
            if (
                Path(argv[i]).name == first
                and list(argv[i + 1 : i + 1 + len(rest)]) == rest
            ):
                return entry
    return None


def overdue(
    proc: Path,
    conventions: Conventions,
    commands: Sequence[str],
    max_minutes: int,
    *,
    ticks: int | None = None,
    page_kb: int | None = None,
) -> list[Overdue] | None:
    """The workers' overdue tests now, by spec; None when /proc is unreadable."""
    read = procfs.processes(proc, ticks=ticks, page_kb=page_kb)
    if read is None:
        return None
    uptime, seen = read
    scopes: dict[str, list[procfs.Process]] = {}
    specs: dict[str, int] = {}
    for one in seen:
        found_in = conventions.spec_of_scope(one.cgroup)
        if found_in is not None:
            scopes.setdefault(found_in[0], []).append(one)
            specs[found_in[0]] = found_in[1]
    found: list[Overdue] = []
    for scope, members in scopes.items():
        session = min(members, key=lambda one: (one.started, one.pid))
        tests: dict[int, list[tuple[procfs.Process, str]]] = {}
        for one in members:
            command = command_of(procfs.argv(proc, one.pid), commands)
            if command is not None and one is not session:
                tests.setdefault(one.group, []).append((one, command))
        for group, hits in tests.items():
            oldest, command = min(hits, key=lambda hit: (hit[0].started, hit[0].pid))
            minutes = int((uptime - oldest.started) // 60)
            if minutes < max_minutes:
                continue
            whole = [one for one in seen if one.group == group]
            if group != session.group and all(one in members for one in whole):
                held = whole
            else:  # the session's group, or one that leaves the scope
                held = _descendants([hit for hit, _ in hits], members, session)
            found.append(
                Overdue(
                    spec=specs[scope],
                    group=group if held is whole else None,
                    pids=tuple(sorted(one.pid for one in held)),
                    command=command,
                    minutes=minutes,
                    memory_mb=sum(one.rss_kb for one in held) // 1024,
                )
            )
    return sorted(found, key=lambda o: (o.spec, min(o.pids)))


def _descendants(
    roots: list[procfs.Process],
    members: list[procfs.Process],
    session: procfs.Process,
) -> list[procfs.Process]:
    """The test's processes and what they started, inside the scope: a
    pytest's workers, a node's children. Never the session."""
    held = {one.pid: one for one in roots}
    grew = True
    while grew:
        grew = False
        for one in members:
            if one.pid not in held and one.parent in held and one is not session:
                held[one.pid] = one
                grew = True
    return list(held.values())


def stop(
    found: Sequence[Overdue],
    still: Callable[[], Sequence[Overdue] | None],
    *,
    kill: Callable[[int, int], None] | None = None,
    killpg: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """SIGTERM to each, one grace for all, then SIGKILL to what `still` finds
    overdue — a group, a pid read again, never one used again meanwhile; one
    already gone is no failure."""
    kill, killpg = kill or SIGNAL_PID, killpg or SIGNAL_GROUP

    def send(sig: int, targets: Sequence[Overdue]) -> None:
        for one in targets:
            sends = (
                [(killpg, one.group)] if one.group else [(kill, p) for p in one.pids]
            )
            for signal_to, target in sends:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    signal_to(target, sig)

    if not found:
        return
    send(signal.SIGTERM, found)
    sleep(GRACE_S)
    left = still() or []
    survivors = []
    for one in found:
        same = [o for o in left if o.spec == one.spec and o.command == one.command]
        if one.group:
            survivors += [o for o in same if o.group == one.group]
        else:
            pids = {p for o in same for p in o.pids} & set(one.pids)
            if pids:
                survivors.append(replace(one, pids=tuple(sorted(pids))))
    send(signal.SIGKILL, survivors)
