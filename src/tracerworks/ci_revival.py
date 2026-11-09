"""No open pull request stays without CI on its head (#1790, spec #1788).

A label's `ci mode` that gave up on a queued run (#1779, 24/10), a run
cancelled by hand or by a newer one that never came: GitHub says nothing,
and the pull request waits for a CI that will not start. So, in the merge
queue's pass, tracerworks reads every open pull request of a trusted author,
ready and `MERGEABLE` (a conflict starts no CI: its own rule), outside the
queue's test: a head with no run of `ci_workflow` alive (queued, running)
nor finished with a verdict — none, or only cancelled ones — for
`[waits] no_ci_minutes` sees its last run re-run, and one comment says so.
No run at all to re-run: the comment says what to do. Once per head (table
`ci_revival`): a re-run cancelled again is the operator's to read, never a
loop. A pull request whose CI ran nothing (documentation only) is not read,
nor a head whose run was cancelled for its conflict (#2013), nor a pull
request waiting in the merge queue whose run the queue cancelled (#2109):
the queue reruns everything at its turn.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from tracerworks import conflict_runs, texts
from tracerworks.cleanup import Checks, checks_of
from tracerworks.merge_queue import UNFINISHED, GitHub, Run, cancelled_runs, ran_nothing
from tracerworks.process import CommandError
from tracerworks.settings import Settings

#: A completed run that gave no verdict: its CI never told anything.
NO_VERDICT = frozenset({"cancelled", "stale", ""})

#: French, as every comment for the operator on GitHub (merge_queue's).
REVIVED_COMMENT = (
    "**CI relancée par tracerworks (#1790)** : la tête `{head}` n'avait aucun run"
    " de `{workflow}` vivant ni fini depuis {minutes} min (dernier run"
    " annulé : {url}). Relancé une fois ; s'il est encore annulé, tracerworks"
    " ne le relance plus.\n"
)
NO_RUN_COMMENT = (
    "**Aucune CI sur la tête `{head}`** depuis {minutes} min, et aucun run de"
    " `{workflow}` à relancer (#1790). Le geste : pousser la branche à nouveau"
    " (`git commit --allow-empty` puis `git push`), ou retirer puis remettre"
    " l'étiquette de mode.\n"
)


@dataclass(frozen=True)
class Open:
    """An open pull request, as this pass reads it."""

    number: int
    head: str
    branch: str
    draft: bool
    author: str
    mergeable: str
    labels: frozenset[str]
    nothing: bool  # its CI ran nothing: documentation only
    checks: Checks = field(default_factory=lambda: Checks([], []))


def open_pulls(gh: GitHub, settings: Settings) -> list[Open]:
    listed = gh.open_pulls(
        "number,headRefOid,headRefName,isDraft,author,mergeable,labels,"
        "statusCheckRollup"
    )
    check = settings.merge_queue.nothing_check
    try:
        return [
            Open(
                number=int(p["number"]),
                head=str(p["headRefOid"]),
                branch=str(p.get("headRefName") or ""),
                draft=bool(p.get("isDraft")),
                author=str((p.get("author") or {}).get("login") or ""),
                mergeable=str(p.get("mergeable") or "UNKNOWN"),
                labels=frozenset(str(x["name"]) for x in p.get("labels") or []),
                nothing=ran_nothing(p.get("statusCheckRollup") or [], check),
                checks=checks_of(p.get("statusCheckRollup") or []),
            )
            for p in listed
        ]
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        command = "gh pr list (CI revival)"
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc


def without_ci(runs: Sequence[Run]) -> bool:
    """None alive, none with a verdict: its CI never told anything."""
    return not any(
        r.status in UNFINISHED
        or (r.status == "completed" and r.conclusion not in NO_VERDICT)
        for r in runs
    )


def concerned(pull: Open, settings: Settings) -> bool:
    return (
        not pull.draft
        and pull.author in settings.trusted_logins
        and pull.mergeable == "MERGEABLE"
        and settings.merge_queue.testing_label not in pull.labels
        and not pull.nothing
    )


def _row(
    db: sqlite3.Connection, project: str, pull: int, head: str
) -> tuple[int, int | None] | None:
    row = db.execute(
        "SELECT seen_at, acted_at FROM ci_revival"
        " WHERE project = ? AND pull = ? AND head = ?",
        (project, pull, head),
    ).fetchone()
    return (
        None if row is None else (int(row[0]), None if row[1] is None else int(row[1]))
    )


def revive(
    db: sqlite3.Connection,
    gh: GitHub,
    settings: Settings,
    now: int,
    pulls: Sequence[Open] | None = None,
) -> list[str]:
    """One pass over `pulls` (read when not given): what it did, one line
    each. A failing call on one pull request is said and tried again at the
    next pass; the others go on."""
    project, minutes = settings.github_repo, settings.waits.no_ci_minutes
    said: list[str] = []
    for pull in open_pulls(gh, settings) if pulls is None else pulls:
        if not concerned(pull, settings) or conflict_runs.cancelled(
            db, project, pull.number, pull.head
        ):  # cancelled on purpose (#2013): its CI is its worker's push
            continue
        try:
            runs = gh.runs(pull.head)
        except CommandError as exc:
            said.append(texts.CI_REVIVAL_FAILED.format(pull=pull.number, error=exc))
            continue
        if {r.id for r in runs} & cancelled_runs(db, project, pull.number):
            continue  # this head's run, cancelled by the queue it waits in (#2109)
        row = _row(db, project, pull.number, pull.head)
        if not without_ci(runs):
            if row is not None and row[1] is None:  # its CI came back: the clock again
                db.execute(
                    "DELETE FROM ci_revival"
                    " WHERE project = ? AND pull = ? AND head = ?",
                    (project, pull.number, pull.head),
                )
            continue
        if row is None:
            db.execute(
                "INSERT INTO ci_revival (project, pull, head, seen_at)"
                " VALUES (?, ?, ?, ?)",
                (project, pull.number, pull.head, now),
            )
            continue
        seen_at, acted_at = row
        if acted_at is not None or now - seen_at < minutes * 60:
            continue
        short = pull.head[:7]
        fields = {"head": short, "workflow": settings.ci_workflow, "minutes": minutes}
        try:
            if runs:
                gh.rerun(runs[0].id)
                gh.comment(
                    pull.number, REVIVED_COMMENT.format(url=runs[0].url, **fields)
                )
            else:
                gh.comment(pull.number, NO_RUN_COMMENT.format(**fields))
        except CommandError as exc:
            said.append(texts.CI_REVIVAL_FAILED.format(pull=pull.number, error=exc))
            continue
        db.execute(
            "UPDATE ci_revival SET acted_at = ?, run = ?"
            " WHERE project = ? AND pull = ? AND head = ?",
            (now, runs[0].id if runs else None, project, pull.number, pull.head),
        )
        line = texts.CI_REVIVED if runs else texts.CI_NO_RUN
        said.append(line.format(pull=pull.number, head=short, minutes=minutes))
    return said
