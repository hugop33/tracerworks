"""What a delivered pull request cost and took, for the Delivered panel (#860).

Read from the history base (`store.py`) when the panel opens: the chantier's
tokens by category (sub-agents included, as `clean` measures them), the times
from the spec to production, its CI — attempts, green at once or not, minutes
by machine, the tests that failed — and its size. What the base does not know
is None, which the page says « unknown »: never a zero.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from tracerworks import store, texts
from tracerworks.quality import green_at_first_try
from tracerworks.query import QueryError, open_read_only

_TOKENS = ("total", "input", "cache_write", "cache_read", "output")


def build_delivery(
    path: Path, project: str, ci_workflow: str, number: int
) -> dict[str, Any]:
    """`delivered.json?pr=<n>`: the pull request's figures, or None if the
    base has not recorded it."""
    try:
        db = open_read_only(path)
    except QueryError as exc:
        return {"error": str(exc)}
    with closing(db):
        try:
            return {"error": None, "pull": pull_stats(db, project, ci_workflow, number)}
        except sqlite3.Error as exc:
            return {"error": texts.BASE_UNREADABLE.format(error=exc)}


def pull_stats(
    db: sqlite3.Connection, project: str, ci_workflow: str, number: int
) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT title, url, branch, spec, created_at, merged_at, "
        + ", ".join(store.PULL_SIZE)
        + " FROM pull_merged WHERE project = ? AND number = ?",
        (project, number),
    ).fetchone()
    if row is None:
        return None
    title, url, branch, spec, opened, merged, *size = row
    published = spec_title = tokens = None
    if spec is not None:
        found = db.execute(
            "SELECT title, created_at FROM spec WHERE project = ? AND number = ?",
            (project, spec),
        ).fetchone()
        if found:
            spec_title, published = found
        usage = db.execute(
            "SELECT tokens_total, tokens_input, tokens_cache_write, tokens_cache_read,"
            " tokens_output FROM chantier WHERE project = ? AND spec = ?",
            (project, spec),
        ).fetchone()
        tokens = None if usage is None else dict(zip(_TOKENS, usage, strict=True))
    prod = db.execute(
        "SELECT min(at) FROM deployment WHERE project = ? AND at >= ?",
        (project, merged),
    ).fetchone()[0]
    return {
        "number": number,
        "title": title,
        "url": url,
        "branch": branch,
        "spec": None if spec is None else {"number": spec, "title": spec_title},
        "tokens": tokens,
        "times": {
            "published": published,
            "opened": opened,
            "merged": merged,
            "prod": prod,
        },
        "ci": _ci(db, project, ci_workflow, branch, opened, merged),
        "size": dict(zip(store.PULL_SIZE, size, strict=True)),
    }


def _ci(
    db: sqlite3.Connection,
    project: str,
    ci_workflow: str,
    branch: str,
    opened: int | None,
    merged: int,
) -> dict[str, Any]:
    """Its CI: the runs of the CI workflow on its branch while it was open.
    Attempts, and « green at once » by the Quality tab's rule
    (`quality.green_at_first_try`: no judged run red before the merge);
    minutes by machine; the failed tests, unknown while a failed job's log is
    unread."""
    its_runs = (
        "r.project = ? AND r.branch = ? AND " + store.workflow_is("r.workflow_path")
        + " AND r.created_at <= ? AND r.created_at >= ?"
    )  # fmt: skip
    to_runs = (
        " JOIN ci_run r ON r.project = j.project"
        " AND r.run_id = j.run_id AND r.attempt = j.attempt WHERE " + its_runs
    )
    of_runs = " FROM ci_job j" + to_runs
    asked = (project, branch, ci_workflow, ci_workflow, merged, opened or 0)
    verdicts = [
        conclusion
        for (conclusion,) in db.execute(
            "SELECT r.conclusion FROM ci_run r WHERE " + its_runs
            + " AND r.conclusion IN ('success', 'failure')"
            " ORDER BY r.created_at, r.attempt",
            asked,
        )
    ]  # fmt: skip
    last = db.execute(
        "SELECT r.url FROM ci_run r WHERE " + its_runs
        + " ORDER BY r.created_at DESC, r.attempt DESC LIMIT 1",
        asked,
    ).fetchone()  # fmt: skip
    seconds = dict(
        db.execute(
            "SELECT j.machine, sum(j.duration_s)" + of_runs
            + " AND j.machine IS NOT NULL AND j.duration_s IS NOT NULL"
            " GROUP BY j.machine",
            asked,
        ).fetchall()
    )  # fmt: skip
    unread = db.execute(
        "SELECT count(*)" + of_runs + " AND j.conclusion = 'failure'"
        " AND NOT EXISTS (SELECT 1 FROM ci_log l WHERE l.project = j.project"
        " AND l.job_id = j.job_id AND l.tests IS NOT NULL)",
        asked,
    ).fetchone()[0]  # fmt: skip
    failed = [
        test
        for (test,) in db.execute(
            "SELECT DISTINCT f.test FROM ci_test_failure f JOIN ci_job j"
            " ON j.project = f.project AND j.job_id = f.job_id" + to_runs
            + " ORDER BY 1",
            asked,
        )
    ]  # fmt: skip
    return {
        "runs": len(verdicts) or None,
        "first_green": green_at_first_try(verdicts),
        "minutes": None
        if not seconds
        else {m: seconds.get(m, 0) / 60 for m in ("github", "vps")},
        "failed_tests": None if unread else failed,
        "last_run_url": last[0] if last else None,
    }
