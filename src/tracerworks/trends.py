"""The Tendances tab (#839): the history base, shaped for the page's curves.

Everything is computed here, the page only draws. Read only (`query.py`'s
connection). A part the base cannot give is None, said on the page.

Projection (#864, replacing #836's mean since the period's start): what the
period has used so far, plus the slope of the last rolling days carried to its
end — whatever period those days fall in, so the 1st of the month already has
one. Both weigh the latest days more, exponentially: each instant counts half
as much one half-life (48 h) further back (the operator, 05/10 — a uniform window fell
to nothing after one quiet Claude day, and kept billing the month for the CI
the VPS took over; the latest days lead, the week's trend stays).
- Actions minutes: the minutes GitHub billed for its jobs over the last 7 days
  (`[history] actions_slope_days`, `actions_half_life_hours`); the month runs
  from the 1st (UTC) to the next 1st.
- Claude weekly quota: over the last 7 days (`claude_week_slope_days`,
  `claude_week_half_life_hours`), each window's rise added up — a reset is
  not a fall; to the next reset.
Less history than the window: the slope over what there is, its span said; no
history: no projection.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from tracerworks import texts
from tracerworks.query import QueryError, open_read_only

DAY = 86400
WEEK = 7 * DAY
DAYS_SHOWN = 30  # tokens per day; CI per day while there is no run yet
CI_SHARE_DAYS = 3  # green share over the CI bars: rolling 3 days (11/10)
VPS_DAYS = 7  # memory, CPU, disk: hourly means over a week
NEW_WINDOW = 3600  # a reset time moved by more: the quota's next window


@dataclass(frozen=True)
class Slope:
    """A consumption per day, over `covered` seconds of history: the window
    of `window_days`, or less when the history is shorter. With
    `half_life_days`, a weighted mean: each instant counts half as much that
    many days further back."""

    per_day: float
    covered: float
    window_days: int
    half_life_days: float | None = None


def slope_said(slope: Slope | None) -> dict[str, Any] | None:
    """What the page says of a slope: its rate, and the days it covers."""
    if slope is None:
        return None
    return {
        "per_day": round(slope.per_day, 2),
        "days": round(slope.covered / DAY, 2),
        "window_days": slope.window_days,
        "half_life_days": slope.half_life_days,
    }


def projection(
    used: float, now: float, end: float, slope: Slope | None
) -> float | None:
    """`used` at `now`, carried to `end` along the slope."""
    if slope is None or now > end:
        return None
    return round(used + slope.per_day * (end - now) / DAY, 1)


def _weight(a: float, b: float, now: float, half_life: float | None) -> float:
    """What the span from `a` to `b` weighs: its length in seconds, each
    instant counting half as much `half_life` seconds further back from `now`."""
    if half_life is None:
        return b - a
    k = math.log(2) / half_life
    return (math.exp(-k * (now - b)) - math.exp(-k * (now - a))) / k


def _slope(
    total: float,
    first: float | None,
    now: float,
    days: int,
    half_life: float | None = None,
) -> Slope | None:
    """`total` consumed over the last `days`, or since `first` — the history's
    first moment — when it began later; weighted, over the span's weight."""
    if first is None:
        return None
    since = max(first, now - days * DAY)
    covered = now - since
    if covered <= 0:
        return None
    per_day = total / _weight(since, now, now, half_life) * DAY
    return Slope(per_day, covered, days, None if half_life is None else half_life / DAY)


def actions_slope(
    db: sqlite3.Connection,
    now: float,
    days: int,
    half_life_hours: int | None = None,
) -> Slope | None:
    """The minutes GitHub billed for the jobs that ended in the window, every
    project and month alike: the account has one quota — each job's weighing
    what the instant it ended does. The history begins with the first job of
    any machine: a day of VPS jobs only billed none."""
    half_life = None if half_life_hours is None else half_life_hours * 3600.0
    (first,) = db.execute("SELECT min(completed_at) FROM ci_job").fetchone()
    total = sum(
        float(minutes) * (1.0 if half_life is None else 0.5 ** ((now - at) / half_life))
        for at, minutes in db.execute(
            "SELECT completed_at, billed_minutes FROM ci_job"
            " WHERE machine = 'github' AND billed_minutes IS NOT NULL"
            " AND completed_at >= ? AND completed_at <= ?",
            (now - days * DAY, now),
        )
    )
    return _slope(total, first, now, days, half_life)


def rises(
    points: list[tuple[float, float, float]],
    start: float,
    now: float,
    half_life: float | None = None,
) -> float:
    """(seen at, used %, resets at), in order, one reading per instant: each
    window's rise since `start`, added up — a new window starts from zero,
    its reset is not a fall. A rise is spread evenly between its two
    readings: one across `start` counts for its share after, and with a
    half-life each share weighs what its instants do."""
    total = 0.0
    for (was, before, resets), (at, used, next_resets) in pairwise(points):
        rise = used if next_resets - resets > NEW_WINDOW else max(0.0, used - before)
        total += rise * _weight(max(was, start), at, now, half_life) / (at - was)
    return total


def claude_week_slope(
    db: sqlite3.Connection,
    now: float,
    days: int,
    half_life_hours: int | None = None,
) -> Slope | None:
    """The weekly quota's readings in the window, and the last one before it:
    the rise across the window's start is not lost between two readings."""
    start = now - days * DAY
    half_life = None if half_life_hours is None else half_life_hours * 3600.0
    points = [
        (float(at), float(used), float(resets))
        for at, used, resets in db.execute(
            "SELECT * FROM (SELECT seen_at, used_percent, resets_at"
            " FROM claude_quota_sample WHERE period = 'seven_day' AND seen_at < ?"
            " ORDER BY seen_at DESC LIMIT 1)"
            " UNION ALL SELECT seen_at, used_percent, resets_at"
            " FROM claude_quota_sample"
            " WHERE period = 'seven_day' AND seen_at >= ? AND seen_at <= ?"
            " ORDER BY seen_at",
            (start, start, now),
        )
    ]
    if len(points) < 2 or points[-1][0] < start:
        return None  # no rise read in the window
    return _slope(
        rises(points, start, now, half_life), points[0][0], now, days, half_life
    )


def slopes(
    path: Path,
    now: float,
    actions_days: int,
    claude_days: int,
    claude_half_life_hours: int | None = None,
    actions_half_life_hours: int | None = None,
) -> tuple[Slope | None, Slope | None]:
    """(Actions, Claude week) from the history base, for the dashboard's
    gauges; None for what it cannot give — no base, or an unreadable one."""
    try:
        db = open_read_only(path)
    except QueryError:
        return None, None
    with closing(db):
        try:
            return (
                actions_slope(db, now, actions_days, actions_half_life_hours),
                claude_week_slope(db, now, claude_days, claude_half_life_hours),
            )
        except sqlite3.Error:
            return None, None


@dataclass(frozen=True)
class Pace:
    """The `[history]` settings a projection follows: each slope's window,
    and its half-life (None: every instant weighs the same)."""

    actions_days: int
    claude_days: int
    actions_half_life_hours: int | None = None
    claude_half_life_hours: int | None = None


def month_bounds(now: float) -> tuple[float, float]:
    today = datetime.fromtimestamp(now, UTC)
    start = datetime(today.year, today.month, 1, tzinfo=UTC)
    end = (start + timedelta(days=32)).replace(day=1)
    return start.timestamp(), end.timestamp()


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d")


def _date(at: float) -> date:
    return datetime.fromtimestamp(at, UTC).date()


def _days(now: float, count: int) -> list[str]:
    return [_day(now - i * DAY) for i in range(count - 1, -1, -1)]


def claude_week(
    db: sqlite3.Connection,
    now: float,
    days: int,
    half_life_hours: int | None = None,
) -> dict[str, Any] | None:
    """The weekly window over the current week, and its projection — the
    5-hour one is the gauge's, not the week chart's (the operator, 05/10)."""
    last = db.execute(
        "SELECT seen_at, used_percent, resets_at FROM claude_quota_sample"
        " WHERE period = 'seven_day' ORDER BY seen_at DESC LIMIT 1"
    ).fetchone()
    if last is None or last[2] <= now:
        return None  # no reading, or the week it belongs to is over
    end = float(last[2])
    start = end - WEEK
    week = [
        [at, used]
        for at, used in db.execute(
            "SELECT seen_at, used_percent FROM claude_quota_sample"
            " WHERE period = 'seven_day' AND seen_at >= ? ORDER BY seen_at",
            (start,),
        )
    ]
    slope = claude_week_slope(db, now, days, half_life_hours)
    return {
        "start": start,
        "end": end,
        "seven_day": week,
        "projected": projection(float(last[1]), float(last[0]), end, slope),
        "slope": slope_said(slope),
    }


def actions_month(
    db: sqlite3.Connection,
    now: float,
    days: int,
    half_life_hours: int | None = None,
) -> dict[str, Any] | None:
    start, end = month_bounds(now)
    points = db.execute(
        "SELECT read_at, used_minutes, included_minutes FROM actions_minutes_sample"
        " WHERE read_at >= ? AND read_at < ? ORDER BY read_at",
        (start, end),
    ).fetchall()
    if not points:
        return None
    at, used, included = points[-1]
    slope = actions_slope(db, now, days, half_life_hours)
    return {
        "start": start,
        "end": end,
        "points": [[a, u] for a, u, _ in points],
        "included": included,
        "projected": projection(float(used), float(at), end, slope),
        "slope": slope_said(slope),
    }


def quotas(db: sqlite3.Connection, now: float, pace: Pace) -> dict[str, Any]:
    """The Claude week and the Actions month in progress, with their
    projections: Measures' QUOTAS section (#2048), whatever its span."""
    return {
        "claude": claude_week(db, now, pace.claude_days, pace.claude_half_life_hours),
        "actions": actions_month(
            db, now, pace.actions_days, pace.actions_half_life_hours
        ),
    }


def tokens_per_day(db: sqlite3.Connection, project: str, now: float) -> list[Any]:
    """[day, total, input, cache written, cache read, output] (#859: stacked)."""
    since = now - DAYS_SHOWN * DAY
    by_day = {
        day: list(sums)
        for day, *sums in db.execute(
            "SELECT date(finished_at, 'unixepoch'), sum(tokens_total),"
            " sum(tokens_input), sum(tokens_cache_write), sum(tokens_cache_read),"
            " sum(tokens_output) FROM chantier"
            " WHERE project = ? AND finished_at >= ? GROUP BY 1",
            (project, since),
        )
    }
    return [[day, *by_day.get(day, [0] * 5)] for day in _days(now, DAYS_SHOWN)]


def ci_per_day(db: sqlite3.Connection, project: str, now: float) -> list[Any]:
    """Finished attempts per day, from the project's first one (the last 30
    days while there is none): green (success) and red (failure) — the
    cancelled and skipped ones are neither — and the share of green over the
    `CI_SHARE_DAYS` ending that day, None when there was neither."""
    (first,) = db.execute(
        "SELECT min(finished_at) FROM ci_run WHERE project = ?", (project,)
    ).fetchone()
    shown = DAYS_SHOWN if first is None else (_date(now) - _date(first)).days + 1
    counts: dict[str, dict[str, int]] = {}
    for day, conclusion, n in db.execute(
        "SELECT date(finished_at, 'unixepoch'), conclusion, count(*) FROM ci_run"
        " WHERE project = ? AND conclusion IN ('success', 'failure') GROUP BY 1, 2",
        (project,),
    ):
        counts.setdefault(day, {})[conclusion] = n
    days = _days(now, shown)
    greens = [counts.get(day, {}).get("success", 0) for day in days]
    reds = [counts.get(day, {}).get("failure", 0) for day in days]
    rows: list[Any] = []
    for i, day in enumerate(days):
        start = max(0, i - CI_SHARE_DAYS + 1)
        green, red = sum(greens[start : i + 1]), sum(reds[start : i + 1])
        share = green / (green + red) if green + red else None
        rows.append([day, greens[i], reds[i], share])
    return rows


def vps_hours(db: sqlite3.Connection, now: float) -> list[Any]:
    """Hourly means over the week: memory available (Mo), CPU %, disk %.
    The health journal fills the hours before the live readings began."""
    return [
        list(row)
        for row in db.execute(
            "SELECT (at / 3600) * 3600 AS hour, round(avg(memory_available_mb)),"
            " round(avg(cpu_percent), 1), round(avg(disk_used_percent), 1)"
            " FROM vps_sample WHERE at >= ? GROUP BY hour ORDER BY hour",
            (now - VPS_DAYS * DAY,),
        )
    ]


def machine_hours(db: sqlite3.Connection, now: float) -> dict[str, Any]:
    """The week's hourly means, and the last total memory read: the scale
    of the memory curve (Measures' MACHINE section, #2051)."""
    total = db.execute(
        "SELECT memory_total_mb FROM vps_sample"
        " WHERE memory_total_mb IS NOT NULL ORDER BY at DESC LIMIT 1"
    ).fetchone()
    return {"vps": vps_hours(db, now), "memory_total_mb": total[0] if total else None}


def build_trends(
    path: Path,
    project: str,
    now: float,
    actions_days: int,
    claude_days: int,
    claude_half_life_hours: int | None = None,
    actions_half_life_hours: int | None = None,
) -> dict[str, Any]:
    try:
        db = open_read_only(path)
    except QueryError as exc:
        return {"error": str(exc)}
    with closing(db):
        try:
            hours = machine_hours(db, now)
            return {
                "error": None,
                "now": now,
                "claude": claude_week(db, now, claude_days, claude_half_life_hours),
                "actions": actions_month(
                    db, now, actions_days, actions_half_life_hours
                ),
                "tokens_per_day": tokens_per_day(db, project, now),
                "ci_per_day": ci_per_day(db, project, now),
                "vps": hours["vps"],
                "memory_total_mb": hours["memory_total_mb"],
            }
        except sqlite3.Error as exc:
            return {"error": texts.BASE_UNREADABLE.format(error=exc)}
