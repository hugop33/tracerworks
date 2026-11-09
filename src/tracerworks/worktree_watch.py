"""A worktree that vanishes outside `clean` is said (#2077, 29/10).

#2047's worktree and local branch disappeared between two passes of the
relauncher (16:14:14 and 16:15:14 UTC), outside tracerworks, and nothing said
so: its spec stayed open, and nobody knew when to look. Now each pass of
`clean` keeps the worktrees it saw in the history base (`worktree_seen`). One
seen on the previous pass, absent now, that `clean` did not remove, is written
once (`worktree_vanished`): a line in the relauncher's journal, with the time
of the last pass that still saw it, and an alert for a day — the alerts'
model, one episode per vanished worktree, « resolved » when the day is over.

A worktree list git could not give is never read as « everything vanished »:
the caller passes none, and the pass changes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

#: How long the alert of a vanished worktree stays open.
ALERT_SECONDS = 24 * 3600


def watch(
    db: sqlite3.Connection,
    project: str,
    listed: Collection[int],
    removed: Collection[int],
    now: float,
) -> list[tuple[int, int]]:
    """Compare this pass's worktrees with the last pass's; the vanished ones,
    (spec, last seen), written once. `listed`: the worktrees git listed at the
    start of the pass; `removed`: those this pass's `clean` took away."""
    at = int(now)
    # read under the write lock: two passes at once never write one twice
    db.execute("BEGIN IMMEDIATE")
    try:
        seen = dict(
            db.execute(
                "SELECT spec, seen_at FROM worktree_seen WHERE project = ?", (project,)
            ).fetchall()
        )
        vanished = sorted(
            (int(spec), int(last)) for spec, last in seen.items() if spec not in listed
        )
        db.executemany(
            "INSERT INTO worktree_vanished (project, spec, last_seen, noticed_at)"
            " VALUES (?, ?, ?, ?)",
            [(project, spec, last, at) for spec, last in vanished],
        )
        db.execute("DELETE FROM worktree_seen WHERE project = ?", (project,))
        db.executemany(
            "INSERT INTO worktree_seen (project, spec, seen_at) VALUES (?, ?, ?)",
            [(project, spec, at) for spec in sorted(set(listed) - set(removed))],
        )
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return vanished


def vanished(db: sqlite3.Connection, project: str, now: float) -> list[tuple[int, int]]:
    """The worktrees vanished in the last day: (spec, last seen) — what the
    alerts say."""
    return [
        (int(spec), int(last))
        for spec, last in db.execute(
            "SELECT spec, MAX(last_seen) FROM worktree_vanished WHERE project = ?"
            " AND noticed_at > ? GROUP BY spec ORDER BY spec",
            (project, int(now) - ALERT_SECONDS),
        )
    ]
