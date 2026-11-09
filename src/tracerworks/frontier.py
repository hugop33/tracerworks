"""The GitHub frontier: which chantier `start-next` may launch (architecture §2.1).

A spec carries the project's READY label; each of its tickets names it in its
body (a `## <parent keyword>` section, or a line `<parent keyword> : #<spec>`)
and lists its blockers (`<blocker keyword> : #a, #b`, or a `- #n` list under a
`## <blocker keyword>` title) — the keywords are the project's, from its
configuration — and native dependencies are read too when they exist. A ticket
is ON THE FRONTIER when it is open, unassigned (an assignee is a claim) and
every blocker is closed. A chantier is eligible when it is ready, has a ticket
on the frontier, and has no worker — not even a stopped one: a stopped
chantier is resumed by an explicit `start <n>`, never behind the operator's
back.

ONLY A TRUSTED LOGIN'S ISSUE COUNTS (#1529). An issue written by anyone else
is neither a spec nor a ticket, and its parent and blocker lines are not
read: a third party can neither add work to a chantier nor reorder it. It
still blocks a trusted ticket that names it, and native dependencies block
whoever made them — the trust rule only holds work back, never lets it
through. The spec's launch label must have been put by the operator.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from tracerworks import texts

_REF = re.compile(r"#(\d+)")


@dataclass(frozen=True)
class BodyRules:
    """How this project's tickets name their parent and their blockers."""

    parent_keywords: Sequence[str]
    blocker_keywords: Sequence[str]

    def parent(self, body: str) -> int | None:
        """The first `#n` of a `## Parent` section (`## Parent\n\nChantier
        15 — spec #444.`), else of a line that starts `Parent :`. « Parent »
        in a sentence is no parent (11/10, #1190)."""
        words = _words(self.parent_keywords)
        for section in _sections(body, words):
            match = _REF.search(section)
            if match:
                return int(match.group(1))
        match = re.search(
            rf"^[ \t>*_]*(?:{words})[ \t*_]*:[^#\n]{{0,80}}?#(\d+)",
            body,
            re.MULTILINE,
        )
        return int(match.group(1)) if match else None

    def blockers(self, body: str) -> tuple[int, ...]:
        """Every `<keyword> : #a, #b` line, and every list item (`- #n`)
        under a `## <keyword>` title — `to-tickets` wrote that form (12/10,
        22/10). Prose under the title is not read: a reference there may be
        only context."""
        words = _words(self.blocker_keywords)
        lines = re.findall(rf"(?:{words})\s*:\s*([^\n]*)", body, re.IGNORECASE)
        lines += [
            item
            for section in _sections(body, words)
            for item in _LIST_ITEM.findall(section)
        ]
        found = (int(n) for line in lines for n in _REF.findall(line))
        return tuple(dict.fromkeys(found))


_HEADING = re.compile(r"^#{1,6}[ \t]", re.MULTILINE)
#: A list item's leading references (`- #1651, #1652 (la langue…)`): a `#n`
#: further in its text is context (#1655 cited « le module de #1650 »).
_LIST_ITEM = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?((?:#\d+[ \t,;&]*)+)",
    re.MULTILINE,
)


def _words(keywords: Sequence[str]) -> str:
    return "|".join(re.escape(k) for k in keywords)


def _sections(body: str, words: str) -> list[str]:
    """The text under each `#…# <keyword>` title, up to the next title."""
    title = re.compile(
        rf"^#{{1,6}}[ \t]+(?:{words})[ \t]*:?[ \t]*$", re.MULTILINE | re.IGNORECASE
    )
    sections = []
    for match in title.finditer(body):
        rest = body[match.end() :]
        end = _HEADING.search(rest)
        sections.append(rest[: end.start()] if end else rest)
    return sections


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: frozenset[str]
    assigned: bool
    parent: int | None
    blockers: tuple[int, ...]
    body: str = ""
    created_at: int | None = None  # epoch seconds, when the list asked for it
    author: str | None = None  # its author's login, when the list asked for it
    closed_at: int | None = None


def parse_issues(payload: str, rules: BodyRules) -> list[Issue]:
    """`gh issue list --json number,title,body,labels,assignees` → issues."""
    issues = []
    for item in json.loads(payload):
        body = str(item.get("body") or "")
        issues.append(
            Issue(
                number=int(item["number"]),
                title=str(item["title"]),
                labels=frozenset(label["name"] for label in item.get("labels", [])),
                assigned=bool(item.get("assignees")),
                parent=rules.parent(body),
                blockers=rules.blockers(body),
                body=body,
                created_at=_epoch(item.get("createdAt")),
                closed_at=_epoch(item.get("closedAt")),
                author=_login(item.get("author")),
            )
        )
    return issues


def _login(author: object) -> str | None:
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) and login else None


def _epoch(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


@dataclass(frozen=True)
class Candidate:
    spec: Issue
    head: Issue  # the ticket the worker will take first


@dataclass(frozen=True)
class Queue:
    """Which specs `start-next` may take, and in which order.

    A spec written in advance is not a request to build it: it enters the
    queue only when it carries EVERY required label (the project's « ready »
    and « to launch »). Among those, the ones carrying the priority label go
    first, then the oldest (lowest number).
    """

    required: frozenset[str]
    priority: str

    def admits(self, issue: Issue) -> bool:
        return self.required <= issue.labels

    def rank(self, issue: Issue) -> tuple[bool, int]:
        return (self.priority not in issue.labels, issue.number)


def head_of(
    tickets: Iterable[Issue],
    open_numbers: set[int],
    blocked_elsewhere: Callable[[int, list[int]], bool],
) -> Issue | None:
    """The first open ticket on the frontier: unassigned, every blocker closed."""
    for ticket in sorted(tickets, key=lambda i: i.number):
        if ticket.assigned:
            continue
        if any(b in open_numbers for b in ticket.blockers):
            continue
        unlisted = [b for b in ticket.blockers if b not in open_numbers]
        if blocked_elsewhere(ticket.number, unlisted):
            continue
        return ticket
    return None


def choose(
    open_issues: Iterable[Issue],
    queue: Queue,
    has_worker: Callable[[int], str | None],
    blocked_elsewhere: Callable[[int, list[int]], bool],
    trusted: Callable[[Issue], bool] = lambda issue: True,
    launched: Callable[[Issue], str | None] = lambda spec: None,
) -> tuple[Candidate | None, list[str]]:
    """The first eligible chantier of the queue, its head ticket, and why
    others were skipped.

    `has_worker(n)` names the worker a chantier already has (None if none);
    `blocked_elsewhere(t, unlisted)` asks GitHub what the open list cannot say
    — native dependencies, and the state of blockers absent from the list —
    only for a ticket the text says is free: a few calls per candidate, not
    per issue.

    `trusted(issue)`: whether its author is a trusted login (#1529);
    `launched(spec)`: why its launch label does not count (None: put by the
    operator) — asked for the first spec otherwise eligible only.
    """
    issues = list(open_issues)
    open_numbers = {i.number for i in issues}  # any author's: it holds back
    children: dict[int, list[Issue]] = {}
    for issue in issues:
        if issue.parent is not None and trusted(issue):
            children.setdefault(issue.parent, []).append(issue)
    skipped: list[str] = []
    for spec in sorted(issues, key=queue.rank):
        if not queue.admits(spec):
            continue
        if not trusted(spec):
            why = texts.FOREIGN_AUTHOR.format(login=spec.author or "?")
            skipped.append(texts.SKIPPED_SPEC.format(n=spec.number, why=why))
            continue
        if spec.number not in children:
            continue
        worker = has_worker(spec.number)
        if worker is not None:
            skipped.append(texts.SKIPPED_SPEC.format(n=spec.number, why=worker))
            continue
        head = head_of(children[spec.number], open_numbers, blocked_elsewhere)
        if head is None:
            skipped.append(texts.NO_FREE_TICKET.format(n=spec.number))
            continue
        refused = launched(spec)
        if refused is not None:
            skipped.append(texts.SKIPPED_SPEC.format(n=spec.number, why=refused))
            continue
        return Candidate(spec, head), skipped
    return None, skipped
