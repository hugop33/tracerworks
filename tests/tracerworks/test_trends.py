"""The Tendances tab (#839): projections and series, on fixed data."""

from __future__ import annotations

import math
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracerworks import store
from tracerworks.trends import Slope, build_trends, month_bounds, projection

REPO = "owner/repo"
NOW = datetime(2026, 9, 16, 0, 0, tzinfo=UTC).timestamp()  # mid-September
SEPT, OCT = (
    datetime(2026, 9, 1, tzinfo=UTC).timestamp(),
    datetime(2026, 10, 1, tzinfo=UTC).timestamp(),
)
DAY = 86400


def test_the_projection_carries_the_slope_to_the_end() -> None:
    # 1500 used, 100 a day over the 15 days left → 3000
    assert projection(1500, NOW, OCT, Slope(100, 7 * DAY, 7)) == 3000.0
    # the very first hour of the month already has one (#864)
    assert projection(3, SEPT + 3600, OCT, Slope(0, DAY, 7)) == 3.0


def test_no_projection_without_a_slope_or_past_the_end() -> None:
    assert projection(10, NOW, OCT, None) is None
    assert projection(10, OCT + 1, OCT, Slope(1, DAY, 7)) is None


def test_the_month_runs_from_the_first_to_the_next_first() -> None:
    assert month_bounds(NOW) == (SEPT, OCT)
    december = datetime(2026, 12, 31, 23, tzinfo=UTC).timestamp()
    assert month_bounds(december)[1] == datetime(2027, 1, 1, tzinfo=UTC).timestamp()


def base(tmp_path: Path) -> Path:
    path = tmp_path / "f.sqlite3"
    week_end = NOW + 4 * DAY  # a third... of a week: 3 days in, 4 to go
    with closing(store.connect(path)) as db:
        store.insert(
            db,
            "claude_quota_sample",
            [
                {"period": "seven_day", "seen_at": NOW - DAY, "used_percent": 20,
                 "resets_at": week_end},
                {"period": "seven_day", "seen_at": NOW, "used_percent": 30,
                 "resets_at": week_end},
                {"period": "five_hour", "seen_at": NOW, "used_percent": 55,
                 "resets_at": NOW + 3600},
                # the week before: not shown
                {"period": "seven_day", "seen_at": NOW - 5 * DAY, "used_percent": 90,
                 "resets_at": week_end - 7 * DAY},
            ],
        )  # fmt: skip
        store.insert(
            db,
            "actions_minutes_sample",
            [
                {"read_at": SEPT - DAY, "account": "o", "year": 2026, "month": 8,
                 "used_minutes": 2900, "included_minutes": 3000},
                {"read_at": NOW - DAY, "account": "o", "year": 2026, "month": 9,
                 "used_minutes": 1400, "included_minutes": 3000},
                {"read_at": NOW, "account": "o", "year": 2026, "month": 9,
                 "used_minutes": 1500, "included_minutes": 3000},
            ],
        )  # fmt: skip
        store.insert(
            db,
            "chantier",
            [
                {"project": REPO, "spec": s, "finished_at": NOW - d * DAY + 60,
                 "duration_s": 1, "tokens_total": t, "tokens_input": t // 10,
                 "tokens_cache_write": t // 5, "tokens_cache_read": t // 2,
                 "tokens_output": t - t // 10 - t // 5 - t // 2,
                 "messages": 1, "source": "clean"}
                for s, d, t in ((1, 0, 100), (2, 0, 50), (3, 2, 7), (4, 40, 999))
            ]
            + [
                {"project": "other/repo", "spec": 9, "finished_at": NOW,
                 "duration_s": 1, "tokens_total": 5000, "tokens_input": 0,
                 "tokens_cache_write": 0, "tokens_cache_read": 0,
                 "tokens_output": 0, "messages": 1, "source": "clean"}
            ],
        )  # fmt: skip
        runs = [
            (1, "success", NOW - 60), (2, "failure", NOW - 120),
            (3, "success", NOW - DAY), (4, "cancelled", NOW - 60),
            (5, "skipped", NOW - 60),
        ]  # fmt: skip
        store.insert(
            db,
            "ci_run",
            [
                {"project": REPO, "run_id": i, "attempt": 1, "workflow": "CI",
                 "workflow_path": "ci.yml", "event": "push", "branch": "b",
                 "head_sha": "s", "conclusion": c, "finished_at": at,
                 "title": "", "url": ""}
                for i, c, at in runs
            ],
        )  # fmt: skip
        store.insert(
            db,
            "vps_sample",
            [
                {"at": NOW - 3600 + 60, "source": "live", "memory_available_mb": 4000,
                 "memory_total_mb": 11000, "cpu_percent": 10, "disk_used_percent": 70},
                {"at": NOW - 3600 + 120, "source": "live", "memory_available_mb": 6000,
                 "memory_total_mb": 11000, "cpu_percent": 30, "disk_used_percent": 72},
                {"at": NOW - 8 * DAY, "source": "health_log",
                 "memory_available_mb": 1, "disk_used_percent": 1},
            ],
        )  # fmt: skip
    return path


def test_the_trends_of_a_fixed_base(tmp_path: Path) -> None:
    data = build_trends(base(tmp_path), REPO, NOW, 7, 2)
    assert data["error"] is None
    claude = data["claude"]
    assert (claude["start"], claude["end"]) == (NOW - 3 * DAY, NOW + 4 * DAY)
    assert claude["seven_day"] == [[NOW - DAY, 20.0], [NOW, 30.0]]
    assert "five_hour" not in claude  # the week alone (the operator, 05/10)
    # the new week's 20 % rose over the 4 days since the last reading of the
    # week before, one of them in the 2-day window: 5, then +10 — 7.5 a day
    assert claude["slope"] == {
        "per_day": 7.5,
        "days": 2.0,
        "window_days": 2,
        "half_life_days": None,
    }
    assert claude["projected"] == 60.0
    actions = data["actions"]
    assert actions["points"] == [[NOW - DAY, 1400.0], [NOW, 1500.0]]  # not August
    # no job in the base: no slope, no projection — never a made-up rate
    assert (actions["included"], actions["projected"]) == (3000, None)
    # by category, stacked (#859): input, cache written, cache read, output
    tokens = {day: rest for day, *rest in data["tokens_per_day"]}
    assert len(tokens) == 30 and tokens["2026-09-16"] == [150, 15, 30, 75, 30]
    assert tokens["2026-09-14"] == [7, 0, 1, 3, 3]
    assert sum(t[0] for t in tokens.values()) == 157  # 40 days ago: out
    assert tokens["2026-09-15"] == [0, 0, 0, 0, 0]
    ci = {day: (green, red) for day, green, red, _ in data["ci_per_day"]}
    # cancelled and skipped are neither green nor red
    assert ci["2026-09-15"] == (2, 1) and ci["2026-09-16"] == (0, 0)
    assert list(ci) == ["2026-09-15", "2026-09-16"]  # from the first run, not 30 days
    assert data["vps"] == [[NOW - 3600, 5000.0, 20.0, 71.0]]
    assert data["memory_total_mb"] == 11000


def test_an_empty_base_has_no_curve_and_no_zero_made_up(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    store.connect(path).close()
    data = build_trends(path, REPO, NOW, 7, 2)
    assert data["claude"] is None and data["actions"] is None
    assert data["vps"] == [] and data["memory_total_mb"] is None


def ci_runs(db: Any, runs: list[tuple[str, float, str]]) -> None:
    """(conclusion, finished at, project)"""
    store.insert(
        db,
        "ci_run",
        [
            {"project": project, "run_id": i, "attempt": 1, "workflow": "CI",
             "workflow_path": "ci.yml", "event": "push", "branch": "b",
             "head_sha": "s", "conclusion": c, "finished_at": at,
             "title": "", "url": ""}
            for i, (c, at, project) in enumerate(runs)
        ],
    )  # fmt: skip


def test_the_ci_runs_from_the_project_s_first_run_with_its_green_share(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        ci_runs(
            db,
            [
                (
                    "failure",
                    NOW - 60 * DAY,
                    "other/repo",
                ),  # another project's: not its start
                ("success", NOW - 40 * DAY, REPO),
                ("failure", NOW - 40 * DAY + 60, REPO),
                ("failure", NOW - 40 * DAY + 120, REPO),
                ("success", NOW - 35 * DAY, REPO),
                ("success", NOW - 2 * DAY, REPO),
            ],
        )
    days = build_trends(path, REPO, NOW, 7, 2)["ci_per_day"]
    assert days[0][0] == "2026-08-07" and days[-1][0] == "2026-09-16"
    assert len(days) == 41  # 40 days ago, kept: the whole history
    share = {day: s for day, _, _, s in days}
    # the share of green over the 3 days ending that day
    assert share["2026-08-07"] == 1 / 3
    assert share["2026-08-09"] == 1 / 3  # the 7th still in the window
    assert share["2026-08-10"] is None  # the 7th has left it, nothing since
    assert share["2026-08-12"] == 1.0
    assert share["2026-08-14"] == 1.0  # the 12th still in it
    assert share["2026-08-19"] is None  # no green nor red in 3 days: no share
    assert share["2026-09-14"] == 1.0 and share["2026-09-16"] == 1.0


def test_an_empty_base_has_30_days_of_ci_and_no_share(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    store.connect(path).close()
    days = build_trends(path, REPO, NOW, 7, 2)["ci_per_day"]
    assert len(days) == 30 and days[-1] == ["2026-09-16", 0, 0, None]


def test_a_week_already_over_is_not_shown(tmp_path: Path) -> None:
    data = build_trends(base(tmp_path), REPO, NOW + 5 * DAY, 7, 2)
    assert data["claude"] is None


def test_no_base_is_said(tmp_path: Path) -> None:
    assert (
        "no history base"
        in build_trends(tmp_path / "none.sqlite3", REPO, NOW, 7, 2)["error"]
    )


# ---- #864: the slope of the last rolling days, whatever period they fall in

OCT_1 = OCT + 2 * 3600  # 1 October, 02:00: two hours into the month
NOV = datetime(2026, 11, 1, tzinfo=UTC).timestamp()


def jobs(db: Any, ended: list[tuple[float, int, str]]) -> None:
    """(completed at, billed minutes, machine)"""
    store.insert(
        db,
        "ci_run",
        [{"project": REPO, "run_id": 1, "attempt": 1, "workflow": "CI",
          "workflow_path": "ci.yml", "event": "push", "branch": "b",
          "head_sha": "s", "conclusion": "success", "title": "", "url": ""}],
    )  # fmt: skip
    store.insert(
        db,
        "ci_job",
        [
            {"project": REPO, "job_id": i, "run_id": 1, "attempt": 1, "name": "j",
             "machine": machine, "conclusion": "success", "completed_at": at,
             "billed_minutes": minutes, "url": ""}
            for i, (at, minutes, machine) in enumerate(ended)
        ],
    )  # fmt: skip


def week_samples(db: Any, samples: list[tuple[float, float, float]]) -> None:
    """(seen at, used %, resets at)"""
    store.insert(
        db,
        "claude_quota_sample",
        [
            {"period": "seven_day", "seen_at": at, "used_percent": used,
             "resets_at": resets}
            for at, used, resets in samples
        ],
    )  # fmt: skip


def test_the_first_of_the_month_projects_from_the_month_before(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        jobs(
            db,
            [
                (OCT_1 - 9 * DAY, 999, "github"),  # before the 7 days: out
                (OCT_1 - 6 * DAY, 300, "github"),  # September: in
                (OCT_1 - DAY, 400, "github"),
                (OCT_1 - 3600, 0, "vps"),  # our runner: never billed
            ],
        )
        store.insert(
            db,
            "actions_minutes_sample",
            [{"read_at": OCT_1, "account": "o", "year": 2026, "month": 10,
              "used_minutes": 6, "included_minutes": 3000}],
        )  # fmt: skip
    actions = build_trends(path, REPO, OCT_1, 7, 2)["actions"]
    assert actions["slope"] == {
        "per_day": 100.0,
        "days": 7.0,
        "window_days": 7,
        "half_life_days": None,
    }
    # 6 used, then 100 a day over the 30 days and 22 hours left
    assert actions["projected"] == round(6 + 100 * (NOV - OCT_1) / DAY, 1)
    # the operator, 05/10: the minutes too, each job weighing half as much one
    # half-life further back — the CI moved to the VPS, the month follows
    weighted = build_trends(path, REPO, OCT_1, 7, 2, actions_half_life_hours=24)
    # 300 six days ago at 2^-6, 400 a day ago at 2^-1, over the week's weight
    per_day = (300 / 64 + 400 / 2) / ((1 - 2**-7) / math.log(2))
    assert weighted["actions"]["slope"] == {
        "per_day": round(per_day, 2),
        "days": 7.0,
        "window_days": 7,
        "half_life_days": 1.0,
    }
    assert weighted["actions"]["projected"] == round(
        6 + per_day * (NOV - OCT_1) / DAY, 1
    )


def test_a_claude_reset_inside_the_window_adds_each_window_rise(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f.sqlite3"
    reset = NOW - DAY  # the old week ended yesterday
    with closing(store.connect(path)) as db:
        week_samples(
            db,
            [
                (NOW - 3 * DAY, 40, reset),  # before the 2 days: out
                (NOW - 2 * DAY, 80, reset),
                (NOW - DAY - 60, 90, reset),  # +10
                (NOW - DAY + 60, 4, reset + 7 * DAY),  # new week: +4, not -86
                (NOW, 16, reset + 7 * DAY),  # +12
            ],
        )
    claude = build_trends(path, REPO, NOW, 7, 2)["claude"]
    assert claude["slope"] == {
        "per_day": 13.0,
        "days": 2.0,
        "window_days": 2,
        "half_life_days": None,
    }
    assert claude["projected"] == 16 + 13 * 6  # six days to the reset


def test_a_history_shorter_than_the_window_gives_the_slope_over_what_there_is(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        jobs(db, [(NOW - 2 * DAY, 100, "github"), (NOW - DAY, 200, "github")])
        store.insert(
            db,
            "actions_minutes_sample",
            [{"read_at": NOW, "account": "o", "year": 2026, "month": 9,
              "used_minutes": 1000, "included_minutes": 3000}],
        )  # fmt: skip
        week_samples(db, [(NOW - DAY / 2, 10, NOW + DAY), (NOW, 13, NOW + DAY)])
    data = build_trends(path, REPO, NOW, 7, 2)
    # the history starts at the first job, two days ago: 300 in 2 days
    assert data["actions"]["slope"] == {
        "per_day": 150.0,
        "days": 2.0,
        "window_days": 7,
        "half_life_days": None,
    }
    assert data["actions"]["projected"] == 1000 + 150 * 15
    assert data["claude"]["slope"] == {
        "per_day": 6.0,
        "days": 0.5,
        "window_days": 2,
        "half_life_days": None,
    }
    assert data["claude"]["projected"] == 19.0


def test_no_history_in_the_window_no_projection(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        week_samples(db, [(NOW, 13, NOW + DAY)])  # one reading: no rise yet
        store.insert(
            db,
            "actions_minutes_sample",
            [{"read_at": NOW, "account": "o", "year": 2026, "month": 9,
              "used_minutes": 1000, "included_minutes": 3000}],
        )  # fmt: skip
    data = build_trends(path, REPO, NOW, 7, 2)
    assert data["claude"]["projected"] is None and data["claude"]["slope"] is None
    assert data["actions"]["projected"] is None and data["actions"]["slope"] is None


def test_a_half_life_weighs_the_latest_days_more_and_keeps_the_week(
    tmp_path: Path,
) -> None:
    """the operator, 05/10: the slope follows the latest days but keeps the week's
    trend — each instant counts half as much one half-life further back. The
    same +10 over the last day and six days ago: the recent one weighs 64
    times more, the old one still counts."""
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        week_samples(
            db,
            [
                (NOW - 7 * DAY, 0, NOW + DAY),
                (NOW - 6 * DAY, 10, NOW + DAY),  # +10, a week ago
                (NOW - DAY, 10, NOW + DAY),  # five quiet days
                (NOW, 20, NOW + DAY),  # +10, over the last day
            ],
        )
    flat = build_trends(path, REPO, NOW, 7, 7)["claude"]
    assert flat["slope"]["per_day"] == round(20 / 7, 2)
    claude = build_trends(path, REPO, NOW, 7, 7, claude_half_life_hours=24)["claude"]
    # each day's weight over the week's: 2^-6 and 2^0 over 2 - 2^-6, of 10 a day
    per_day = 10 * (1 / 64 + 1) / (2 - 1 / 64)
    assert claude["slope"] == {
        "per_day": round(per_day, 2),
        "days": 7.0,
        "window_days": 7,
        "half_life_days": 1.0,
    }
    assert claude["projected"] == round(20 + per_day, 1)  # a day to the reset


def test_sparse_readings_count_the_rise_across_the_window_s_start(
    tmp_path: Path,
) -> None:
    """A reading before the window and the first one in it: the rise between
    them counts for the part of it inside the window — never dropped while
    its time is counted."""
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        week_samples(
            db,
            [
                (NOW - 3 * DAY, 20, NOW + DAY),
                (NOW - DAY, 40, NOW + DAY),  # +20 over 2 days: 10 in the window
                (NOW, 50, NOW + DAY),  # +10
            ],
        )
    claude = build_trends(path, REPO, NOW, 7, 2)["claude"]
    assert claude["slope"] == {
        "per_day": 10.0,
        "days": 2.0,
        "window_days": 2,
        "half_life_days": None,
    }
