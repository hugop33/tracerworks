"""The run of a worker's head in conflict is cancelled, once per head (#2013).

A head in conflict with the base will not merge, even green: its run proves
nothing. On 28/10, #1996's (#1974) run 40763050431 stayed queued 25 min
without a runner, ahead of the queue's, while its worker, resumed on the
conflict, waited for it. The resume now tells the worker to push its merge
at once (`parking.RESUME_CONFLICT`); and, in the same pass as the CI's
revival (#1790), tracerworks force-cancels every run of `ci_workflow` still
alive (queued, running) on the head of a worker's pull request `CONFLICTING`
— a plain cancel leaves the `if: always()` aggregates queued. One comment
says it. Once per head (table `conflict_cancel`), written when it cancelled
something: no run alive yet (GitHub may say `CONFLICTING` before the run of
a fresh push is queued), the head is read again at the next pass.

Never in the queue's test: the queue leaves on a conflict already (#1715).
A head cancelled so is never revived by #1790 (`cancelled`): its conflict
gone without a push, its CI is its worker's push.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from tracerworks import texts
from tracerworks.merge_queue import UNFINISHED, GitHub
from tracerworks.process import CommandError
from tracerworks.settings import Settings

if TYPE_CHECKING:  # ci_revival reads `cancelled`
    from tracerworks.ci_revival import Open

#: French, as every comment for the operator on GitHub (merge_queue's).
COMMENT = (
    "**Run de la tête `{head}` annulé par tracerworks (#2013)** : la PR est en"
    " conflit avec `{base}`, cette tête ne fusionnera pas, même verte. Annulé"
    " pour ne pas prendre un runner : {urls}. La CI repart au push de la"
    " fusion.\n"
)


def concerned(pull: Open, settings: Settings) -> bool:
    return (
        pull.mergeable == "CONFLICTING"
        and pull.author in settings.trusted_logins
        and settings.merge_queue.testing_label not in pull.labels
        and settings.conventions.parse_branch(pull.branch) is not None
    )


def cancelled(db: sqlite3.Connection, project: str, pull: int, head: str) -> bool:
    """A run of this head was cancelled for its conflict."""
    return (
        db.execute(
            "SELECT 1 FROM conflict_cancel WHERE project = ? AND pull = ? AND head = ?",
            (project, pull, head),
        ).fetchone()
        is not None
    )


def tend(
    db: sqlite3.Connection,
    gh: GitHub,
    settings: Settings,
    pulls: Sequence[Open],
    now: int,
) -> list[str]:
    """One pass over `pulls`: what it cancelled, one line each. A failing
    call is said, and the head read again at the next pass."""
    project = settings.github_repo
    said: list[str] = []
    for pull in pulls:
        if not concerned(pull, settings) or cancelled(
            db, project, pull.number, pull.head
        ):
            continue
        try:
            alive = [r for r in gh.runs(pull.head) if r.status in UNFINISHED]
            for run in alive:
                gh.force_cancel(run.id)
        except CommandError as exc:
            said.append(
                texts.CONFLICT_CANCEL_FAILED.format(pull=pull.number, error=exc)
            )
            continue
        if not alive:
            continue  # nothing yet: read again at the next pass
        runs = ",".join(str(r.id) for r in alive)
        db.execute(
            "INSERT OR IGNORE INTO conflict_cancel (project, pull, head, at, runs)"
            " VALUES (?, ?, ?, ?, ?)",
            (project, pull.number, pull.head, now, runs),
        )
        short = pull.head[:7]
        said.append(
            texts.CONFLICT_CANCELLED.format(pull=pull.number, head=short, runs=runs)
        )
        urls = ", ".join(r.url or str(r.id) for r in alive)
        try:
            gh.comment(
                pull.number,
                COMMENT.format(head=short, base=settings.base_branch, urls=urls),
            )
        except CommandError as exc:  # cancelled and recorded: said, not again
            said.append(
                texts.CONFLICT_CANCEL_NOT_SAID.format(pull=pull.number, error=exc)
            )
    return said
