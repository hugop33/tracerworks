"""What GitHub says: the runner that shares the machine, the issues, their blockers.

Every call names the repository (`-R owner/name`, `repos/owner/name/…`) from the
project file: nothing depends on the directory `gh` runs in.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from tracerworks import texts
from tracerworks.process import CommandError, Result, Runner, checked

#: The author is read too (#1529): only a trusted login's issue is a spec or
#: a ticket.
ISSUE_FIELDS = "number,title,body,labels,assignees,author"
#: The dashboard also dates them: specs created, tickets closed (#811).
OPEN_FIELDS = f"{ISSUE_FIELDS},createdAt"
CLOSED_FIELDS = f"{ISSUE_FIELDS},closedAt"


def job_log(run: Runner, repo: str, job: int) -> Result:
    """A CI job's log, as `gh` gives it — the one read of a job's log.

    `gh` 2.101 (the VPS's, 28/10) refuses, exit 1, to print a response with
    terminal escape sequences unless told to allow them, and a job's log
    nearly always has some (the colours of its steps, of pytest): without the
    flag no log was read, and the merge queue and the light lane took every
    test's red for a machine's (#1999). The callers strip the colours.
    """
    path = f"repos/{repo}/actions/jobs/{job}/logs"
    return run(["gh", "api", "--allow-escape-sequences", path])


@dataclass(frozen=True)
class RunnerStatus:
    name: str
    status: (
        str  # online | offline | unknown (GitHub could not be read, or no such runner)
    )
    busy: bool | None

    def describe(self) -> str:
        if self.status == "unknown":
            return texts.RUNNER_UNKNOWN.format(name=self.name)
        if self.status != "online":
            return texts.RUNNER_STATUS.format(name=self.name, status=self.status)
        busy = texts.BUSY if self.busy else texts.FREE
        return texts.RUNNER_ONLINE.format(name=self.name, busy=busy)


def parse_runners(payload: str, name: str) -> RunnerStatus:
    """The runners that share the machine, read as one (#641).

    `name` lists them, comma-separated. Busy when any is busy; online when
    any is online; a listed runner GitHub does not know (not registered yet,
    or retired) is ignored — but none known at all is « unknown ».
    """
    names = {part.strip() for part in name.split(",") if part.strip()}
    try:
        runners = json.loads(payload)["runners"]
    except (ValueError, KeyError, TypeError):
        return RunnerStatus(name, "unknown", None)
    found = [r for r in runners if r.get("name") in names]
    if not found:
        return RunnerStatus(name, "unknown", None)
    online = [r for r in found if r.get("status") == "online"]
    if not online:
        return RunnerStatus(name, str(found[0].get("status")), False)
    return RunnerStatus(name, "online", any(bool(r.get("busy")) for r in online))


def runner_status(run: Runner, name: str, repo: str) -> RunnerStatus:
    result = run(["gh", "api", f"repos/{repo}/actions/runners"])
    if not result.ok:
        return RunnerStatus(name, "unknown", None)
    return parse_runners(result.stdout, name)


@dataclass(frozen=True)
class SpecIssue:
    number: int
    title: str
    open: bool
    url: str

    @property
    def repo(self) -> str:
        """`https://github.com/owner/repo/issues/565` → `owner/repo`."""
        return "/".join(self.url.split("/")[3:5])


_NOT_FOUND = ("Could not resolve to an issue", "Not Found", "not found")


def view_issue(run: Runner, number: int, repo: str) -> SpecIssue | None:
    """The issue, None if GitHub says it does not exist — any OTHER failure
    (network, auth) is loud: it must not read as « no such chantier »."""
    result = run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "number,title,state,url",
        ]
    )
    if not result.ok and any(m in result.stderr for m in _NOT_FOUND):
        return None
    data = json.loads(checked(result, f"gh issue view {number}"))
    return SpecIssue(
        int(data["number"]),
        str(data["title"]),
        data["state"] == "OPEN",
        str(data["url"]),
    )


def open_issues_payload(run: Runner, repo: str, fields: str = ISSUE_FIELDS) -> str:
    return checked(
        run(
            [
                "gh",
                "issue",
                "list",
                "-R",
                repo,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                fields,
            ]
        ),
        "gh issue list",
    )


def is_blocked(run: Runner, repo: str, ticket: int, unlisted: list[int]) -> bool:
    """What the open-issue list cannot tell: native dependencies, and the state
    of blockers that are not in it (a PR, a typo, an issue past the list's
    limit). Any failed read counts as blocked — never as free."""
    native = run(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{ticket}",
            "--jq",
            ".issue_dependencies_summary.blocked_by // 0",
        ]
    )
    if not (native.ok and native.stdout.strip() == "0"):
        return True
    for blocker in unlisted:
        state = run(["gh", "api", f"repos/{repo}/issues/{blocker}", "--jq", ".state"])
        if not (state.ok and state.stdout.strip() == "closed"):
            return True
    return False


#: Closed issues read per snapshot. A list this long may be missing some: the
#: caller then treats every fraction as unknown rather than wrong.
CLOSED_LIMIT = 2000


def closed_issues_payload(
    run: Runner, repo: str, created_since: str | None = None
) -> str | None:
    """The closed issues, most recent first; None when GitHub cannot say.

    One paginated list rather than a search per spec: ~7 s and a few GraphQL
    points a minute, against thirty searches sharing the workers' quota.
    `created_since` (YYYY-MM-DD) bounds it: a ticket is never older than its
    spec, so the oldest open spec's day is enough — the list stays short as
    the repository grows."""
    search = ["--search", f"created:>={created_since}"] if created_since else []
    result = run(
        [
            "gh",
            "issue",
            "list",
            "-R",
            repo,
            "--state",
            "closed",
            "--limit",
            str(CLOSED_LIMIT),
            *search,
            "--json",
            CLOSED_FIELDS,
        ]
    )
    return result.stdout if result.ok else None


#: Signs every comment tracerworks posts on a pull request (#2175): under the
#: operator's login, as a worker's are — never a worker's answer to its red.
TRACERWORKS_SIGNATURE = "\n\n<!-- tracerworks -->"


def by_tracerworks(comment: Mapping[str, Any]) -> bool:
    return TRACERWORKS_SIGNATURE.strip() in str(comment.get("body") or "")


def written_by(comment: Mapping[str, Any], trusted: Collection[str]) -> bool:
    """Whether a comment (or an issue) of `gh … --json` is a trusted login's
    (#1529): anyone else's is data, never a signal tracerworks acts on."""
    author = comment.get("author")
    login = author.get("login") if isinstance(author, Mapping) else None
    return isinstance(login, str) and login in trusted


def last_comment(
    run: Runner, repo: str, number: int, trusted: Collection[str]
) -> str | None:
    """The first non-blank line of the issue's last comment by a trusted
    login (#1529); None if unreadable or if it has none."""
    result = run(["gh", "issue", "view", str(number), "-R", repo, "--json", "comments"])
    if not result.ok:
        return None
    try:
        comments = json.loads(result.stdout)["comments"]
    except (ValueError, KeyError, TypeError):
        return None
    comments = [c for c in comments if written_by(c, trusted)]
    if not comments:
        return None
    lines = str(comments[-1].get("body") or "").splitlines()
    return next((line.strip() for line in lines if line.strip()), None)


def last_labeled_by(run: Runner, repo: str, number: int, label: str) -> str | None:
    """The login that last put `label` on the issue (#1529), from its events,
    page by page; None when it cannot be read or was never labelled — an
    unknown actor is never the operator."""
    result = run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/events",
            "--jq",
            '.[] | select(.event == "labeled") | [.label.name, .actor.login] | @tsv',
        ]
    )
    if not result.ok:
        return None
    actors = [
        line.split("\t", 1)[1]
        for line in result.stdout.splitlines()
        if "\t" in line and line.split("\t", 1)[0] == label
    ]
    return actors[-1] if actors else None


def last_reopened_at(run: Runner, repo: str, number: int) -> float | None:
    """When the issue was last reopened (#2077), from its events; None: never.
    A failed or unreadable answer is loud (`CommandError`)."""
    command = f"gh api issues/{number}/events"
    payload = checked(
        run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/issues/{number}/events",
                "--jq",
                '.[] | select(.event == "reopened") | .created_at',
            ]
        ),
        command,
    )
    try:
        stamps = [
            datetime.fromisoformat(line.strip().replace("Z", "+00:00")).timestamp()
            for line in payload.splitlines()
            if line.strip()
        ]
    except ValueError as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc
    return max(stamps, default=None)


#: What `star_issue` did (#1877): nothing to star, nothing to change, or the
#: label put or removed.
StarOutcome = Literal["absent", "closed", "unchanged", "put", "removed"]


def star_issue(
    run: Runner, repo: str, number: int, label: str, starred: bool
) -> StarOutcome:
    """The operator's star put on (or taken off) an OPEN issue — the
    dashboard's only write. The issue is read first: absent or closed, nothing
    is written; already as asked, nothing either (idempotent). Any failure of
    `gh` is loud (`CommandError`, its stderr): never a star that did not take."""
    result = run(
        ["gh", "issue", "view", str(number), "-R", repo, "--json", "state,labels"]
    )
    if not result.ok and any(m in result.stderr for m in _NOT_FOUND):
        return "absent"
    data = json.loads(checked(result, f"gh issue view {number}"))
    if data["state"] != "OPEN":
        return "closed"
    if (label in {str(lb["name"]) for lb in data["labels"]}) == starred:
        return "unchanged"
    flag = "--add-label" if starred else "--remove-label"
    edit = ["gh", "issue", "edit", str(number), "-R", repo, flag, label]
    checked(run(edit), f"gh issue edit {number}")
    return "put" if starred else "removed"
