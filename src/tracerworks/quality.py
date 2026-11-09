"""The Qualité tab (#840): how well the factory works, from the history base.

Every indicator is computed here from the base, read only; the page draws.
A part the base cannot give is None or an empty list, said on the page —
never a zero standing for « unknown ».

A pull request is known by its branch: GitHub hands the runs of this project
no pull request number, and a branch is one pull request.

The flaky test (spec #836, Q13), each rule enough alone:
- SAME COMMIT: a test fails in a job, and the same job passes later on the
  same commit (a re-run, or a new run of the same commit);
- TWO PULL REQUESTS: a test fails on at least two different branches within
  7 days, none of which touches its module. A branch touches the module of
  `tests/…/test_browser_widget_journey.py` when its pull request changes that
  file, or a file one of whose path parts (a folder, or the file's own name)
  is a word of the test file's name of four letters or more (`widget`,
  `browser`, `journey`). A branch whose files are unknown is not counted.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from tracerworks import store, texts
from tracerworks.query import QueryError, open_read_only

DAY = 86400
WINDOW = 30 * DAY  # pull requests, chantiers, the operator's time
RUNNER_WINDOW = 7 * DAY
FLAKY_SPAN = 7 * DAY
#: the two rules that recognise a flaky test, by the names the tab shows
SAME_COMMIT = "même commit"
TWO_PULLS = "2 PR"
MIN_WORD = 4  # shorter words (`app`, `ci`, `api`) touch everything
LISTED = 15


def _median(values: Iterable[float]) -> float | None:
    kept = list(values)
    return float(statistics.median(kept)) if kept else None


def _quantile(values: Iterable[float], q: float) -> float | None:
    kept = sorted(values)
    if not kept:
        return None
    return float(kept[min(len(kept) - 1, int(q * len(kept)))])


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).date().isoformat()


def _week(at: float) -> str:
    year, week, _ = datetime.fromtimestamp(at, UTC).isocalendar()
    return f"{year}-S{week:02d}"


# --- 1. what a merged pull request cost ---------------------------------------------


def cost_per_pull(db: sqlite3.Connection, project: str, now: float) -> dict[str, Any]:
    rows = db.execute(
        "SELECT c.spec, s.title, c.finished_at, c.tokens_total, count(p.number),"
        " c.tokens_input, c.tokens_cache_write, c.tokens_cache_read, c.tokens_output"
        " FROM chantier c JOIN pull_merged p"
        " ON p.project = c.project AND p.spec = c.spec"
        " LEFT JOIN spec s ON s.project = c.project AND s.number = c.spec"
        " WHERE c.project = ? GROUP BY c.spec ORDER BY c.finished_at DESC",
        (project,),
    ).fetchall()
    items = [
        {"spec": spec, "title": title, "at": at, "tokens": tokens, "pulls": n,
         "cost": tokens / n}
        for spec, title, at, tokens, n, *_ in rows
    ]  # fmt: skip
    # each one's cost by category: input, cache written, cache read, output
    kinds = [[k / row[4] for k in row[5:]] for row in rows]
    # by UTC day, the 30 ending today (#853): a day without a finished spec
    # is absent; each cost then by category — input, cache written, cache
    # read, output (#859, stacked)
    days: dict[str, list[list[float]]] = defaultdict(list)
    first = _day(now - WINDOW + DAY)
    for item, by_kind in zip(items, kinds, strict=True):
        if _day(item["at"]) >= first:
            days[_day(item["at"])].append([item["cost"], *by_kind])
    recent = [i["cost"] for i in items if i["at"] >= now - WINDOW]
    before = [i["cost"] for i in items if now - 2 * WINDOW <= i["at"] < now - WINDOW]
    return {
        "mean": statistics.fmean(recent) if recent else None,
        "mean_before": statistics.fmean(before) if before else None,
        "chantiers": len(recent),
        "days": [
            [d, statistics.fmean(c[0] for c in costs), len(costs)]
            + [statistics.fmean(c[k] for c in costs) for k in range(1, 5)]
            for d, costs in sorted(days.items())
        ],
        "items": items[:LISTED],
    }


# --- 2. CI green the first time -----------------------------------------------------


JUDGED = ("success", "failure")  # a cancelled or skipped run judged nothing


def green_at_first_try(verdicts: Iterable[str]) -> bool | None:
    """The rule the Quality tab, the Measures series and a chantier's card
    read (#2293): a pull request is green at first try when none of its CI
    runs judged before the merge was red — however many runs the merge queue
    made. None when no run judged it: unknown, never false."""
    judged = [v for v in verdicts if v in JUDGED]
    return None if not judged else "failure" not in judged


def first_green(
    db: sqlite3.Connection, project: str, ci_workflow: str, now: float
) -> dict[str, Any]:
    """For each pull request merged in the window: its CI attempts before the
    merge that ran to a verdict, and whether it was green at first try
    (`green_at_first_try`); the worst are those with a red, most attempts first."""
    rows = db.execute(
        "SELECT p.number, p.title, p.url, r.conclusion"
        " FROM pull_merged p JOIN ci_run r ON r.project = p.project"
        " AND r.branch = p.branch AND r.conclusion IN ('success', 'failure')"
        f" AND {store.workflow_is('r.workflow_path')}"
        " AND r.created_at <= p.merged_at"
        " WHERE p.project = ? AND p.merged_at >= ?"
        " ORDER BY p.number, r.created_at, r.attempt",
        (ci_workflow, ci_workflow, project, now - WINDOW),
    ).fetchall()
    pulls: dict[int, dict[str, Any]] = {}
    verdicts: dict[int, list[str]] = defaultdict(list)
    for number, title, url, conclusion in rows:
        pull = pulls.setdefault(
            number,
            {"number": number, "title": title, "url": url, "attempts": 0},
        )  # fmt: skip
        pull["attempts"] += 1
        verdicts[number].append(conclusion)
    if not pulls:
        return {"pulls": 0, "first_green": None, "share": None, "mean_attempts": None,
                "worst": []}  # fmt: skip
    green = [n for n in pulls if green_at_first_try(verdicts[n])]
    ordered = sorted(pulls.values(), key=lambda p: (-p["attempts"], -p["number"]))
    return {
        "pulls": len(pulls),
        "first_green": len(green),
        "share": round(len(green) / len(pulls) * 100, 1),
        "mean_attempts": round(
            statistics.fmean(p["attempts"] for p in pulls.values()), 2
        ),
        "worst": [p for p in ordered if p["number"] not in green][:5],
    }


# --- 3. from the spec to production -------------------------------------------------


def cycle_times(db: sqlite3.Connection, project: str, now: float) -> dict[str, Any]:
    """Per chantier delivered in the window, its last merged pull request:
    spec published → merged → the first successful deployment after."""
    rows = db.execute(
        "SELECT s.number, s.title, s.created_at, max(p.merged_at),"
        " (SELECT min(d.at) FROM deployment d WHERE d.project = p.project"
        "  AND d.at >= max(p.merged_at))"
        " FROM pull_merged p JOIN spec s ON s.project = p.project AND s.number = p.spec"
        " WHERE p.project = ? GROUP BY s.number HAVING max(p.merged_at) >= ?"
        " ORDER BY max(p.merged_at) DESC",
        (project, now - WINDOW),
    ).fetchall()
    items = [
        {"spec": n, "title": t, "published": pub, "merged": merged, "prod": prod,
         "to_merge": merged - pub, "to_prod": None if prod is None else prod - merged}
        for n, t, pub, merged, prod in rows
    ]  # fmt: skip
    return {
        "chantiers": len(items),
        "median_to_merge": _median(i["to_merge"] for i in items),
        "median_to_prod": _median(
            i["to_prod"] for i in items if i["to_prod"] is not None
        ),
        "not_in_prod": sum(1 for i in items if i["prod"] is None),
        "items": items[:LISTED],
    }


# --- 4. time spent waiting for the operator -----------------------------------------


def waiting_operator(
    db: sqlite3.Connection, project: str, now: float
) -> dict[str, Any]:
    start = now - WINDOW
    rows = db.execute(
        "SELECT issue, started_at, ended_at FROM needs_human WHERE project = ?"
        " AND (ended_at IS NULL OR ended_at >= ?) ORDER BY started_at",
        (project, start),
    ).fetchall()
    weeks: dict[str, float] = defaultdict(float)
    total = 0.0
    for _, started, ended in rows:
        begin, end = max(started, start), ended if ended is not None else now
        total += max(0.0, end - begin)
        weeks[_week(begin)] += max(0.0, end - begin)
    return {
        "total": total,
        "episodes": len(rows),
        "weeks": sorted([w, s] for w, s in weeks.items()),
        "open": [{"issue": i, "since": s} for i, s, e in rows if e is None],
    }


# --- 5. waiting for a runner --------------------------------------------------------


def runner_wait(db: sqlite3.Connection, project: str, now: float) -> dict[str, Any]:
    rows = db.execute(
        "SELECT machine, wait_s, date(created_at, 'unixepoch') FROM ci_job"
        " WHERE project = ? AND machine IS NOT NULL AND wait_s IS NOT NULL"
        " AND created_at >= ?",
        (project, now - RUNNER_WINDOW),
    ).fetchall()
    by_machine: dict[str, list[float]] = defaultdict(list)
    by_day: dict[tuple[str, str], list[float]] = defaultdict(list)
    for machine, wait, day in rows:
        by_machine[machine].append(wait)
        by_day[(machine, day)].append(wait)
    return {
        machine: {
            "jobs": len(by_machine[machine]),
            "median": _median(by_machine[machine]),
            "p90": _quantile(by_machine[machine], 0.9),
            "max": max(by_machine[machine]) if by_machine[machine] else None,
            "days": sorted(
                [day, _median(w)] for (m, day), w in by_day.items() if m == machine
            ),
        }
        for machine in ("github", "vps")
    }


# --- 6. flaky tests -------------------------------------------------------------------


def words_of(test: str) -> set[str]:
    stem = PurePosixPath(test.split("::")[0]).stem
    return {w for w in stem.split("_") if len(w) >= MIN_WORD and w != "test"}


def touches(test: str, changed: Iterable[str]) -> bool:
    """Whether a change set touches the module of `test` (module docstring)."""
    file, words = test.split("::")[0], words_of(test)
    for path in changed:
        if path == file:
            return True
        parts = PurePosixPath(path)
        if any(p in words for p in (*parts.parent.parts, parts.stem)):
            return True
    return False


@dataclass(frozen=True)
class Failure:
    """One recorded failure of a test (#2224)."""

    at: float
    url: str
    #: the rules that recognise THIS failure flaky; empty: a plain red
    rules: tuple[str, ...] = ()


def flaky_failures(
    db: sqlite3.Connection, project: str, base_branch: str
) -> dict[str, list[Failure]]:
    """Every recorded failure of each test, oldest first, and the rules that
    recognise it flaky: « même commit » when the same job went green later at
    the same commit; « 2 PR » when it failed on a pull request that does not
    touch it, and on another one such within `FLAKY_SPAN`. Read by the
    Qualité tab (`flaky_tests`) and by the flaky tests' bugs (#2224)."""
    rows = db.execute(
        "SELECT f.test, j.completed_at, r.branch, r.url,"
        " EXISTS (SELECT 1 FROM ci_job k JOIN ci_run q ON q.project = k.project"
        "   AND q.run_id = k.run_id AND q.attempt = k.attempt"
        "   WHERE k.project = j.project AND k.name = j.name"
        "   AND q.head_sha = r.head_sha AND k.conclusion = 'success'"
        "   AND k.completed_at > j.completed_at)"
        " FROM ci_test_failure f JOIN ci_job j USING (project, job_id)"
        " JOIN ci_run r ON r.project = j.project AND r.run_id = j.run_id"
        " AND r.attempt = j.attempt WHERE f.project = ? ORDER BY j.completed_at",
        (project,),
    ).fetchall()
    files: dict[str, list[str]] = defaultdict(list)
    for branch, path in db.execute(
        "SELECT branch, path FROM branch_file WHERE project = ?", (project,)
    ):
        files[branch].append(path)
    known = {
        branch
        for (branch,) in db.execute(
            "SELECT branch FROM branch_read WHERE project = ? AND files IS NOT NULL",
            (project,),
        )
    }
    by_test: dict[str, list[tuple[float, str, str, bool]]] = defaultdict(list)
    for test, at, branch, url, again in rows:
        by_test[test].append((at or 0, branch, url, bool(again)))
    found: dict[str, list[Failure]] = {}
    for test, listed in by_test.items():
        untouched = [
            (at, branch)
            for at, branch, _, _ in listed
            if branch != base_branch
            and branch in known
            and not touches(test, files[branch])
        ]
        failures = []
        for at, branch, url, again in listed:
            rules = []
            if again:
                rules.append(SAME_COMMIT)
            if (at, branch) in untouched and any(
                other != branch and abs(when - at) <= FLAKY_SPAN
                for when, other in untouched
            ):
                rules.append(TWO_PULLS)
            failures.append(Failure(at, url, tuple(rules)))
        found[test] = failures
    return found


def flaky_tests(db: sqlite3.Connection, project: str, base_branch: str) -> list[Any]:
    found: list[dict[str, Any]] = []
    for test, failures in flaky_failures(db, project, base_branch).items():
        rules = [
            rule
            for rule in (SAME_COMMIT, TWO_PULLS)
            if any(rule in failure.rules for failure in failures)
        ]
        if not rules:
            continue
        last = failures[-1]
        found.append(
            {"test": test, "failures": len(failures), "last_at": last.at,
             "last_url": last.url, "rules": rules}
        )  # fmt: skip
    return sorted(found, key=lambda f: (-f["failures"], -(f["last_at"] or 0)))[:20]


# --- 7. alerts ------------------------------------------------------------------------


def alert_episodes(db: sqlite3.Connection, now: float) -> list[Any]:
    """The alerts of the window, and any still open, newest first (#841)."""
    return [
        {"key": key, "text": text, "opened_at": opened, "closed_at": closed,
         "sent": sent is not None, "silenced": bool(silenced)}
        for key, text, opened, closed, sent, silenced in db.execute(
            "SELECT key, text, opened_at, closed_at, opened_sent_at, silenced"
            " FROM alert WHERE opened_at >= ? OR closed_at IS NULL"
            " ORDER BY opened_at DESC LIMIT 50",
            (now - WINDOW,),
        )
    ]  # fmt: skip


def build_quality(
    path: Path,
    project: str,
    ci_workflow: str,
    base_branch: str,
    now: float,
    operator: str = "",
    human_label: str = "",
) -> dict[str, Any]:
    try:
        db = open_read_only(path)
    except QueryError as exc:
        return {"error": str(exc)}
    with closing(db):
        try:
            logs = db.execute(
                "SELECT count(*), sum(tests IS NULL) FROM ci_log WHERE project = ?",
                (project,),
            ).fetchone()
            failed = db.execute(
                "SELECT count(*) FROM ci_job"
                " WHERE project = ? AND conclusion = 'failure'",
                (project,),
            ).fetchone()[0]
            return {
                "error": None,
                "now": now,
                # the page names them as the project file does
                "operator_name": operator,
                "human_label": human_label,
                "cost": cost_per_pull(db, project, now),
                "first_green": first_green(db, project, ci_workflow, now),
                "cycle": cycle_times(db, project, now),
                "operator": waiting_operator(db, project, now),
                "runners": runner_wait(db, project, now),
                "flaky": flaky_tests(db, project, base_branch),
                "alerts": alert_episodes(db, now),
                # how much of the CI's failures the flaky list rests on
                "logs": {"failed_jobs": failed, "read": logs[0], "gone": logs[1] or 0},
            }
        except sqlite3.Error as exc:
            return {"error": texts.BASE_UNREADABLE.format(error=exc)}
