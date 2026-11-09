"""Creating the conventional worktree and branch of a chantier — never deleting one.

    new chantier      a branch <prefix><n>-<slug> from the remote base branch, untracked
    branch on remote  checked out, tracking the remote (a resumed chantier)
    worktree exists   reused as is: that is how a stopped chantier resumes
    branch merged     never resumed: a new branch <prefix><n>-<slug>-r<k> from the
                      base, at the first free rank (a relaunched spec, #1567)

Each worktree prepared, new or reused, gets the repository's hooks (#1703):
`core.hooksPath` in its OWN configuration (`extensions.worktreeConfig`), so a
worker's push runs the quick checks first, and the main clone is left as it is.

A branch whose pull request is merged is done: on 08/10 the reopened spec
#1021 resumed its merged branch, and `clean` closed it again at once. It is
left out of the choice and of the « several branches » refusal, and kept as
it is. A worktree still on disk is reused as is: `clean` removes a merged
one first, and `start-next` never takes a chantier that still has one.

Nothing here resets, cleans or removes: work that is not pushed lives only in
the worktree (architecture §7.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tracerworks import texts
from tracerworks.cleanup import merged_pull_request
from tracerworks.conventions import Conventions
from tracerworks.errors import Refused
from tracerworks.git import Worktree
from tracerworks.process import Runner, checked
from tracerworks.settings import Settings


@dataclass(frozen=True)
class Prepared:
    path: Path
    branch: str
    resuming: bool  # the worktree or the branch already existed


def _branches(output: str, spec: int, conventions: Conventions) -> list[str]:
    found = []
    for line in output.splitlines():
        name = line.split()[-1].removeprefix("refs/heads/") if line.strip() else ""
        parsed = conventions.parse_branch(name)
        if parsed and parsed[0] == spec:
            found.append(name)
    return found


def _free_name(name: str, merged: set[str]) -> str:
    """The conventional name; after merged branches (a relaunched spec), the
    same with `-r<k>` at its first free rank, from 2."""
    if not merged:
        return name
    rank = 2
    while f"{name}-r{rank}" in merged:
        rank += 1
    return f"{name}-r{rank}"


def prepare(
    runner: Runner,
    settings: Settings,
    spec: int,
    slug: str,
    existing: Worktree | None,
) -> Prepared:
    repo, remote_name = str(settings.repo_dir), settings.remote
    conventions = settings.conventions
    if existing is not None:
        wire_hooks(runner, settings, existing.path)
        return Prepared(existing.path, existing.branch or "HEAD", resuming=True)
    path = conventions.worktree_path(spec)
    if path.exists():
        raise Refused(texts.NOT_A_WORKTREE.format(path=path, repo=repo))
    checked(runner(["git", "-C", repo, "fetch", "--quiet", remote_name]), "git fetch")
    pattern = conventions.branch_pattern(spec)
    remote = _branches(
        checked(
            runner(["git", "-C", repo, "ls-remote", "--heads", remote_name, pattern]),
            "git ls-remote",
        ),
        spec,
        conventions,
    )
    local = _branches(
        checked(
            runner(
                ["git", "-C", repo, "branch", "--list", "--format=%(refname)", pattern]
            ),
            "git branch --list",
        ),
        spec,
        conventions,
    )
    known = set(remote) | set(local)
    done = {b for b in known if merged_pull_request(runner, settings.github_repo, b)}
    remote = [b for b in remote if b not in done]
    local = [b for b in local if b not in done]
    if len(set(remote) | set(local)) > 1:
        names = ", ".join(sorted(set(remote) | set(local)))
        raise Refused(texts.SEVERAL_BRANCHES.format(n=spec, names=names))
    settings.worktree_root.mkdir(parents=True, exist_ok=True)
    if local:
        branch = local[0]
        checked(
            runner(["git", "-C", repo, "worktree", "add", str(path), branch]),
            "git worktree add",
        )
        wire_hooks(runner, settings, path)
        return Prepared(path, branch, resuming=True)
    if remote:
        branch = remote[0]
        checked(
            runner(
                [
                    "git",
                    "-C",
                    repo,
                    "worktree",
                    "add",
                    "--track",
                    "-b",
                    branch,
                    str(path),
                    f"{remote_name}/{branch}",
                ]
            ),
            "git worktree add",
        )
        wire_hooks(runner, settings, path)
        return Prepared(path, branch, resuming=True)
    branch = _free_name(conventions.branch_name(spec, slug), done)
    checked(
        runner(
            [
                "git",
                "-C",
                repo,
                "worktree",
                "add",
                "--no-track",
                "-b",
                branch,
                str(path),
                settings.base_ref,
            ]
        ),
        "git worktree add",
    )
    wire_hooks(runner, settings, path)
    return Prepared(path, branch, resuming=False)


def wire_hooks(runner: Runner, settings: Settings, path: Path) -> None:
    """The repository's hooks folder as this worktree's `core.hooksPath`,
    relative: each worktree runs the hook its own branch holds."""
    if settings.hooks_dir is None:
        return
    git = ["git", "-C", str(path), "config"]
    checked(runner([*git, "extensions.worktreeConfig", "true"]), "wiring the hooks")
    checked(
        runner([*git, "--worktree", "core.hooksPath", settings.hooks_dir]),
        "wiring the hooks",
    )
