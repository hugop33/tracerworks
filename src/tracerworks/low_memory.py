"""The machine's memory low for long (#2096), for the workshop's banner.

On 29/10 the memory available stayed near 1 GB from 21:00 to 07:15 UTC: the
health log wrote `ALERTE` all night, read by no one, and the kernel killed the
runners at 01:31. The streak is read from the history's `vps_sample` (serve's
reading each minute, the health log's each quarter hour); the banner says it
past `[waits] low_memory_minutes`, with the three largest processes now and
the worker each belongs to. Only a process's name is kept, never its command
line, which can carry a secret.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from tracerworks import procfs
from tracerworks.conventions import Conventions
from tracerworks.measures import MEMORY_LOW_MB
from tracerworks.observe import available_memory_mb
from tracerworks.query import QueryError, open_read_only

#: a streak whose latest reading is older than this is no longer known: serve
#: stopped reading, and its last word is not the machine's now
STALE_S = 10 * 60
LARGEST = 3
#: how far back a streak is read: a day low is said as a day
STREAK_LOOKBACK_S = 86400


def short(meminfo: Path) -> int | None:
    """The memory available now when under the alert threshold: a machine's
    red waits for it before its re-run (#2096: on 29/10 each re-run was
    killed again). None when above, or unreadable — never held on a guess."""
    mb = available_memory_mb(meminfo)
    return mb if mb is not None and mb < MEMORY_LOW_MB else None


def low_since(
    samples: Sequence[tuple[float, int]], now: float, threshold_mb: int = MEMORY_LOW_MB
) -> float | None:
    """When the memory available fell under the threshold for the last time
    and stayed there, from (at, MB) readings; None while above, or unknown."""
    ordered = sorted(samples)
    if not ordered or now - ordered[-1][0] > STALE_S:
        return None
    since = None
    for at, mb in ordered:
        since = (since if since is not None else at) if mb < threshold_mb else None
    return since


def streak(path: Path, now: float) -> float | None:
    """`low_since` from the history base; None when absent or unreadable."""
    try:
        db = open_read_only(path)
    except QueryError:
        return None
    with closing(db):
        try:
            rows = db.execute(
                "SELECT at, memory_available_mb FROM vps_sample"
                " WHERE at >= ? AND memory_available_mb IS NOT NULL",
                (int(now - STREAK_LOOKBACK_S),),
            ).fetchall()
        except sqlite3.Error:
            return None
    return low_since(rows, now)


def largest(
    proc: Path, conventions: Conventions, *, page_kb: int | None = None
) -> list[dict[str, Any]]:
    """The largest processes now (resident memory), each with the worker its
    scope says, or None; none when /proc is unreadable."""
    read = procfs.processes(proc, ticks=1, page_kb=page_kb)  # no age read here
    if read is None:
        return []
    _, seen = read
    top = sorted(seen, key=lambda one: (-one.rss_kb, one.pid))[:LARGEST]
    return [
        {
            "pid": one.pid,
            "name": one.name,
            "mb": one.rss_kb // 1024,
            "spec": scope[1]
            if (scope := conventions.spec_of_scope(one.cgroup))
            else None,
        }
        for one in top
    ]
