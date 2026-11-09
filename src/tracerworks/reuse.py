"""A light lane's green, reused for the same code diff (#1844, spec #1843).

A commit that does not change a pull request's code — a conflict on a doc
resolved by merging the base, a merge of the base — still has no check of its
own: GitHub attaches checks to a commit. Like Gerrit (votes copied on a
« trivial rebase ») and Chromium (try results reused for an equivalent patch),
the queue lets such a head in on the green light lane of an earlier head of
the same pull request, when:

- **the same code diff**: the pull request's diff from its merge base with
  the base branch, restricted to code — `docs/**`, every `*.md` and
  `[merge_queue] reuse_ignore` left out — has the same `git patch-id`, whitespace
  kept (`--verbatim`: an indent is code in Python);
- **no file changed on both sides**: none of the head's code files was also
  changed by the base between the two merge bases — a « trivial » rebase can
  break through a dependency (Trusted Firmware);
- **a green that counts**: the newest earlier head with the same code diff
  decides — its last CI run finished and green; red, nothing is reused;
  still running, asked again; cancelled (a newer push cut it short), the
  next earlier head is looked at.

The queue stays the net: its full suite runs on the head, brought up to date
with the base, before any merge. `[merge_queue] reuse = false` switches the
rule off.

The diffs are read in the main clone (`repo_dir`), after a fetch of the base
and of the pull request's head; nothing is checked out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tracerworks.process import CommandError, Runner, checked

#: What is never code, whatever `reuse_ignore` adds (git glob pathspecs).
NOT_CODE = ("docs/**", "**/*.md")
#: Earlier heads looked at, newest first: past these, no reuse.
MAX_EARLIER = 10
#: A verdict's reason when git failed: undecided, asked again.
GIT_FAILED = "git: "


@dataclass(frozen=True)
class CodeDiff:
    """A commit's code changes from its merge base with the base branch."""

    base: str  # the merge base
    patch_id: str  # "" for no code change at all
    files: frozenset[str]


@dataclass(frozen=True)
class Earlier:
    """An earlier head of the pull request and its last CI run."""

    sha: str
    finished: bool
    green: bool
    conclusion: str
    url: str


@dataclass(frozen=True)
class Verdict:
    """`reused_from` set: the head enters on that head's green. `decided`
    False: ask again on a later pass (a run still going, git unreadable)."""

    reused_from: str | None
    url: str
    why: str
    decided: bool = True


class Code:
    """The code diffs of the main clone."""

    def __init__(
        self,
        runner: Runner,
        repo_dir: Path,
        remote: str,
        base_branch: str,
        ignore: Sequence[str],
    ) -> None:
        self.run = runner
        self.repo = str(repo_dir)
        self.remote = remote
        self.base = f"refs/remotes/{remote}/{base_branch}"
        self.base_branch = base_branch
        self.pathspec = [
            "--",
            ".",
            *(f":(exclude,glob){p}" for p in (*NOT_CODE, *ignore)),
        ]

    def _git(self, *argv: str, stdin: bytes | None = None) -> str:
        result = self.run(["git", "-C", self.repo, *argv], stdin=stdin)
        return checked(result, f"git {argv[0]}")

    def fetch(self, pull: int) -> None:
        """The base and the pull request's head, as GitHub has them now."""
        self._git(
            "fetch", "--quiet", self.remote,
            f"+refs/heads/{self.base_branch}:{self.base}",
            f"refs/pull/{pull}/head",
        )  # fmt: skip

    def has(self, sha: str) -> bool:
        """`sha` is here, or could be fetched (an earlier head force-pushed
        away is still GitHub's for a while)."""
        probe = ["git", "-C", self.repo, "cat-file", "-e", f"{sha}^{{commit}}"]
        if self.run(probe).ok:
            return True
        return self.run(
            ["git", "-C", self.repo, "fetch", "--quiet", self.remote, sha]
        ).ok

    def diff(self, sha: str) -> CodeDiff:
        base = self._git("merge-base", self.base, sha).strip()
        names = self._git(
            "diff", "--no-renames", "--name-only", base, sha, *self.pathspec
        )
        patch = self._git(
            "diff", "--no-renames", "--no-color", "--binary", base, sha, *self.pathspec
        )
        patch_id = ""
        if patch.strip():
            ids = self._git("patch-id", "--verbatim", stdin=patch.encode("utf-8"))
            patch_id = ids.split()[0] if ids.split() else ""
        return CodeDiff(base, patch_id, frozenset(names.split("\n")) - {""})

    def changed(self, old: str, new: str) -> frozenset[str]:
        """The code files the base changed from `old` to `new`."""
        if old == new:
            return frozenset()
        names = self._git(
            "diff", "--no-renames", "--name-only", old, new, *self.pathspec
        )
        return frozenset(names.split("\n")) - {""}


def judge(code: Code, pull: int, head: str, earlier: Sequence[Earlier]) -> Verdict:
    """Whether `head` enters on an earlier head's green; `earlier` newest
    first. The first one with the same code diff decides. Git failing:
    undecided, asked again."""
    if not earlier:
        return Verdict(None, "", "no earlier head with a CI run")
    try:
        code.fetch(pull)
        now = code.diff(head)
        for before in earlier[:MAX_EARLIER]:
            if not code.has(before.sha):
                continue
            then = code.diff(before.sha)
            if then.patch_id != now.patch_id:
                continue
            short = before.sha[:8]
            if not before.finished:
                return Verdict(None, "", f"the light lane of {short} still runs", False)
            if before.conclusion == "cancelled":
                continue  # a newer push cut it short: it says nothing of the code
            if not before.green:
                why = f"the light lane of {short} ended {before.conclusion or '?'}"
                return Verdict(None, "", why)
            both = now.files & code.changed(then.base, now.base)
            if both:
                files = ", ".join(sorted(both)[:3])
                why = f"code changed by the base and the pull request: {files}"
                return Verdict(None, "", why)
            return Verdict(before.sha, before.url, f"same code diff as {short}")
    except CommandError as exc:
        return Verdict(None, "", f"{GIT_FAILED}{exc}", False)
    return Verdict(None, "", "no earlier head with the same code diff")
