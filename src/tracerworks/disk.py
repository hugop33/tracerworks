"""How much disk a worktree takes: `du -sk`, fail-soft.

`du` counts a hard-linked inode once per invocation (a `.venv` built by `uv`
is hard links into its cache) and does not follow symlinks — what the
operator asked for (26/09). Any failure is an unknown size, never an error:
the size informs, it guards nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tracerworks import texts
from tracerworks.process import Runner

KB_PER_MB = 1024
KB_PER_GB = 1024 * 1024


def size_kb(runner: Runner, path: Path) -> int | None:
    result = runner(["du", "-sk", str(path)])
    first = result.stdout.split(maxsplit=1)[:1]
    if not result.ok or not first or not first[0].isdigit():
        return None
    return int(first[0])


@dataclass(frozen=True)
class Filesystem:
    available_kb: int
    total_kb: int
    used_percent: float  # `df`'s own: used / (used + available)


def filesystem(runner: Runner, path: Path) -> Filesystem | None:
    """`df -Pk`: the filesystem `path` lives on; None on any failure."""
    result = runner(["df", "-Pk", str(path)])
    lines = result.stdout.splitlines()
    fields = lines[1].split() if result.ok and len(lines) > 1 else []
    if len(fields) < 4 or not all(f.isdigit() for f in fields[1:4]):
        return None
    total, used, available = (int(f) for f in fields[1:4])
    if used + available == 0:
        return None
    return Filesystem(available, total, round(used / (used + available) * 100, 1))


def describe_size(kb: int | None) -> str:
    """« 0.9 GB » from half a gigabyte on, « 312 MB » below, « ? » unknown."""
    if kb is None:
        return "?"
    if kb >= KB_PER_GB // 2:  # from 0.5 GB on, in GB
        return texts.SIZE_GB.format(gb=kb / KB_PER_GB)
    return texts.SIZE_MB.format(mb=kb // KB_PER_MB)
