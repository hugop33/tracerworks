"""The spec's labels its pull request carries (#1297, spec #1294).

The project names them (`[github] carried_labels`) — for the project that built
it, the CI's mode labels: the operator puts one on a spec at launch, the primer
tells the worker to open its pull request with it, and `clean` puts it back on
the open pull request if the worker forgot.

ONCE: a pull request on which anyone ever set or removed one of those labels
is left alone — the operator choosing another mode on it, or none, is never
undone. The pull request's own events say so; nothing is kept here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from tracerworks import texts
from tracerworks.process import CommandError, Runner, checked

#: The labels a pull request's events set or removed, one per line.
_LABEL_EVENTS = (
    '.[] | select(.event == "labeled" or .event == "unlabeled") | .label.name'
)


def _names(entries: Any, command: str) -> list[str]:
    try:
        return [str(e["name"]) for e in entries]
    except (TypeError, KeyError) as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc


def _json(payload: str, command: str) -> Any:
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc


def spec_labels(
    runner: Runner, repo: str, spec: int, carried: Sequence[str]
) -> list[str]:
    """The carried labels the spec has, in the project's order; none
    configured, nothing asked."""
    if not carried:
        return []
    return carried_of(issue_labels(runner, repo, spec), carried)


def issue_labels(runner: Runner, repo: str, spec: int) -> list[str]:
    """Every label the spec has: ONE read at a worker's start serves its
    carried labels and its level (#2251). A failed read is loud."""
    command = f"gh issue view {spec} (labels)"
    payload = checked(
        runner(["gh", "issue", "view", str(spec), "--repo", repo, "--json", "labels"]),
        command,
    )
    data = _json(payload, command)
    entries = data.get("labels") if isinstance(data, dict) else None
    return _names(entries, command)


def carried_of(labels: Sequence[str], carried: Sequence[str]) -> list[str]:
    """The carried labels among `labels`, in the project's order."""
    present = set(labels)
    return [label for label in carried if label in present]


def carry(
    runner: Runner, repo: str, spec: int, branch: str, carried: Sequence[str]
) -> str | None:
    """Put the spec's carried labels the open pull request of `branch` lacks
    on it, unless one of them was ever set or removed there: what was done,
    said, or None. A failed read is loud (`CommandError`)."""
    wanted = spec_labels(runner, repo, spec, carried)
    if not wanted:
        return None
    command = f"gh pr list --head {branch} (open, labels)"
    listing = [
        "gh", "pr", "list", "--repo", repo, "--head", branch,
        "--state", "open", "--json", "number,labels", "--limit", "1",
    ]  # fmt: skip
    found = _json(checked(runner(listing), command), command)
    if not isinstance(found, list) or not found:
        return None
    try:
        pull, on_pull = int(found[0]["number"]), found[0]["labels"]
    except (TypeError, KeyError, ValueError) as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc
    missing = [label for label in wanted if label not in _names(on_pull, command)]
    if not missing:
        return None
    # Every page: a removal past the first hundred events is still a choice.
    events = ["gh", "api", "--paginate", f"repos/{repo}/issues/{pull}/events"]
    listed = checked(
        runner([*events, "-q", _LABEL_EVENTS]), f"gh api issues/{pull}/events"
    )
    touched = set(listed.split())
    if touched & set(carried):
        return None
    edit = ["gh", "pr", "edit", str(pull), "--repo", repo]
    checked(
        runner([*edit, "--add-label", ",".join(missing)]),
        f"gh pr edit {pull} --add-label",
    )
    return texts.LABELS_CARRIED.format(n=spec, pull=pull, labels=", ".join(missing))
