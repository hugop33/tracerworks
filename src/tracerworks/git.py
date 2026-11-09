"""What `git` says about the worktrees: whose they are, and what is unsaved."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tracerworks.conventions import Conventions
from tracerworks.process import Runner


@dataclass(frozen=True)
class Worktree:
    spec: int
    path: Path
    branch: str | None  # None: detached HEAD


def parse_worktree_list(
    porcelain: str, conventions: Conventions
) -> dict[int, Worktree]:
    """`git worktree list --porcelain` → the worktrees that follow the convention."""
    found: dict[int, Worktree] = {}
    for block in porcelain.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields:
            continue
        path = Path(fields["worktree"])
        spec = conventions.spec_of_worktree(path)
        if spec is None:
            continue
        ref = fields.get("branch", "")
        branch = ref.removeprefix("refs/heads/") if ref else None
        found[spec] = Worktree(spec, path, branch)
    return found


@dataclass(frozen=True)
class Saved:
    """How much of a worktree's work would be lost with the worktree.

    `unpushed` counts commits the remote does not have: ahead of the upstream,
    or — for a branch never pushed — ahead of the remote base branch. `dirty` counts
    changed, staged AND untracked paths: an untracked new file is work too.
    """

    upstream: str | None
    unpushed: int
    dirty: int

    @property
    def everything_pushed(self) -> bool:
        return self.unpushed == 0 and self.dirty == 0


_AB = re.compile(r"^# branch\.ab \+(\d+) -(\d+)$", re.MULTILINE)
_UPSTREAM = re.compile(r"^# branch\.upstream (\S+)$", re.MULTILINE)


def parse_status(porcelain_v2: str) -> tuple[str | None, int | None, int]:
    """`git status --porcelain=v2 --branch` → (upstream, ahead, dirty paths)."""
    upstream = _UPSTREAM.search(porcelain_v2)
    ab = _AB.search(porcelain_v2)
    dirty = sum(
        1 for line in porcelain_v2.splitlines() if line and not line.startswith("#")
    )
    return (
        upstream.group(1) if upstream else None,
        int(ab.group(1)) if ab else None,
        dirty,
    )


def saved_state(runner: Runner, worktree: Path, base_ref: str) -> Saved | None:
    """None when git cannot read the worktree — never « all pushed » by default."""
    status = runner(
        ["git", "-C", str(worktree), "status", "--porcelain=v2", "--branch"]
    )
    if not status.ok:
        return None
    upstream, ahead, dirty = parse_status(status.stdout)
    if upstream is not None and ahead is not None:
        return Saved(upstream, ahead, dirty)
    # No upstream (never pushed) or an upstream that is gone: what the
    # remote's base branch does not have is what a lost worktree would take with it.
    count = runner(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{base_ref}..HEAD"]
    )
    if not count.ok or not count.stdout.strip().isdigit():
        return None
    return Saved(None, int(count.stdout.strip()), dirty)


def parse_dirty(porcelain_z: str) -> list[str]:
    """`git status --porcelain=v1 -z` → the paths changed, staged or
    untracked; a rename or a copy says its new path (its old one follows)."""
    paths: list[str] = []
    entries = iter(porcelain_z.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            next(entries, None)  # its old path
    return paths


def dirty_paths(runner: Runner, worktree: Path) -> list[str] | None:
    """What a worktree has not committed (#2175: a resumed worker is told
    of it); None when git cannot read it."""
    status = runner(["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"])
    return parse_dirty(status.stdout) if status.ok else None


_TICKET_REF = re.compile(r"\(#(\d+)\)")


def pushed_tickets(runner: Runner, worktree: Path, base_ref: str) -> set[int]:
    """The tickets a pushed commit of the branch names — « … (#1728) », the
    commit of a ticket (#1729). Unreadable, or nothing pushed: none, so that
    a fraction never counts a ticket no commit proves."""
    result = runner(
        ["git", "-C", str(worktree), "log", "--format=%s", f"{base_ref}..@{{upstream}}"]
    )
    if not result.ok:
        return set()
    return {int(n) for n in _TICKET_REF.findall(result.stdout)}


def last_commit_time(runner: Runner, worktree: Path) -> int | None:
    result = runner(["git", "-C", str(worktree), "log", "-1", "--format=%ct"])
    text = result.stdout.strip()
    return int(text) if result.ok and text.isdigit() else None
