"""The processes as /proc says them, read one way by the passes that look at
them (#2096): a worker's overdue test, the banner's largest processes. A
process gone or garbled while read is skipped, never a failure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int  # its process group
    name: str  # its own choice of bytes: a name, never its command line
    started: float  # seconds since boot
    rss_kb: int  # resident memory
    cgroup: str  # its unified cgroup's path (`0::…`)


def _read(here: Path, ticks: int, page_kb: int) -> Process | None:
    try:
        stat = here.joinpath("stat").read_text(encoding="utf-8", errors="replace")
        cgroup = here.joinpath("cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # gone while read
    # the name sits in parentheses and may hold spaces or parentheses itself
    opened, closed = stat.find("("), stat.rfind(")")
    fields = stat[closed + 2 :].split()
    path = next((ln[3:] for ln in cgroup.splitlines() if ln.startswith("0::")), "")
    try:
        return Process(
            pid=int(here.name),
            parent=int(fields[1]),
            group=int(fields[2]),
            name=stat[opened + 1 : closed],
            started=int(fields[19]) / ticks,
            rss_kb=int(fields[21]) * page_kb,
            cgroup=path,
        )
    except (IndexError, ValueError):
        return None


def processes(
    proc: Path, *, ticks: int | None = None, page_kb: int | None = None
) -> tuple[float, list[Process]] | None:
    """(seconds since boot, every process readable); None when /proc is not."""
    ticks = os.sysconf("SC_CLK_TCK") if ticks is None else ticks
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024 if page_kb is None else page_kb
    try:
        uptime = float(proc.joinpath("uptime").read_text().split()[0])
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except (OSError, IndexError, ValueError):
        return None
    found = (_read(entry, ticks, page_kb) for entry in entries)
    return uptime, [one for one in found if one is not None]


def argv(proc: Path, pid: int) -> tuple[str, ...]:
    """Its command line, word by word; none when gone. Read to be matched,
    never kept: it can carry a secret."""
    try:
        raw = proc.joinpath(str(pid), "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(word.decode(errors="replace") for word in raw.split(b"\0") if word)
