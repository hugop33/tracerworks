"""The strays: a process of tracerworks's user that holds a CPU core outside the
CI, the services and the workers.

On 16/10 a diagnostic sent by SSH (a recursive glob over ~/.local) went on
alone once its session closed, and held one core for twenty minutes while the
CI's timing tests went red. Nothing watched for it: the CI and the services
live in the user's systemd manager, the workers under the tmux server; what is
left — a process of a login session outside tmux — is nobody's.

Read from /proc each minute, for the alerts. The criterion is an average since
the process started: one that spins from its start (the 16/10 glob) is seen
after 10 min; one asleep for hours before spinning is not — a known blind spot.
A long legitimate job sent by SSH (an rsync, a dump) is told too, on purpose:
the operator knows it is his. Only the process name is kept, never its command
line: a command line can carry a secret.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Held this long from its start…
STRAY_AFTER = 10 * 60
#: …at this share of one core on average, a process is a stray.
STRAY_ABOVE = 50.0

_SESSION = re.compile(r"/user-(\d+)\.slice/session-\d+\.scope$")
#: The workers' parent: their panes' processes descend from it.
_TMUX_SERVER = "tmux: server"


@dataclass(frozen=True)
class Stray:
    pid: int
    name: str
    percent: int  # of one core, on average since it started
    minutes: int  # since it started


@dataclass(frozen=True)
class _Seen:
    pid: int
    parent: int
    name: str
    started: float  # seconds since boot
    cpu: float  # seconds used


def _text(path: Path) -> str:
    # a name is any bytes the process chose: never a reason to read nothing
    return path.read_text(encoding="utf-8", errors="replace")


def _read(here: Path, uid: int, ticks: int) -> _Seen | None:
    """One process of `uid` in a login session; None for any other, or one
    gone or garbled while read."""
    try:
        cgroup = _text(here / "cgroup")
        scope = next(
            (line[3:] for line in cgroup.splitlines() if line.startswith("0::")), ""
        )
        session = _SESSION.search(scope)
        if session is None or int(session.group(1)) != uid:
            return None  # the CI, the containers, the services, another user's
        owner = re.search(r"^Uid:\s+(\d+)", _text(here / "status"), re.MULTILINE)
        if owner is None or int(owner.group(1)) != uid:
            return None  # in our session but not ours: its sshd, as root
        stat = _text(here / "stat")
    except OSError:
        return None
    # the name sits in parentheses and may hold spaces or parentheses itself
    opened, closed = stat.find("("), stat.rfind(")")
    fields = stat[closed + 2 :].split()
    if opened < 0 or closed < opened or len(fields) < 20:
        return None
    try:
        return _Seen(
            pid=int(here.name),
            parent=int(fields[1]),
            name=stat[opened + 1 : closed],
            started=int(fields[19]) / ticks,
            cpu=(int(fields[11]) + int(fields[12])) / ticks,  # utime + stime
        )
    except ValueError:
        return None


def _workers(seen: list[_Seen]) -> set[int]:
    """The pids that descend from a tmux server, the server included."""
    parent = {one.pid: one.parent for one in seen}
    servers = {one.pid for one in seen if one.name == _TMUX_SERVER}
    workers: set[int] = set()
    for one in seen:
        chain: list[int] = []
        pid = one.pid
        while pid in parent and pid not in chain:  # a loop never hangs the cycle
            if pid in servers or pid in workers:
                workers.update(chain, [pid])
                break
            chain.append(pid)
            pid = parent[pid]
    return workers


def strays(
    proc: Path = Path("/proc"),
    *,
    uid: int | None = None,
    ticks: int | None = None,
) -> list[Stray] | None:
    """The strays now, by pid; None when /proc cannot be read."""
    uid = os.getuid() if uid is None else uid
    ticks = os.sysconf("SC_CLK_TCK") if ticks is None else ticks
    try:
        uptime = float(proc.joinpath("uptime").read_text().split()[0])
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except (OSError, IndexError, ValueError):
        return None
    seen = [one for one in (_read(e, uid, ticks) for e in entries) if one is not None]
    workers = _workers(seen)
    found: list[Stray] = []
    for one in seen:
        age = uptime - one.started
        if one.pid in workers or age < STRAY_AFTER:
            continue
        percent = one.cpu / age * 100
        if percent >= STRAY_ABOVE:
            found.append(Stray(one.pid, one.name, round(percent), int(age // 60)))
    return sorted(found, key=lambda stray: stray.pid)
