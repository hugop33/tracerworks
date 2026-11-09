"""The Measures tab (#1821): each daily series on a fixture base, over 7, 30
or 90 days; a day without data is a gap, never an invented zero."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from tracerworks import oom, store
from tracerworks.measures import SPANS, build_measures, span_of
from tracerworks.process import Result
from tracerworks.trends import Pace, build_trends

REPO = "owner/repo"
WORKER = "tracerworks-worker-"  # a worker's scope's prefix (#2095)
DAY = 86400
NOW = 1_790_000_000 - 1_790_000_000 % DAY + 20 * 3600  # 20:00 UTC
TODAY = NOW - NOW % DAY
_ids = count(1)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "f.sqlite3"


@pytest.fixture
def db(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(path)) as base:
        yield base


def at(days_ago: int, hour: float = 10) -> int:
    return int(TODAY - days_ago * DAY + hour * 3600)


def run(
    db: sqlite3.Connection,
    when: int,
    conclusion: str = "success",
    minutes: float = 20,
    pr: int | None = 7,
    branch: str = "agent/7",
    event: str = "pull_request",
    path: str = ".github/workflows/ci.yml",
) -> None:
    run_id = next(_ids)
    store.insert(db, "ci_run", [{
        "project": REPO, "run_id": run_id, "attempt": 1, "workflow": "CI",
        "workflow_path": path, "event": event, "branch": branch, "head_sha": "s",
        "pr": pr, "conclusion": conclusion, "created_at": when, "started_at": when,
        "finished_at": when + int(minutes * 60), "title": "",
        "url": f"u{run_id}"}])  # fmt: skip


def merged(
    db: sqlite3.Connection,
    number: int,
    when: int,
    hours: float = 5,
    closed: int | None = 2,
) -> None:
    store.insert(db, "pull_merged", [{
        "project": REPO, "number": number, "title": "t", "branch": f"b{number}",
        "created_at": when - int(hours * 3600), "merged_at": when, "url": "u",
        "closed_issues": closed}])  # fmt: skip


def sample(db: sqlite3.Connection, when: int, memory_mb: int, disk: float = 70) -> None:
    store.insert(db, "vps_sample", [{
        "at": when, "source": "health_log", "memory_available_mb": memory_mb,
        "memory_total_mb": 16000, "disk_used_percent": disk}])  # fmt: skip


PACE = Pace(
    actions_days=7, claude_days=7, actions_half_life_hours=48, claude_half_life_hours=24
)


def measures(path: Path, span: int = 7, now: float = NOW) -> dict[str, Any]:
    return build_measures(
        path, REPO, "ci.yml", "main", "workflow_dispatch", now, span, pace=PACE
    )


def test_the_days_end_today_one_per_day_in_utc(
    db: sqlite3.Connection, path: Path
) -> None:
    got = measures(path, 7)
    assert got["error"] is None
    assert len(got["days"]) == 7
    assert got["days"][-1] == "2026-09-21"
    assert got["days"][0] == "2026-09-15"
    assert [len(measures(path, n)["days"]) for n in SPANS] == [7, 30, 90]


def test_a_span_is_7_30_or_90_and_30_by_default() -> None:
    assert span_of({}) == 30
    assert span_of({"days": "90"}) == 90
    for wrong in ("12", "x", "-7", "9000"):
        assert span_of({"days": wrong}) is None


def test_pull_request_runs_per_day_by_verdict(
    db: sqlite3.Connection, path: Path
) -> None:
    run(db, at(1), "success")
    run(db, at(1, 11), "failure")
    run(db, at(1, 12), "cancelled")
    run(db, at(0), "success")
    run(db, at(0), "success", pr=None, branch="main", event="push")  # not a PR's CI
    run(db, at(0), "success", path=".github/workflows/deploy.yml")  # not the CI
    s = measures(path)["series"]
    assert s["runs_green"][-2:] == [1, 1]
    assert s["runs_red"][-2:] == [1, 0]
    assert s["runs_other"][-2:] == [1, 0]
    assert s["runs_per_pr"][-2:] == [3, 1], "one PR, its three runs that day"


def test_a_pull_request_run_without_its_number_still_counts(
    db: sqlite3.Connection, path: Path
) -> None:
    """GitHub leaves `pr` out of most pull request runs (731 of 1 115 on
    25/10): the event says it is one (review of #1834)."""
    run(db, at(1), "success", pr=None)
    run(db, at(1, 11), "failure", pr=None, branch="agent/8")
    s = measures(path)["series"]
    assert [s["runs_green"][-2], s["runs_red"][-2]] == [1, 1]
    assert s["runs_per_pr"][-2] == 1, "two branches, one run each"


def test_the_span_s_own_median_never_the_median_of_daily_ones(
    db: sqlite3.Connection, path: Path
) -> None:
    for _ in range(5):
        run(db, at(2), "success", 1)
    run(db, at(1), "success", 100)
    got = measures(path)
    assert got["series"]["ci_p50"][-3:-1] == [1, 100]
    assert got["period"]["ci_p50"] == 1, "five one-minute runs and one of 100"
    assert got["period"]["ci_p90"] == 100


def test_a_count_before_its_first_record_is_a_gap_after_it_a_zero(
    db: sqlite3.Connection, path: Path
) -> None:
    run(db, at(3))
    s = measures(path)["series"]
    assert s["runs_green"] == [None, None, None, 1, 0, 0, 0]
    assert s["merged"] == [None] * 7, "nothing merged ever: unknown, not zero"


def test_ci_duration_median_and_p90_of_the_day_in_minutes(
    db: sqlite3.Connection, path: Path
) -> None:
    for minutes in (10, 20, 30, 40, 100):
        run(db, at(1), "success", minutes)
    run(db, at(1), "cancelled", 500)  # judged nothing: not a duration
    s = measures(path)["series"]
    assert s["ci_p50"][-2:] == [30, None]
    assert s["ci_p90"][-2] == 100, "the nearest rank, as Quality counts a p90"
    assert s["ci_p90"][-1] is None, "a day without a run: a gap, never zero"


def test_the_night_suite_its_duration_and_its_verdict(
    db: sqlite3.Connection, path: Path
) -> None:
    run(
        db, at(2, 1.5), "success", 95, pr=None, branch="main", event="workflow_dispatch"
    )
    run(
        db,
        at(1, 1.5),
        "failure",
        101,
        pr=None,
        branch="main",
        event="workflow_dispatch",
    )
    s = measures(path)["series"]
    assert s["night_minutes"][-3:] == [95, 101, None]
    assert s["night_ok"][-3:] == [1, 0, None]


def test_merged_pulls_their_lead_time_and_the_issues_they_closed(
    db: sqlite3.Connection, path: Path
) -> None:
    merged(db, 1, at(1), hours=2, closed=1)
    merged(db, 2, at(1, 12), hours=10, closed=3)
    merged(db, 3, at(0), hours=4, closed=None)  # not read yet: no ticket said
    s = measures(path)["series"]
    assert s["merged"][-2:] == [2, 1]
    assert s["lead_h"][-2:] == [6, 4]
    assert s["tickets"][-2:] == [4, 0]


def test_deployments_and_the_merge_queue(db: sqlite3.Connection, path: Path) -> None:
    store.insert(
        db,
        "deployment",
        [{"project": REPO, "run_id": 1, "head_sha": "s", "at": at(1), "url": "u"}],
    )
    stay = {"project": REPO, "entered_at": at(1), "state": "out"}
    store.insert(db, "merge_queue", [
        {**stay, "pull": 1, "left_at": at(1) + 3000, "outcome": "merged"},
        {**stay, "pull": 2, "left_at": at(1) + 600, "outcome": "sent_back"},
    ])  # fmt: skip
    s = measures(path)["series"]
    assert s["deploys"][-2:] == [1, 0]
    assert s["mq_minutes"][-2:] == [50, None]
    assert s["mq_sent_back"][-2:] == [1, 0]


def test_memory_lowest_alerts_and_disk_highest_of_the_day(
    db: sqlite3.Connection, path: Path
) -> None:
    sample(db, at(1, 1), 3072, 71)
    sample(db, at(1, 2), 1024, 74)
    sample(db, at(1, 3), 1400, 72)
    sample(db, at(1, 4), 1600, 72)  # above 1.5 GB: no alert
    s = measures(path)["series"]
    assert s["mem_min_gb"][-2:] == [1.0, None]
    assert s["mem_low"][-2:] == [2, 0], "readings under 1.5 GB"
    assert s["disk_max"][-2:] == [74, None]


def load(
    db: sqlite3.Connection, when: int, load5: float | None, **cpu: float | None
) -> None:
    store.insert(db, "vps_load", [{"at": when, "load5": load5, "cores": 6, **cpu}])


def test_the_load_median_and_peak_the_cpu_and_each_consumer_per_day(
    db: sqlite3.Connection, path: Path
) -> None:
    """#2280: the 5-minute readings (#2279); a day without one is a gap.
    The shares are those of the readings that said every consumer: stacked,
    they make the CPU of those readings; a reading with a `?` counts for the
    day's CPU only."""
    whole = {"cpu_ci": 50, "cpu_factory": 2, "cpu_containers": 8, "cpu_rest": 10}
    load(db, at(1, 1), 4, cpu_percent=20, cpu_workers=10, cpu_ci=None)
    load(db, at(1, 2), 16.5, cpu_percent=90, cpu_workers=20, **whole)
    load(db, at(1, 3), 8, cpu_percent=40, cpu_workers=30, cpu_ci=None)
    load(db, at(3, 1), None, cpu_percent=None)  # a first reading: all `?`
    s = measures(path)["series"]
    assert s["load_p50"][-4:] == [None, None, 8, None]
    assert s["load_max"][-4:] == [None, None, 16.5, None]
    assert s["cores"][-2:] == [6, None]
    assert s["cpu_mean"][-4:] == [None, None, 50, None]
    shares = ("cpu_workers", "cpu_ci", "cpu_factory", "cpu_containers", "cpu_rest")
    assert [s[k][-2] for k in shares] == [20, 50, 2, 8, 10], "the whole reading's"
    assert [s[k][-1] for k in shares] == [None] * 5, "no reading today: a gap"


def test_a_consumer_never_measured_on_a_day_is_a_gap(
    db: sqlite3.Connection, path: Path
) -> None:
    load(db, at(1, 1), 4, cpu_percent=20, cpu_workers=10, cpu_ci=None)
    s = measures(path)["series"]
    assert s["cpu_mean"][-2] == 20
    assert s["cpu_workers"][-2] is None and s["cpu_ci"][-2] is None


def test_tokens_quota_and_alerts_per_day(db: sqlite3.Connection, path: Path) -> None:
    store.insert(db, "chantier", [{
        "project": REPO, "spec": 9, "finished_at": at(1), "tokens_total": 3_500_000,
        "tokens_input": 0, "tokens_cache_write": 0, "tokens_cache_read": 0,
        "tokens_output": 0, "messages": 1, "source": "clean"}])  # fmt: skip
    store.insert(db, "claude_quota_sample", [
        {"period": "seven_day", "seen_at": at(1), "used_percent": 40, "resets_at": NOW},
        {"period": "seven_day", "seen_at": at(1, 15), "used_percent": 47.5,
         "resets_at": NOW},
        {"period": "five_hour", "seen_at": at(1), "used_percent": 90, "resets_at": NOW},
    ])  # fmt: skip
    store.insert(db, "alert", [{"key": "memory", "opened_at": at(1), "text": "low"}])
    s = measures(path)["series"]
    assert s["tokens_m"][-2:] == [3.5, 0]
    assert s["quota_week"][-2:] == [47.5, None]
    assert s["alerts"][-2:] == [1, 0]
    assert measures(path)["alerts"][0]["text"] == "low"


def test_every_series_has_a_value_per_day(path: Path, db: sqlite3.Connection) -> None:
    got = measures(path, 30)
    assert "not_measured" not in got
    assert len(got["series"]) == 45  # + the load and its consumers (#2280)
    assert all(len(v) == 30 for v in got["series"].values())


def test_the_oom_kills_are_read_from_the_session_s_journal() -> None:
    journal = (
        "1792915189.123456 vps systemd[71060]: actions-runner-vps-1.service:"
        " Failed with result 'oom-kill'.\n"
        "1792915190.000001 vps systemd[71060]: ci.slice: The kernel OOM killer killed"
        " some processes in this unit.\n"
    )
    assert oom.rows(journal, WORKER) == [
        {"at": 1792915189, "unit": "actions-runner-vps-1.service"}
    ]
    asked: list[list[str]] = []

    def runner(answer: Result) -> Any:
        def run(argv: Any, **_: Any) -> Result:
            asked.append(list(argv))
            return answer

        return run

    assert oom.read(runner(Result(0, journal)), 1_000_000, WORKER) == oom.rows(
        journal, WORKER
    )
    assert asked[0][:3] == ["journalctl", "--user", "--no-pager"]
    assert "@827200" in asked[0], "two days back"
    assert oom.read(runner(Result(1, "")), 1_000_000, WORKER) == [], (
        "nothing matched: none"
    )
    assert oom.read(runner(Result(1, "", "Failed to open")), 1_000_000, WORKER) == []
    assert oom.read(runner(Result(1, "x", "")), 1_000_000, WORKER) is None
    assert oom.read(runner(Result(127, "", "no journalctl")), 1_000_000, WORKER) is None


def test_a_kill_inside_a_worker_s_scope_is_read_in_its_name() -> None:
    """#2095: a worker's scope is `OOMPolicy=continue` — the kernel kills its
    test, the worker lives on, and systemd never writes « Failed with result »
    for it. Its own line is read instead, for a worker's scope only: a service
    (a runner) writes both, another scope or a slice is no one's (measured on
    the VPS 30/10)."""
    journal = (
        "1793346093.115109 vps systemd[71060]:"
        " tracerworks-worker-1958-1793300000.scope:"
        " The kernel OOM killer killed some processes in this unit.\n"
        "1793346094.000001 vps systemd[71060]: actions-runner-vps-1.service:"
        " The kernel OOM killer killed some processes in this unit.\n"
        "1793346094.000002 vps systemd[71060]: actions-runner-vps-1.service:"
        " Failed with result 'oom-kill'.\n"
        "1793346095.000001 vps systemd[71060]: oomprobe-1.scope:"
        " The kernel OOM killer killed some processes in this unit.\n"
        "1793346096.000001 vps systemd[71060]: tracerworks-worker-77-1793300000.scope:"
        " The kernel OOM killer killed some processes in this unit.\n"
        "1793346096.000002 vps systemd[71060]: tracerworks-worker-77-1793300000.scope:"
        " Failed with result 'oom-kill'.\n"
    )
    assert oom.rows(journal, WORKER) == [
        {"at": 1793346093, "unit": "tracerworks-worker-1958-1793300000.scope"},
        {"at": 1793346094, "unit": "actions-runner-vps-1.service"},
        {"at": 1793346096, "unit": "tracerworks-worker-77-1793300000.scope"},
    ]
    asked: list[list[str]] = []

    def run(argv: Any, **_: Any) -> Result:
        asked.append(list(argv))
        return Result(0, journal)

    oom.read(run, 1_000_000, WORKER)
    pattern = asked[0][asked[0].index("-g") + 1]
    assert re.search(pattern, "Failed with result 'oom-kill'")
    assert re.search(
        pattern, "The kernel OOM killer killed some processes in this unit."
    )


def test_the_window_holds_only_its_days(db: sqlite3.Connection, path: Path) -> None:
    merged(db, 1, at(10))
    merged(db, 2, at(3))
    assert measures(path, 7)["series"]["merged"] == [0, 0, 0, 1, 0, 0, 0]
    assert sum(v or 0 for v in measures(path, 30)["series"]["merged"]) == 2


def test_an_unreadable_base_is_said(tmp_path: Path) -> None:
    got = measures(tmp_path / "absent.sqlite3")
    assert got["error"]


# ---- the series #1822 records -------------------------------------------------------


def state(
    db: sqlite3.Connection,
    spec: int,
    when: int,
    cause: str,
    phase: str = "development",
    refused: int = 0,
) -> None:
    store.insert(db, "chantier_state", [{
        "project": REPO, "spec": spec, "at": when, "phase": phase, "cause": cause,
        "refused": refused}])  # fmt: skip


def test_a_worker_s_wait_by_cause_in_minutes_per_day(
    db: sqlite3.Connection, path: Path
) -> None:
    state(db, 1, at(1, 10), "worker: working")
    state(db, 1, at(1, 11), "worker at rest, its CI runs", "light_check")
    state(db, 1, at(1, 12), "worker: question", "human")
    state(db, 1, at(1, 12.5), "worker: permission", "human")
    state(
        db, 1, at(1, 13), "worker: waits for memory, confirmed: refused, then lasting"
    )
    state(db, 1, at(1, 13.25), "pull merged", "done")
    state(db, 2, at(1, 23), "merge queue: waiting", "merge_queue")  # over midnight
    state(db, 2, at(0, 1), "pull merged", "done")
    state(
        db, 3, at(1, 14), "no worker, the memory low", "queued", refused=1
    )  # refused: not real
    s = measures(path)["series"]
    assert s["wait_ci"][-2:] == [120, 60]
    assert s["wait_question"][-2:] == [30, 0]
    assert s["wait_permission"][-2:] == [30, 0]
    assert s["wait_memory"][-2:] == [15, 0]
    assert s["wait_ci"][:-2] == [None] * 5, "before the first state: a gap"


def test_a_last_state_still_waiting_counts_to_now_at_most_a_day(
    db: sqlite3.Connection, path: Path
) -> None:
    state(db, 1, at(0, 19), "human label", "human")  # NOW is 20:00
    state(db, 2, at(4, 10), "human label", "human")  # never closed: cut after 24 h
    s = measures(path)["series"]
    assert s["wait_question"][-1] == 60
    assert s["wait_question"][-5:-2] == [840, 600, 0]


def job(
    db: sqlite3.Connection,
    run_id: int,
    name: str,
    conclusion: str,
    read: int | None = 1,
    tests: list[str] | None = None,
    runner: str | None = "vps-1",
    start: int = 0,
    minutes: float = 10,
    wait_s: int | None = 60,
) -> None:
    job_id = next(_ids)
    store.insert(db, "ci_job", [{
        "project": REPO, "job_id": job_id, "run_id": run_id, "attempt": 1,
        "name": name, "runner_name": runner, "machine": "vps" if runner else "github",
        "conclusion": conclusion, "created_at": start - (wait_s or 0),
        "started_at": start, "completed_at": start + int(minutes * 60),
        "wait_s": wait_s, "url": ""}])  # fmt: skip
    if read is not None:
        store.insert(db, "ci_log", [{"project": REPO, "job_id": job_id,
            "read_at": start, "tests": len(tests or [])}])  # fmt: skip
    failures = [{"project": REPO, "job_id": job_id, "test": t} for t in tests or []]
    store.insert(db, "ci_test_failure", failures)


def red_run(db: sqlite3.Connection, when: int, **failed: object) -> None:
    run(db, when, "failure")
    run_id = db.execute("SELECT max(run_id) FROM ci_run").fetchone()[0]
    job(db, run_id, "checks", "success", start=when)
    job(db, run_id, "tests", "failure", start=when, **failed)  # type: ignore[arg-type]


def test_a_red_by_cause_a_job_naming_a_test_or_the_machine(
    db: sqlite3.Connection, path: Path
) -> None:
    red_run(db, at(1), tests=["tests/a.py::test_x"])
    red_run(db, at(1, 11), tests=[])  # its log read, no test named: the machine
    red_run(db, at(1, 12), read=None)  # its log not read: neither, until it is
    s = measures(path)["series"]
    assert s["red_test"][-2:] == [1, 0]
    assert s["red_machine"][-2:] == [1, 0]


def test_the_runners_wait_and_their_busy_share(
    db: sqlite3.Connection, path: Path
) -> None:
    run(db, at(1))
    run_id = db.execute("SELECT max(run_id) FROM ci_run").fetchone()[0]
    job(
        db, run_id, "a", "success", start=at(1, 6), minutes=360, wait_s=120
    )  # 6 h: a quarter
    job(
        db,
        run_id,
        "b",
        "success",
        start=at(1, 6),
        minutes=60,
        wait_s=600,
        runner="vps-2",
    )
    job(
        db, run_id, "c", "success", start=at(1, 6), minutes=60, wait_s=5, runner=None
    )  # GitHub's
    job(db, run_id, "d", "success", start=at(0, 10), minutes=60, runner="vps-2")
    got = measures(path)
    assert got["series"]["runner_wait"][-2:] == [6, 1], (
        "the median wait on the VPS, in minutes"
    )
    assert got["runners"]["vps-1"][-2:] == [25, 0]
    assert got["runners"]["vps-2"][-2] == pytest.approx(4.17, abs=0.01)
    assert got["runners"]["vps-2"][-1] == 5, "today: 1 h of the 20 h gone"
    assert got["runners"]["vps-1"][0] is None, "before its first job: a gap"
    assert set(got["runners"]) == {"vps-1", "vps-2"}


def test_the_automatic_relaunches_per_day_by_kind(
    db: sqlite3.Connection, path: Path
) -> None:
    store.insert(db, "nudge", [
        {"project": REPO, "spec": 1, "at": at(1), "progress": "p", "outcome": "sent"},
        {"project": REPO, "spec": 1, "at": at(1), "progress": "q",
         "outcome": "needs_human"},
    ])  # fmt: skip
    store.insert(db, "ci_revival", [
        {"project": REPO, "pull": 3, "head": "h", "seen_at": at(1), "acted_at": at(1)},
        {"project": REPO, "pull": 4, "head": "h", "seen_at": at(1), "acted_at": None},
    ])  # fmt: skip
    store.insert(db, "machine_rerun", [
        {"project": REPO, "pull": 3, "head": "h", "at": at(1), "by": "parked"},
        {"project": REPO, "pull": 4, "head": "h", "at": at(1), "by": "test_red"},
    ])  # fmt: skip
    s = measures(path)["series"]
    assert [s["nudge_worker"][-2], s["nudge_ci"][-2], s["nudge_machine"][-2]] == [
        1,
        1,
        1,
    ]


def test_the_oom_kills_and_the_runners_among_them(
    db: sqlite3.Connection, path: Path
) -> None:
    store.insert(db, "oom_kill", [
        {"at": at(1), "unit": "actions-runner-vps-1.service"},
        {"at": at(1, 11), "unit": "tracerworks-serve.service"},
    ])  # fmt: skip
    s = measures(path)["series"]
    assert s["oom"][-2:] == [2, 0]
    assert s["oom_runner"][-2:] == [1, 0]


def test_the_cost_per_pull_and_the_first_green(
    db: sqlite3.Connection, path: Path
) -> None:
    for spec, tokens in ((1, 2_000_000), (2, 4_000_000)):
        store.insert(db, "chantier", [{
            "project": REPO, "spec": spec, "finished_at": at(1), "tokens_total": tokens,
            "tokens_input": 0, "tokens_cache_write": 0, "tokens_cache_read": 0,
            "tokens_output": 0, "messages": 1, "source": "clean"}])  # fmt: skip
    merged(db, 1, at(1, 15))
    merged(db, 2, at(1, 16))
    run(db, at(1, 9), "success", branch="b1")  # green at once
    run(db, at(1, 9), "failure", branch="b2")
    run(db, at(1, 10), "success", branch="b2")
    s = measures(path)["series"]
    assert s["cost_m"][-2:] == [3, None], "tokens per chantier finished, in millions"
    assert s["first_green"][-2:] == [50, None], "% of the day's merged PRs"


def test_a_runner_s_share_at_midnight_divides_by_no_zero(
    path: Path, db: sqlite3.Connection
) -> None:
    run(db, at(1))
    run_id = db.execute("SELECT max(run_id) FROM ci_run").fetchone()[0]
    job(db, run_id, "a", "success", start=at(1, 6), minutes=60)
    got = measures(path, now=TODAY)
    assert got["runners"]["vps-1"][-1] == 0, "the day's first instant: none gone"
    assert got["period"] == {"ci_p50": 20, "ci_p90": 20, "first_green": None}


# ---- the quotas (#2048) ---------------------------------------------------------


def test_the_quotas_are_the_current_period_s_whatever_the_span(
    db: sqlite3.Connection, path: Path
) -> None:
    """The Claude week and the Actions month, as Trends drew them: the
    period in progress and its projection, the same for 7, 30 or 90 days."""
    store.insert(db, "claude_quota_sample", [
        {"period": "seven_day", "seen_at": NOW - 2 * DAY, "used_percent": 20,
         "resets_at": NOW + 3 * DAY},
        {"period": "seven_day", "seen_at": NOW - 3600, "used_percent": 45,
         "resets_at": NOW + 3 * DAY}])  # fmt: skip
    store.insert(db, "actions_minutes_sample", [
        {"read_at": NOW - DAY, "account": "o", "year": 2026, "month": 9,
         "used_minutes": 900, "included_minutes": 3000}])  # fmt: skip
    trends = build_trends(
        path, REPO, NOW, 7, 7, claude_half_life_hours=24, actions_half_life_hours=48
    )
    got = [measures(path, span)["quotas"] for span in SPANS]
    assert got[0] == {"claude": trends["claude"], "actions": trends["actions"]}
    assert got[0]["claude"]["projected"] is not None, "a projection to the reset"
    assert got[1] == got[0] and got[2] == got[0], "the span changes nothing"


def test_a_quota_without_a_reading_is_none(path: Path, db: sqlite3.Connection) -> None:
    assert measures(path)["quotas"] == {"claude": None, "actions": None}


# ---- the green share of the CI runs (#2049) -------------------------------------


def test_the_green_share_rolls_over_3_days_the_days_before_the_span_included(
    db: sqlite3.Connection, path: Path
) -> None:
    run(db, at(8), "failure")  # 2 days before the 7 days' first: counted
    run(db, at(7), "success")  # the day before: counted
    run(db, at(6), "success")  # the span's first day
    run(db, at(6, 11), "cancelled")  # neither green nor red
    run(db, at(9), "failure")  # 3 days before: out of the first day's window
    share = measures(path, 7)["series"]["green_share"]
    assert share[0] == pytest.approx(2 / 3, abs=1e-4), "green / (green + red)"
    assert share[1] == 1.0, "the red of 8 days ago left the window"
    assert share[2] == 1.0
    assert share[3:] == [None, None, None, None], "3 days without green nor red"


def test_the_green_share_ignores_the_cancelled_runs(
    db: sqlite3.Connection, path: Path
) -> None:
    run(db, at(0), "cancelled")
    run(db, at(0, 11), "skipped")
    assert measures(path, 7)["series"]["green_share"][-1] is None


# ---- the machine's hours (#2051) ------------------------------------------------


def test_the_machine_s_hours_are_the_last_week_s_whatever_the_span(
    db: sqlite3.Connection, path: Path
) -> None:
    """Memory, CPU and disk, hourly means over 7 days, as Trends gave them."""
    hour = NOW - NOW % 3600 - 3600
    store.insert(db, "vps_sample", [
        {"at": hour + 60, "source": "live", "memory_available_mb": 4000,
         "memory_total_mb": 15000, "cpu_percent": 10, "disk_used_percent": 70},
        {"at": hour + 120, "source": "live", "memory_available_mb": 6000,
         "memory_total_mb": 16000, "cpu_percent": 30, "disk_used_percent": 72},
        {"at": NOW - 8 * DAY, "source": "health_log",
         "memory_available_mb": 1, "disk_used_percent": 1}])  # fmt: skip
    trends = build_trends(path, REPO, NOW, 7, 7)
    got = [measures(path, span)["hours"] for span in SPANS]
    assert got[0] == {"vps": trends["vps"], "memory_total_mb": 16000}
    assert got[0]["vps"] == [[hour, 5000, 20.0, 71.0]], "a mean per hour, a week back"
    assert got[1] == got[0] and got[2] == got[0], "the span changes nothing"


def test_the_machine_s_hours_without_a_reading_are_empty(
    db: sqlite3.Connection, path: Path
) -> None:
    assert measures(path)["hours"] == {"vps": [], "memory_total_mb": None}


def test_the_merge_queue_s_jobs_wait_for_a_runner_apart(
    db: sqlite3.Connection, path: Path
) -> None:
    """#2111 (spec #2108, story 7): the wait of the queue's jobs, before and
    after its gate — a job of an attempt started while a stay of its pull
    request was in test (30/10: #2065's browser tranche, 70 min)."""
    db.execute(
        "INSERT INTO merge_queue (project, pull, entered_at, state, started_at,"
        " left_at, outcome) VALUES (?, 7, ?, 'out', ?, ?, 'merged')",
        (REPO, at(1, 9), at(1, 9), at(1, 12)),
    )
    run(db, at(1, 10), pr=7)  # in its test: the queue's
    queue_run = db.execute("SELECT max(run_id) FROM ci_run").fetchone()[0]
    job(db, queue_run, "browser tranche 1/2", "success", start=at(1, 11), wait_s=4200)
    job(
        db, queue_run, "integration tranche 1/2", "success", start=at(1, 11), wait_s=600
    )
    run(db, at(1, 13), pr=7)  # after it: its light lane
    light = db.execute("SELECT max(run_id) FROM ci_run").fetchone()[0]
    job(db, light, "integration tranche 1/1", "success", start=at(1, 13), wait_s=60)
    s = measures(path)["series"]
    assert s["queue_runner_wait"][-2:] == [pytest.approx(40.0), None], (
        "the median of the queue's jobs alone, in minutes; none today"
    )


def test_the_browser_s_reading_holds_every_series_the_tab_is_given(
    path: Path, db: sqlite3.Connection
) -> None:
    """The night's browser journeys feed the page a reading taken on the VPS
    (`tests/fixtures/tracerworks/dashboard/measures.json`): a series added to the
    reading since (green_share #2049, queue_runner_wait #2111; the quotas
    #2048 and the machine's hours #2051 at the top) was missing from it, the
    page threw on it and four of its journeys went red on the night of 31/10
    (#2173). Said here, in the factory's job, the day it lags; and every
    series of the fixture has one value per day of it."""
    fixture = Path(__file__).parents[1] / "fixtures/tracerworks/dashboard/measures.json"
    reading = json.loads(fixture.read_text(encoding="utf-8"))
    got = measures(path, 30)
    assert set(got["series"]) - set(reading["series"]) == set()
    assert set(got) - set(reading) == set()
    assert all(len(v) == len(reading["days"]) for v in reading["series"].values())
