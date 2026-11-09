"""The naming conventions that let the state be rebuilt instead of stored.

`<n>` is the number of the spec (the chantier). A worker is visible to
`tracerworks` only through these three names; one started by hand under other
names is invisible to it, deliberately. The prefixes and the root come from
the project file (`settings.py`):

    tmux window   <window_prefix><n>
    worktree      <worktree_root>/<n>
    branch        <branch_prefix><n>-<slug>, created from the base branch
    scratch       <worktree_root>/<n>/<scratch_dir>, the worker's TMPDIR
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SLUG_MAX_LENGTH = 40

#: The worker's temporary folder when the project file leaves it out (#1554).
SCRATCH_DIR = ".tmp"
#: A worker's systemd scope's first word when the project file leaves it out.
SCOPE_PREFIX = "tracerworks-"
_NUMBER = r"([1-9][0-9]*)"
_WORKTREE_DIR = re.compile(r"[1-9][0-9]*")
# « Spec — », « Spec : », « Spec - » : the prefix `to-spec` puts on every spec
# title says nothing about the chantier and would eat the slug's 40 characters.
# It also makes a spec of an issue no nature label names (`natures.py`, #857).
SPEC_PREFIX = re.compile(r"^\s*spec\s*[\u2014:\-\u2013]\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Conventions:
    window_prefix: str
    branch_prefix: str
    worktree_root: Path
    # A name the repository ignores: `git worktree remove` without `--force`
    # takes it with the worktree, drafts included (#1554).
    scratch_dir: str = SCRATCH_DIR
    # What a worker's systemd scope begins with, before the window prefix
    # (`[workers] scope_prefix`, #2350): the project's, not the tool's.
    scope_prefix: str = SCOPE_PREFIX

    def window_name(self, spec: int) -> str:
        return f"{self.window_prefix}{spec}"

    @property
    def unit_prefix(self) -> str:
        """A worker's systemd scope begins so (#1724), its spec and launch
        time after: `tracerworks-worker-600-1761250000.scope`."""
        return f"{self.scope_prefix}{self.window_prefix}"

    def unit_name(self, spec: int) -> str:
        return f"{self.unit_prefix}{spec}"

    def spec_of_scope(self, cgroup: str) -> tuple[str, int] | None:
        """(its worker's scope, its spec) for a process in one, from its
        cgroup's path (#2096); None for any other process."""
        scope = rf"/{re.escape(self.unit_prefix)}{_NUMBER}-\d+\.scope$"
        match = re.search(scope, cgroup)
        return (match.group(0), int(match.group(1))) if match else None

    def spec_of_window(self, name: str) -> int | None:
        match = re.fullmatch(re.escape(self.window_prefix) + _NUMBER, name)
        return int(match.group(1)) if match else None

    def worktree_path(self, spec: int) -> Path:
        return self.worktree_root / str(spec)

    def scratch_path(self, spec: int) -> Path:
        """The worker's TMPDIR: its drafts leave with its worktree."""
        return self.worktree_path(spec) / self.scratch_dir

    def spec_of_worktree(self, path: Path) -> int | None:
        """The spec a worktree belongs to, if it sits where the convention puts it."""
        if path.parent != self.worktree_root or not _WORKTREE_DIR.fullmatch(path.name):
            return None
        return int(path.name)

    def branch_name(self, spec: int, slug: str) -> str:
        return f"{self.branch_prefix}{spec}-{slug}"

    def branch_pattern(self, spec: int) -> str:
        """The glob `git` matches this spec's branches with."""
        return f"{self.branch_prefix}{spec}-*"

    def parse_branch(self, branch: str) -> tuple[int, str] | None:
        """`<prefix>501-console-operateur` → (501, "console-operateur")."""
        match = re.fullmatch(
            re.escape(self.branch_prefix) + _NUMBER + r"-([a-z0-9][a-z0-9-]*)", branch
        )
        return (int(match.group(1)), match.group(2)) if match else None


def slugify(title: str) -> str:
    """An ASCII, lowercase, dash-separated slug of an issue title.

    Accents are folded (« Clients » and « opérateur » stay readable) and the
    slug is cut on a word boundary. A title with nothing sluggable left gives
    a neutral word, never an empty slug: `<prefix>501-` would not parse back.
    """
    title = SPEC_PREFIX.sub("", title)
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", folded.lower())
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > SLUG_MAX_LENGTH:
            break
        slug = candidate
    if not slug and words:
        slug = words[0][:SLUG_MAX_LENGTH]
    return slug or "spec"
