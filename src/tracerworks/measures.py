"""The Measures tab (#1821): what the factory did, day by day, over 7, 30 or
90 days — read from the history base, the `vps-health` readings included.

One value per UTC day and per series. A day without data is None, a gap the
page draws as one, never an invented zero: a measure (a median, a lowest, a
highest) is None on a day without a reading; a count is None before the
first row its table ever had, then 0 on a day without any.

#1822 adds what the base did not say yet: a worker's waits by cause (its
states), the reds by cause (#1792's rule), the automatic relaunches, the
OOM kills, the runners' busy share and the wait before one, the cost of a
chantier and the pull requests green at first try.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracerworks import machine_red, store, texts
from tracerworks.quality import alert_episodes, flaky_tests, green_at_first_try
from tracerworks.query import QueryError, open_read_only
from tracerworks.trends import CI_SHARE_DAYS, Pace, machine_hours, quotas

DAY = 86400
SPANS = (7, 30, 90)
DEFAULT_SPAN = 30
#: Each consumer of the CPU the 5-minute reading names (#2279), in its order.
LOAD_CONSUMERS = ("cpu_workers", "cpu_ci", "cpu_factory", "cpu_containers", "cpu_rest")
MEMORY_LOW_MB = 1536  # the alert threshold of `vps-health` (15/10)
#: A worker's wait, by the cause its state was decided by (#1729's history).
WAIT_CAUSES = {
    "parked during its CI": "wait_ci",
    "worker at rest, its CI runs": "wait_ci",
    "merge queue: testing": "wait_ci",
    "merge queue: waiting": "wait_ci",
    "worker: waits for memory": "wait_memory",
    "no worker, the memory low": "wait_memory",
    "worker: permission": "wait_permission",
    "worker: question": "wait_question",
    "human label": "wait_question",
}
#: A last state never followed by another (a chantier gone without its last
#: one) stops counting a day after it began.
OPEN_STATE_MAX = DAY
RUNNER_UNIT = "actions-runner-"
#: A pull request's CI run: its event says so, its `pr` is often NULL.
PR_EVENT = "event = 'pull_request'"  # the units of the self-hosted runners

Rows = Iterable[tuple[float, float]]


def span_of(query: Mapping[str, str]) -> int | None:
    """The span asked (`?days=`), 30 by default; None for any other."""
    asked = query.get("days", str(DEFAULT_SPAN))
    return int(asked) if asked.isdigit() and int(asked) in SPANS else None


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).date().isoformat()


def _noon(day: str) -> float:
    return datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC).timestamp()


def _midnight(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()


def _share_since(days: list[str]) -> float:
    """The green share's window reaches back before the span's first day."""
    return _midnight(days[0]) - (CI_SHARE_DAYS - 1) * DAY


def _days(now: float, span: int) -> list[str]:
    return [_day(now - (span - 1 - i) * DAY) for i in range(span)]


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _p90(values: list[float]) -> float:
    kept = sorted(values)  # the nearest rank, as Quality counts it
    return float(kept[min(len(kept) - 1, int(0.9 * len(kept)))])


def _by_day(rows: Rows) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for at, value in rows:
        out[_day(at)].append(value)
    return out


def _measure(
    days: list[str], rows: Rows, how: Callable[[list[float]], float]
) -> list[float | None]:
    """A value per day from that day's readings; None without one."""
    got = _by_day(rows)
    return [round(how(got[d]), 2) if got.get(d) else None for d in days]


def _count(days: list[str], rows: Rows, first: float | None) -> list[float | None]:
    """A sum per day; None before the table's first row, 0 after it."""
    got = _by_day(rows)
    since = _day(first) if first is not None else None
    return [
        None if since is None or d < since else round(sum(got.get(d, [])), 2)
        for d in days
    ]


def green_share(days: list[str], greens: Rows, reds: Rows) -> list[float | None]:
    """Per day, green / (green + red) over the `CI_SHARE_DAYS` ending that
    day — the days before the span's first included, so a share does not
    change with the span; None when they hold neither."""
    green, red = _by_day(greens), _by_day(reds)
    since = _share_since(days)
    back = [_day(since + i * DAY) for i in range(CI_SHARE_DAYS - 1)]
    every = back + days
    out: list[float | None] = []
    for i in range(len(back), len(every)):
        window = every[i - CI_SHARE_DAYS + 1 : i + 1]
        g = sum(sum(green.get(d, [])) for d in window)
        r = sum(sum(red.get(d, [])) for d in window)
        out.append(round(g / (g + r), 4) if g + r else None)
    return out


def series(
    db: sqlite3.Connection,
    project: str,
    ci_workflow: str,
    base_branch: str,
    night_event: str,
    now: float,
    span: int,
) -> dict[str, list[float | None]]:
    days, start = _days(now, span), now - span * DAY

    def rows(sql: str, *args: object) -> list[tuple[float, float]]:
        return [
            (at, value)
            for at, value in db.execute(sql, args)
            if at is not None and value is not None
        ]

    def first(table: str, column: str, where: str = "1", *args: object) -> float | None:
        found = db.execute(
            f"SELECT min({column}) FROM {table} WHERE {where}", args
        ).fetchone()
        return found[0] if found else None

    ci = f"project = ? AND {store.workflow_is('workflow_path')}"
    # a pull request's CI by its event: GitHub often leaves its number out
    pr_runs = f"{ci} AND {PR_EVENT}"
    night = f"{ci} AND pr IS NULL AND branch = ? AND event = ?"
    ci_args = (project, ci_workflow, ci_workflow)
    run_at = "coalesce(started_at, created_at)"
    first_run = first("ci_run", run_at, pr_runs, *ci_args)

    def verdicts(where: str, since: float = start) -> list[tuple[float, float]]:
        return rows(
            f"SELECT {run_at}, 1 FROM ci_run"
            f" WHERE {pr_runs} AND {where} AND {run_at} >= ?",
            *ci_args,
            since,
        )

    durations = rows(
        "SELECT started_at, (finished_at - started_at) / 60.0"
        f" FROM ci_run WHERE {pr_runs}"
        " AND conclusion IN ('success', 'failure') AND started_at >= ?",
        *ci_args,
        start,
    )
    # each pull request's runs that day, by its branch: how many runs a change takes
    per_day: dict[tuple[str, str], int] = defaultdict(int)
    for at, branch in db.execute(
        f"SELECT {run_at}, branch FROM ci_run WHERE {pr_runs} AND {run_at} >= ?",
        (*ci_args, start),
    ):
        per_day[(_day(at), branch)] += 1
    per_pull = [(_noon(d), float(n)) for (d, _), n in per_day.items()]
    nights = db.execute(
        f"SELECT {run_at}, (finished_at - started_at) / 60.0, conclusion = 'success'"
        " FROM ci_run"
        f" WHERE {night} AND conclusion IN ('success', 'failure') AND {run_at} >= ?",
        (*ci_args, base_branch, night_event, start),
    ).fetchall()
    pulls = db.execute(
        "SELECT merged_at, created_at, closed_issues FROM pull_merged"
        " WHERE project = ? AND merged_at >= ?",
        (project, start),
    ).fetchall()
    stays = db.execute(
        "SELECT left_at, left_at - entered_at, outcome FROM merge_queue"
        " WHERE project = ? AND state = 'out' AND left_at >= ?",
        (project, start),
    ).fetchall()
    samples = rows(
        "SELECT at, memory_available_mb FROM vps_sample"
        " WHERE memory_available_mb IS NOT NULL AND at >= ?",
        start,
    )
    loads = [
        dict(
            zip(
                ("at", "load5", "cores", "cpu_percent", *LOAD_CONSUMERS), r, strict=True
            )
        )
        for r in db.execute(
            "SELECT at, load5, cores, cpu_percent, "
            + ", ".join(LOAD_CONSUMERS)
            + " FROM vps_load WHERE at >= ?",
            (start,),
        )
    ]

    def load_of(column: str, whole: bool = False) -> list[tuple[float, float]]:
        """One figure of the 5-minute readings (#2279), those that said it;
        `whole`: only the readings that said every consumer, so that the
        shares, stacked, make the CPU of those readings (#2280)."""
        return [
            (r["at"], r[column])
            for r in loads
            if r[column] is not None
            and not (whole and any(r[c] is None for c in LOAD_CONSUMERS))
        ]

    return {
        # CI: a pull request's runs, by verdict; their duration; the night's suite
        "runs_green": _count(days, verdicts("conclusion = 'success'"), first_run),
        "runs_red": _count(days, verdicts("conclusion = 'failure'"), first_run),
        "runs_other": _count(
            days, verdicts("conclusion NOT IN ('success', 'failure')"), first_run
        ),
        "green_share": green_share(
            days,
            verdicts("conclusion = 'success'", _share_since(days)),
            verdicts("conclusion = 'failure'", _share_since(days)),
        ),
        "runs_per_pr": _measure(days, per_pull, statistics.fmean),
        "ci_p50": _measure(days, durations, _median),
        "ci_p90": _measure(days, durations, _p90),
        "night_minutes": _measure(days, [(a, m) for a, m, _ in nights], max),
        "night_ok": _measure(days, [(a, ok) for a, _, ok in nights], min),
        # delivery
        "merged": _count(
            days,
            [(m, 1) for m, _, _ in pulls],
            first("pull_merged", "merged_at", "project = ?", project),
        ),
        "lead_h": _measure(
            days, [(m, (m - c) / 3600) for m, c, _ in pulls if c is not None], _median
        ),
        "tickets": _count(
            days,
            [(m, n) for m, _, n in pulls if n is not None],
            first("pull_merged", "merged_at", "project = ?", project),
        ),
        "deploys": _count(
            days,
            rows(
                "SELECT at, 1 FROM deployment WHERE project = ? AND at >= ?",
                project,
                start,
            ),
            first("deployment", "at", "project = ?", project),
        ),
        "mq_minutes": _measure(
            days, [(a, s / 60) for a, s, o in stays if o == "merged"], _median
        ),
        "mq_sent_back": _count(
            days,
            [(a, 1) for a, _, o in stays if o == "sent_back"],
            first("merge_queue", "entered_at", "project = ?", project),
        ),
        # workers
        "tokens_m": _count(
            days,
            rows(
                "SELECT c.finished_at, c.tokens_total / 1e6 FROM chantier c"
                " WHERE c.project = ? AND c.finished_at >= ?",
                project,
                start,
            ),
            first("chantier c", "c.finished_at", "c.project = ?", project),
        ),
        "quota_week": _measure(
            days,
            rows(
                "SELECT seen_at, used_percent FROM claude_quota_sample"
                " WHERE period = 'seven_day' AND seen_at >= ?",
                start,
            ),
            max,
        ),
        # the machine
        "mem_min_gb": _measure(days, [(a, mb / 1024) for a, mb in samples], min),
        "mem_low": _count(
            days,
            [(a, 1) for a, mb in samples if mb < MEMORY_LOW_MB],
            first("vps_sample", "at"),
        ),
        "alerts": _count(
            days,
            rows("SELECT opened_at, 1 FROM alert WHERE opened_at >= ?", start),
            first("alert", "opened_at"),
        ),
        # the load (#2280): the 5-minute readings' load and CPU, each
        # consumer's share of the machine — the mean of a day's periods
        "load_p50": _measure(days, load_of("load5"), _median),
        "load_max": _measure(days, load_of("load5"), max),
        "cores": _measure(days, load_of("cores"), max),
        "cpu_mean": _measure(days, load_of("cpu_percent"), statistics.fmean),
        **{
            name: _measure(days, load_of(name, whole=True), statistics.fmean)
            for name in LOAD_CONSUMERS
        },
        "disk_max": _measure(
            days,
            rows("SELECT at, disk_used_percent FROM vps_sample WHERE at >= ?", start),
            max,
        ),
    }


def _spread(spans: Iterable[tuple[str, float, float]]) -> dict[str, dict[str, float]]:
    """Minutes per key and per UTC day, each span cut at midnight."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for key, start, end in spans:
        while start < end:
            midnight = start - start % DAY + DAY
            out[key][_day(start)] += (min(end, midnight) - start) / 60
            start = midnight
    return out


def _over(
    days: list[str], minutes: dict[str, float], since: float | None
) -> list[float | None]:
    begun = None if since is None else _day(since)
    return [
        None if begun is None or d < begun else round(minutes.get(d, 0), 2)
        for d in days
    ]


def waits(
    db: sqlite3.Connection, project: str, days: list[str], now: float
) -> dict[str, list[float | None]]:
    """A worker's minutes of waiting per day, by cause: from each state to the
    next of its chantier; a refused state was never real."""
    rows = db.execute(
        "SELECT spec, at, cause FROM chantier_state WHERE project = ? AND refused = 0"
        " ORDER BY spec, at, id",
        (project,),
    ).fetchall()
    spans = []
    for i, (spec, at, cause) in enumerate(rows):
        later = rows[i + 1] if i + 1 < len(rows) and rows[i + 1][0] == spec else None
        end = later[1] if later else min(now, at + OPEN_STATE_MAX)
        key = WAIT_CAUSES.get(cause.split(", confirmed")[0])
        if key:
            spans.append((key, at, end))
    minutes, since = _spread(spans), min((r[1] for r in rows), default=None)
    return {
        key: _over(days, minutes[key], since)
        for key in sorted(set(WAIT_CAUSES.values()))
    }


def red_causes(
    db: sqlite3.Connection, project: str, ci_workflow: str, start: float
) -> tuple[Rows, Rows]:
    """A pull request's red runs: a test's when a failed job names a failed
    test; the machine's when every failed job's log was read and none names
    one (#1792); neither while a log is unread."""
    test, machine = [], []
    for at, named, unread in db.execute(
        "SELECT coalesce(r.started_at, r.created_at),"
        " sum((SELECT count(*) FROM ci_test_failure t"
        "  WHERE t.project = j.project AND t.job_id = j.job_id)),"
        " sum(NOT EXISTS (SELECT 1 FROM ci_log l"
        "  WHERE l.project = j.project AND l.job_id = j.job_id))"
        " FROM ci_run r JOIN ci_job j ON j.project = r.project"
        " AND j.run_id = r.run_id AND j.attempt = r.attempt"
        f" WHERE r.project = ? AND {store.workflow_is('r.workflow_path')}"
        " AND r.event = 'pull_request'"
        " AND r.conclusion = 'failure' AND j.conclusion = 'failure'"
        " AND coalesce(r.started_at, r.created_at) >= ? GROUP BY r.run_id, r.attempt",
        (project, ci_workflow, ci_workflow, start),
    ):
        if named:
            test.append((at, 1))
        elif not unread:
            machine.append((at, 1))
    return test, machine


def runner_busy(
    db: sqlite3.Connection, project: str, days: list[str], now: float
) -> dict[str, list[float | None]]:
    """Each self-hosted runner's busy share of the day, in % — of the part of
    today already gone; None before its first job."""
    rows = db.execute(
        "SELECT runner_name, started_at, completed_at FROM ci_job WHERE project = ?"
        " AND machine = 'vps' AND runner_name IS NOT NULL AND started_at IS NOT NULL",
        (project,),
    ).fetchall()
    minutes = _spread((name, a, b if b is not None else now) for name, a, b in rows)
    today, gone = _day(now), max(1.0, (now % DAY) / 60)  # midnight: never / 0
    out = {}
    for name in sorted(minutes):
        first = min(a for n, a, _ in rows if n == name)
        busy = _over(days, minutes[name], first)
        out[name] = [
            None
            if v is None
            else round(100 * v / (gone if d == today else DAY / 60), 2)
            for d, v in zip(days, busy, strict=True)
        ]
    return out


def red_runs(db: sqlite3.Connection, project: str, ci_workflow: str) -> dict[str, int]:
    """Per branch, its pull request's CI runs ended red (#2058): every commit,
    every attempt — a machine's red re-run is a second one; a run out of
    time too. A cancelled run is not: a new push or the merge queue's
    cancellation replaced it, nothing failed."""
    return {
        str(branch): int(n)
        for branch, n in db.execute(
            "SELECT branch, COUNT(*) FROM ci_run WHERE project = ?"
            f" AND {PR_EVENT} AND conclusion IN ('failure', 'timed_out')"
            f" AND {store.workflow_is('workflow_path')} GROUP BY branch",
            (project, ci_workflow, ci_workflow),
        )
    }


def first_green(
    db: sqlite3.Connection, project: str, ci_workflow: str, start: float
) -> Rows:
    """Per pull request merged in the span: 100 when its CI, run to a verdict
    before the merge, was green at first try, else 0 (Quality's rule,
    `green_at_first_try`)."""
    tries: dict[int, list[str]] = defaultdict(list)
    merged_at = {}
    for number, at, conclusion in db.execute(
        "SELECT p.number, p.merged_at, r.conclusion FROM pull_merged p JOIN ci_run r"
        " ON r.project = p.project AND r.branch = p.branch"
        " AND r.conclusion IN ('success', 'failure')"
        f" AND {store.workflow_is('r.workflow_path')} AND r.created_at <= p.merged_at"
        " WHERE p.project = ? AND p.merged_at >= ?"
        " ORDER BY p.number, r.created_at, r.attempt",
        (ci_workflow, ci_workflow, project, start),
    ):
        tries[number].append(conclusion)
        merged_at[number] = at
    return [
        (merged_at[n], 100.0 if green_at_first_try(t) else 0.0)
        for n, t in tries.items()
    ]


def recorded(
    db: sqlite3.Connection, project: str, ci_workflow: str, now: float, span: int
) -> dict[str, list[float | None]]:
    """The series #1822 added."""
    days, start = _days(now, span), now - span * DAY

    def first(sql: str, *args: object) -> float | None:
        return db.execute(sql, args).fetchone()[0]  # type: ignore[no-any-return]

    test, machine = red_causes(db, project, ci_workflow, start)
    greens = first_green(db, project, ci_workflow, start)
    first_red = first(
        "SELECT min(coalesce(started_at, created_at)) FROM ci_run"
        f" WHERE project = ? AND {store.workflow_is('workflow_path')}"
        f" AND {PR_EVENT} AND conclusion = 'failure'",
        project,
        ci_workflow,
        ci_workflow,
    )
    kills = db.execute(
        "SELECT at, unit FROM oom_kill WHERE at >= ?", (start,)
    ).fetchall()
    first_kill = first("SELECT min(at) FROM oom_kill")
    runner_kills = [(at, 1.0) for at, unit in kills if unit.startswith(RUNNER_UNIT)]
    waited = db.execute(
        "SELECT started_at, wait_s / 60.0 FROM ci_job"
        " WHERE project = ? AND machine = 'vps'"
        " AND wait_s IS NOT NULL AND started_at >= ?",
        (project, start),
    ).fetchall()
    # the merge queue's jobs (#2111): of an attempt started while a stay of
    # its pull request was in test — under the testing label
    queue_waited = db.execute(
        "SELECT j.started_at, j.wait_s / 60.0 FROM ci_job j JOIN ci_run r"
        " ON r.project = j.project AND r.run_id = j.run_id AND r.attempt = j.attempt"
        " WHERE j.project = ? AND j.machine = 'vps' AND j.wait_s IS NOT NULL"
        " AND j.started_at >= ? AND EXISTS (SELECT 1 FROM merge_queue q"
        " WHERE q.project = r.project AND q.pull = r.pr AND q.started_at IS NOT NULL"
        " AND r.started_at >= q.started_at - ?"
        " AND (q.left_at IS NULL OR r.started_at <= q.left_at))",
        (project, start, machine_red.QUEUE_SKEW_S),
    ).fetchall()
    costs = db.execute(
        "SELECT c.finished_at, c.tokens_total / 1e6 FROM chantier c"
        " WHERE c.project = ? AND c.finished_at >= ?",
        (project, start),
    ).fetchall()

    def count(sql: str, since_sql: str) -> list[float | None]:
        rows = [(at, 1.0) for (at,) in db.execute(sql, (project, start))]
        return _count(days, rows, first(since_sql, project))

    return {
        **waits(db, project, days, now),
        "red_test": _count(days, test, first_red),
        "red_machine": _count(days, machine, first_red),
        "nudge_worker": count(
            "SELECT at FROM nudge WHERE project = ? AND outcome = 'sent' AND at >= ?",
            "SELECT min(at) FROM nudge WHERE project = ?",
        ),
        "nudge_ci": count(
            "SELECT acted_at FROM ci_revival WHERE project = ? AND acted_at >= ?",
            "SELECT min(seen_at) FROM ci_revival WHERE project = ?",
        ),
        "nudge_machine": count(
            "SELECT at FROM machine_rerun"
            " WHERE project = ? AND by IN ('parked', 'no_worker') AND at >= ?",
            "SELECT min(at) FROM machine_rerun WHERE project = ?",
        ),
        "oom": _count(days, [(at, 1) for at, _ in kills], first_kill),
        "oom_runner": _count(days, runner_kills, first_kill),
        "runner_wait": _measure(days, waited, _median),
        "queue_runner_wait": _measure(days, queue_waited, _median),
        "cost_m": _measure(days, costs, statistics.fmean),
        "first_green": _measure(days, greens, statistics.fmean),
    }


def period(
    db: sqlite3.Connection, project: str, ci_workflow: str, now: float, span: int
) -> dict[str, float | None]:
    """The span's own figures for the tiles, from every observation in it —
    never a median of daily medians, nor a mean of daily shares."""
    start = now - span * DAY
    minutes = sorted(
        m
        for (m,) in db.execute(
            "SELECT (finished_at - started_at) / 60.0 FROM ci_run"
            f" WHERE project = ? AND {store.workflow_is('workflow_path')}"
            f" AND {PR_EVENT} AND conclusion IN ('success', 'failure')"
            " AND started_at >= ? AND finished_at IS NOT NULL",
            (project, ci_workflow, ci_workflow, start),
        )
    )
    greens = [v for _, v in first_green(db, project, ci_workflow, start)]
    return {
        "ci_p50": round(_median(minutes), 2) if minutes else None,
        "ci_p90": round(_p90(minutes), 2) if minutes else None,
        "first_green": round(statistics.fmean(greens), 2) if greens else None,
    }


def build_measures(
    path: Path,
    project: str,
    ci_workflow: str,
    base_branch: str,
    night_event: str,
    now: float,
    span: int,
    *,
    pace: Pace,
) -> dict[str, Any]:
    """The tab's one reading: the span's daily series, and the quota
    periods in progress (#2048) and the machine's last week (#2051), which no
    span changes."""
    try:
        db = open_read_only(path)
    except QueryError as exc:
        return {"error": str(exc)}
    with closing(db):
        try:
            return {
                "error": None,
                "now": now,
                "span": span,
                "days": _days(now, span),
                "series": {
                    **series(
                        db, project, ci_workflow, base_branch, night_event, now, span
                    ),
                    **recorded(db, project, ci_workflow, now, span),
                },
                "runners": runner_busy(db, project, _days(now, span), now),
                "period": period(db, project, ci_workflow, now, span),
                "flaky": flaky_tests(db, project, base_branch),
                "alerts": alert_episodes(db, now),
                "quotas": quotas(db, now, pace),
                "hours": machine_hours(db, now),
            }
        except sqlite3.Error as exc:
            return {"error": texts.BASE_UNREADABLE.format(error=exc)}
