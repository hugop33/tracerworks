"""What feeds the history base (#836, #837), on recorded answers.

Every collector is fed what GitHub, `/proc`, `df`, the status line's file and
the health journal really answered (trimmed), and the contre-épreuve of each:
a source that cannot be read writes nothing — never a zero.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

import tracerworks
from tests.tracerworks.fakes import FIXTURES, FakeRunner, recorded_settings
from tracerworks import store
from tracerworks.github import job_log
from tracerworks.pytest_log import failed_tests
from tracerworks.quotas import Fetch
from tracerworks.recorder import (
    Recorder,
    comment_row,
    cpu_percent,
    cpu_times,
    health_rows,
    load_rows,
    record_needs_human,
)
from tracerworks.settings import Settings
from tracerworks.strays import Stray
from tracerworks.usage import ChantierUsage, Part, Tokens

HISTORY = FIXTURES / "history"
REPO = "owner/repo"
NOW = datetime(2026, 9, 30, 18, 0, tzinfo=UTC).timestamp()
RUNS = f"repos/{REPO}/actions/runs"
RETRIED, DEPLOYED, FAILED, SKIPPED = 36681539096, 36723716152, 36746593024, 36749922844
BRANCH_776 = "agent/776-retirer-l-export-en-double-du"
BRANCH_826 = "agent/826-tracerworks-message-envoie-tout-de-suite-le"
SPEC_830 = {
    **json.loads((HISTORY / "issue-830.json").read_text(encoding="utf-8")),
    "body": "…",
}
DF = (
    "Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
    "/dev/sda1        100503408 74532212  25954812      75% /\n"
)


def text(name: str) -> str:
    return (HISTORY / name).read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return replace(
        recorded_settings(tmp_path),
        history_db=tmp_path / "history" / "factory.sqlite3",
        health_log=HISTORY / "vps-health.txt",
        claude_quota_file=tmp_path / "claude-quota.json",
        billing_token_file=tmp_path / "billing.token",
        worker_token_file=_worker_token(tmp_path),
    )


def _worker_token(tmp_path: Path) -> Path:
    """The workers' token in place: its alert (#1529) is tested in test_alerts."""
    path = tmp_path / "worker-github.token"
    path.write_text("github_pat_fixture\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def rows(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    with closing(store.connect(settings.history_db)) as db:
        return db.execute(sql).fetchall()


def github(fake: FakeRunner, *, runs: str | None = None, jobs: bool = True) -> None:
    """The minute's GitHub: the runs listing, each attempt's jobs, the merged
    pull requests, the issues waiting for the operator."""
    fake.answer("gh", "api", f"{RUNS}?per_page=100", stdout=runs or text("runs.json"))
    if jobs:
        for path in HISTORY.glob("jobs-*.json"):
            _, run_id, attempt = path.stem.split("-")
            fake.answer(
                "gh",
                "api",
                f"{RUNS}/{run_id}/attempts/{attempt}/jobs?per_page=100&page=1",
                stdout=path.read_text(),
            )
    fake.answer(
        "gh",
        "api",
        f"{RUNS}/{RETRIED}/attempts/1",
        stdout=text(f"attempt-{RETRIED}-1.json"),
    )
    fake.answer("gh", "pr", "list", prefix=True, stdout=text("merged.json"))
    fake.answer("gh", "issue", "list", prefix=True, stdout="[]")
    # the Qualité tab's (#840): failed jobs' logs, failing branches' files, specs
    for log in HISTORY.glob("log-*.txt"):
        fake.answer(
            "gh", "api", "--allow-escape-sequences",
            f"repos/{REPO}/actions/jobs/{log.stem.split('-')[1]}/logs",
            stdout=log.read_text(encoding="utf-8"),
        )  # fmt: skip
    for spec, branch in ((776, BRANCH_776), (826, BRANCH_826)):
        fake.answer(
            "gh", "pr", "list", "-R", REPO, "--head", branch, "--state", "all",
            "--limit", "1", "--json", "number,files",
            stdout=text(f"pr-files-{spec}.json"),
        )  # fmt: skip
    fake.answer("gh", "api", f"repos/{REPO}/issues/830", stdout=json.dumps(SPEC_830))


def world(
    settings: Settings,
    stat: Path = HISTORY / "stat-after.txt",
    billing: Fetch = lambda url, token: (500, b""),
    spawn: Callable[[Callable[[], None]], None] = lambda job: None,
    sender: Callable[[], Callable[[str], None]] | None = None,
    resting: Callable[[], set[int] | None] | None = None,
    asking: Callable[[], dict[int, str] | None] | None = None,
    strays: Callable[[], list[Stray] | None] | None = None,
    **kwargs: Any,
) -> tuple[FakeRunner, Recorder, io.StringIO]:
    fake = FakeRunner()
    fake.answer("df", "-Pk", str(settings.worktree_root), stdout=DF)
    github(fake, **kwargs)
    err = io.StringIO()
    recorder = Recorder(
        settings,
        fake,
        lambda: NOW,
        stat=stat,
        billing=billing,
        err=err,
        spawn=spawn,
        sender=sender,
        resting=resting,
        asking=asking,
        strays=strays,
    )
    return fake, recorder, err


# --- the machine ----------------------------------------------------------------------


def test_cpu_is_the_share_of_busy_time_between_two_readings() -> None:
    before = cpu_times(HISTORY / "stat-before.txt")
    after = cpu_times(HISTORY / "stat-after.txt")
    # busy +1000 (user 600, system 400), idle+iowait +1000 → 50 %
    assert cpu_percent(before, after) == 50.0
    assert cpu_percent(None, after) is None  # one reading is no measure
    assert cpu_percent(after, before) is None  # counters went back: a reboot


def test_the_minute_records_memory_disk_and_the_cpu_from_the_second_turn(
    settings: Settings,
) -> None:
    stat = settings.history_db.parent.parent / "stat"
    stat.write_text(text("stat-before.txt"))
    _, recorder, _ = world(settings, stat=stat)
    recorder.sample(NOW)
    first = rows(settings, "SELECT * FROM vps_sample")
    assert first == [(int(NOW), "live", 2252, 11435, None, 74.2, 25954812, 100503408)]
    stat.write_text(text("stat-after.txt"))
    recorder.sample(NOW + 60)
    later = f"SELECT cpu_percent FROM vps_sample WHERE at > {int(NOW)}"
    assert rows(settings, later) == [(50.0,)]


def test_an_unreadable_machine_writes_nothing(settings: Settings) -> None:
    blind = replace(settings, meminfo=Path("/nonexistent/meminfo"))
    fake, recorder, err = world(blind, stat=Path("/nonexistent/stat"))
    fake.answer("df", "-Pk", str(settings.worktree_root), returncode=1)
    report = recorder.sample(NOW)
    assert report["vps"] == "memory, CPU and disk unreadable"
    assert rows(settings, "SELECT count(*) FROM vps_sample") == [(0,)]
    assert "vps: memory, CPU and disk unreadable" in err.getvalue()


def test_the_health_journal_is_read_in_local_time_since_the_first_day() -> None:
    read = health_rows(text("vps-health.txt"), date(2026, 9, 1))
    stamp = int(datetime(2026, 9, 23, 18, 0, 25).timestamp())
    assert read[0] == {
        "at": stamp,
        "source": "health_log",
        "memory_available_mb": 4804,
        "disk_used_percent": 80.0,
    }
    # 7 readings: the one before `since`, the one without figures and the
    # garbage line are left out — none of them becomes a zero
    assert len(read) == 7
    assert all(r["memory_available_mb"] != 1 for r in read)


def test_a_load_reading_is_read_each_figure_and_a_question_mark_is_null() -> None:
    """#2279: the 5-minute line; `?` is NULL, never 0; the 15-minute line
    and a garbled one are not load readings."""
    read = load_rows(text("vps-load.txt"), date(2026, 11, 1))
    assert [r["at"] for r in read] == [
        int(datetime(2026, 11, 3, 9, minute, second).timestamp())
        for minute, second in ((0, 2), (5, 1), (10, 1))
    ]
    assert read[1] == {
        "at": int(datetime(2026, 11, 3, 9, 5, 1).timestamp()),
        "cpu_percent": 91.4, "iowait_percent": 0.5,
        "load1": 12.54, "load5": 12.68, "load15": 12.36, "cores": 6,
        "memory_available_mb": 4195,
        "cpu_workers": 24.7, "cpu_ci": 40.6, "cpu_factory": 11.4,
        "cpu_containers": 8.6, "cpu_rest": 6.1,
    }  # fmt: skip
    assert read[0]["cpu_percent"] is None and read[0]["cpu_workers"] is None
    assert read[0]["load1"] == 16.4
    assert read[2]["cpu_ci"] is None and read[2]["cpu_factory"] == 1.0
    assert load_rows(text("vps-load.txt"), date(2026, 11, 4)) == []


def test_a_load_line_being_written_is_left_for_the_next_read() -> None:
    """Read without the log's lock: a line cut short (`cpu_rest=6` of
    `6.1%`) would be written once and never read whole again."""
    whole = text("vps-load.txt").splitlines()[2]
    assert load_rows(whole[:-3], None) == []
    assert load_rows(whole + "\n", None)[0]["cpu_rest"] == 6.1


def test_the_health_journal_still_reads_an_old_line_and_a_load_line_s_memory() -> None:
    read = health_rows(text("vps-load.txt"), date(2026, 11, 1))
    assert [(r["memory_available_mb"], r["disk_used_percent"]) for r in read] == [
        (3508, 80.0), (3500, None), (4195, None), (5000, None),
    ]  # fmt: skip


def test_the_load_is_recorded_every_five_minutes_once_each(
    settings: Settings,
) -> None:
    settings = replace(settings, health_log=HISTORY / "vps-load.txt")
    _, recorder, _ = world(settings)
    assert recorder.sample(NOW)["vps_load"] == 3
    assert "vps_load" not in recorder.sample(NOW + 60)  # not due before 5 min
    assert recorder.sample(NOW + 300)["vps_load"] == 0  # already there
    assert rows(settings, "SELECT count(*), max(cpu_ci) FROM vps_load") == [(3, 40.6)]


def test_an_unreadable_health_log_writes_no_load(settings: Settings) -> None:
    settings = replace(settings, health_log=settings.history_db.parent / "absent")
    _, recorder, err = world(settings)
    assert str(recorder.sample(NOW)["vps_load"]).startswith("health log unreadable")
    assert rows(settings, "SELECT count(*) FROM vps_load") == [(0,)]
    assert "vps_load: health log unreadable" in err.getvalue()


# --- the quotas -----------------------------------------------------------------------


def test_a_claude_reading_is_recorded_once_by_when_it_was_seen(
    settings: Settings,
) -> None:
    seen = {
        "five_hour": {
            "used_percent": 41.0,
            "resets_at": NOW + 3600,
            "seen_at": NOW - 30,
        },
        "seven_day": {
            "used_percent": 67.5,
            "resets_at": NOW + 86400,
            "seen_at": NOW - 30,
        },
    }
    settings.claude_quota_file.write_text(json.dumps(seen))
    _, recorder, _ = world(settings)
    assert recorder.sample(NOW)["claude_quota"] == 2
    assert recorder.sample(NOW + 60)["claude_quota"] == 0  # the same reading
    assert rows(
        settings, "SELECT period, used_percent FROM claude_quota_sample ORDER BY 1"
    ) == [
        ("five_hour", 41.0),
        ("seven_day", 67.5),
    ]


def test_no_status_line_yet_writes_no_quota(settings: Settings) -> None:
    _, recorder, _ = world(settings)
    assert recorder.sample(NOW)["claude_quota"] == "no Claude status line received"
    assert rows(settings, "SELECT count(*) FROM claude_quota_sample") == [(0,)]


def test_actions_minutes_are_recorded_every_quarter_hour(settings: Settings) -> None:
    settings.billing_token_file.write_text("token")
    settings.billing_token_file.chmod(0o600)
    body = json.dumps(
        {"usageItems": [{"unitType": "Minutes", "grossQuantity": 1234.5}]}
    ).encode()
    _, recorder, _ = world(settings, billing=lambda url, token: (200, body))
    assert recorder.sample(NOW)["actions"] == 1
    assert "actions" not in recorder.sample(NOW + 60)  # not due before 15 min
    assert rows(
        settings,
        "SELECT account, year, month, used_minutes, included_minutes"
        " FROM actions_minutes_sample",
    ) == [("owner", 2026, 9, 1234.5, 2000)]


def test_past_ninety_percent_ci_github_goes_to_the_vps_and_it_is_said(
    settings: Settings,
) -> None:
    # #1296: the net is the repository variable the CI's `changes` reads.
    settings.billing_token_file.write_text("token")
    settings.billing_token_file.chmod(0o600)
    body = json.dumps(
        {"usageItems": [{"unitType": "Minutes", "grossQuantity": 1850.0}]}
    ).encode()  # of 2000: 92.5 %
    fake, recorder, err = world(settings, billing=lambda url, token: (200, body))
    variables = ("gh", "variable", "list", "--repo", REPO, "--json", "name")
    fake.answer(*variables, stdout='[{"name": "RUNS_ON"}]')
    fake.answer("gh", "variable", "set", prefix=True)
    assert recorder.sample(NOW)["ci_budget"] == 1
    [set_] = fake.ran("gh", "variable", "set")
    assert set_.argv[3:] == ("CI_GITHUB_BUDGET_LOW", "--repo", REPO, "--body", "true")
    assert "ci_budget" in err.getvalue() and "92%" in err.getvalue()


def test_the_settings_tab_s_minutes_move_the_net_at_the_next_reading(
    settings: Settings,
) -> None:
    """#2192: 1850 minutes are past the net of 2000 (the project file's);
    raised to 4000 in the state file, the next reading — no restart — weighs
    them again, records the new plan and removes the variable."""
    settings.billing_token_file.write_text("token")
    settings.billing_token_file.chmod(0o600)
    body = json.dumps(
        {"usageItems": [{"unitType": "Minutes", "grossQuantity": 1850.0}]}
    ).encode()
    fake, recorder, err = world(settings, billing=lambda url, token: (200, body))
    variables = ("gh", "variable", "list", "--repo", REPO, "--json", "name")
    fake.answer(*variables, stdout="[]")
    fake.answer("gh", "variable", "set", prefix=True)
    fake.answer("gh", "variable", "delete", prefix=True)
    assert recorder.sample(NOW)["ci_budget"] == 1

    settings.state_file.write_text("github:\n  actions_included_minutes: 4000\n")
    fake.answer(*variables, stdout='[{"name": "CI_GITHUB_BUDGET_LOW"}]')
    assert recorder.sample(NOW + 15 * 60)["ci_budget"] == 1
    [delete] = fake.ran("gh", "variable", "delete")
    assert delete.argv[3] == "CI_GITHUB_BUDGET_LOW"
    assert "46%" in err.getvalue()
    assert rows(
        settings,
        "SELECT included_minutes FROM actions_minutes_sample ORDER BY read_at",
    ) == [(2000,), (4000,)]


def test_unreadable_actions_minutes_write_nothing(settings: Settings) -> None:
    _, recorder, _ = world(settings)
    report = recorder.sample(NOW)
    assert "token" in str(report["actions"])
    assert report["ci_budget"] == 0  # and no variable touched: none was answered
    assert rows(settings, "SELECT count(*) FROM actions_minutes_sample") == [(0,)]


# --- CI, deployments, pull requests ---------------------------------------------------


def test_each_finished_attempt_is_recorded_with_its_jobs_and_machines(
    settings: Settings,
) -> None:
    _, recorder, _ = world(settings)
    assert recorder.sample(NOW)["ci"] == 5  # 4 runs, one of them run twice
    assert rows(
        settings,
        "SELECT run_id, attempt, workflow, conclusion FROM ci_run ORDER BY 1, 2",
    ) == [
        (RETRIED, 1, "CI", "failure"),
        (RETRIED, 2, "CI", "success"),
        (DEPLOYED, 1, "Deploy", "success"),
        (FAILED, 1, "CI", "failure"),
        (SKIPPED, 1, "Deploy", "skipped"),
    ]
    # the flaky test's story: the browser tranche fell, then passed, same commit
    assert rows(
        settings,
        f"SELECT j.attempt, j.conclusion, r.head_sha = (SELECT head_sha FROM ci_run"
        f" WHERE run_id = {RETRIED} AND attempt = 2) FROM ci_job j JOIN ci_run r"
        f" USING (project, run_id, attempt) WHERE j.run_id = {RETRIED}"
        " AND j.name = 'browser tranche 1/1' ORDER BY 1",
    ) == [(1, "failure", 1), (2, "success", 1)]
    machines = rows(
        settings,
        f"SELECT machine, count(*), sum(billed_minutes > 0) FROM ci_job"
        f" WHERE run_id = {FAILED} GROUP BY machine ORDER BY 1",
    )
    assert [m for m, _, _ in machines] == [None, "github", "vps"] or [
        m for m, _, _ in machines
    ] == ["github", "vps"]
    assert all(billed == 0 for m, _, billed in machines if m == "vps")
    skipped = rows(
        settings,
        f"SELECT machine, wait_s, billed_minutes FROM ci_job WHERE run_id = {SKIPPED}",
    )
    assert skipped == [(None, None, 0)]  # no runner took it: no machine, no wait


def test_a_job_on_github_bills_its_minutes_rounded_up(settings: Settings) -> None:
    _, recorder, _ = world(settings)
    recorder.sample(NOW)
    for duration, billed in rows(
        settings,
        "SELECT duration_s, billed_minutes FROM ci_job WHERE machine = 'github'",
    ):
        assert billed == -(-duration // 60)
        assert billed >= 1


def test_the_deployment_is_the_successful_run_of_the_deploy_workflow(
    settings: Settings,
) -> None:
    _, recorder, _ = world(settings)
    recorder.sample(NOW)
    assert rows(settings, "SELECT run_id FROM deployment") == [(DEPLOYED,)]


def test_a_run_whose_jobs_cannot_be_read_waits_for_the_next_minute(
    settings: Settings,
) -> None:
    fake, recorder, _ = world(settings)
    fake.answer(
        "gh",
        "api",
        f"{RUNS}/{DEPLOYED}/attempts/1/jobs?per_page=100&page=1",
        returncode=1,
    )
    assert recorder.sample(NOW)["ci"] == 4
    assert rows(settings, f"SELECT count(*) FROM ci_run WHERE run_id = {DEPLOYED}") == [
        (0,)
    ]
    assert rows(settings, "SELECT count(*) FROM deployment") == [(0,)]
    github(fake)  # readable again
    assert recorder.sample(NOW + 60)["ci"] == 1
    assert rows(settings, "SELECT run_id FROM deployment") == [(DEPLOYED,)]


def test_a_run_still_going_is_not_recorded(settings: Settings) -> None:
    listing = json.loads(text("runs.json"))
    for run in listing["workflow_runs"]:
        run["status"], run["conclusion"] = "in_progress", None
    _, recorder, _ = world(settings, runs=json.dumps(listing))
    assert recorder.sample(NOW)["ci"] == 0


def test_a_finished_run_is_asked_for_its_jobs_once(settings: Settings) -> None:
    fake, recorder, _ = world(settings)
    recorder.sample(NOW)
    before = len(fake.ran("gh", "api"))
    recorder.sample(NOW + 60)
    asked = [c.argv for c in fake.ran("gh", "api")[before:]]
    assert asked == [("gh", "api", f"{RUNS}?per_page=100")]


def test_an_unreadable_runs_listing_writes_nothing(settings: Settings) -> None:
    fake, recorder, _ = world(settings)
    fake.answer("gh", "api", f"{RUNS}?per_page=100", returncode=1)
    assert recorder.sample(NOW)["ci"] == "CI runs unreadable"
    assert rows(settings, "SELECT count(*) FROM ci_run") == [(0,)]


def test_merged_pull_requests_carry_their_chantier(settings: Settings) -> None:
    _, recorder, _ = world(settings)
    assert recorder.sample(NOW)["pull_merged"] == 3
    assert rows(
        settings, "SELECT number, spec, branch FROM pull_merged ORDER BY 1"
    ) == [
        (832, 830, "agent/830-worker-states"),
        (833, None, "ci/all-but-integration-on-github"),
        (834, None, "tracerworks/claude-statusline-trial"),
    ]


def test_a_merged_pr_keeps_its_size_and_a_row_without_one_gets_it(
    settings: Settings,
) -> None:
    """#860: files, lines added and removed, issues it closed — what the
    Delivered panel shows. A row recorded before (none) is filled in; a PR
    GitHub says nothing about stays unknown (NULL), never zero."""
    with closing(store.connect(settings.history_db)) as db:
        store.insert(db, "pull_merged", [{"project": REPO, "number": 832,
            "title": "old", "branch": "agent/830-worker-states", "spec": 830,
            "created_at": None, "merged_at": 1, "url": "u"}])  # fmt: skip
    _, recorder, _ = world(settings)
    recorder.sample(NOW)
    assert rows(
        settings,
        "SELECT number, changed_files, additions, deletions, closed_issues"
        " FROM pull_merged ORDER BY 1",
    ) == [
        (832, 9, 412, 37, 2),
        (833, None, None, None, None),
        (834, 1, 5, 0, 0),
    ]


# --- the operator ---------------------------------------------------------------------


def test_a_needs_human_episode_opens_and_closes_to_the_minute(tmp_path: Path) -> None:
    with closing(store.connect(tmp_path / "f.sqlite3")) as db:
        assert record_needs_human(db, REPO, {840}, 1000) == (1, 0)
        assert record_needs_human(db, REPO, {840}, 1060) == (0, 0)  # still asking
        assert record_needs_human(db, REPO, set(), 2800) == (0, 1)
        assert record_needs_human(db, REPO, {840}, 5000) == (1, 0)  # asks again
        assert db.execute(
            "SELECT issue, started_at, ended_at FROM needs_human ORDER BY 2"
        ).fetchall() == [(840, 1000, 2800), (840, 5000, None)]


def test_unreadable_issues_close_no_episode(settings: Settings) -> None:
    fake, recorder, _ = world(settings)
    with closing(store.connect(settings.history_db)) as db:
        record_needs_human(db, REPO, {840}, NOW - 600)
    fake.answer("gh", "issue", "list", prefix=True, returncode=1)
    assert recorder.sample(NOW)["needs_human"] == (
        "issues waiting for the operator unreadable"
    )
    assert rows(settings, "SELECT ended_at FROM needs_human") == [(None,)]


# --- chantiers ------------------------------------------------------------------------


def test_the_usage_comment_of_a_spec_is_read_back() -> None:
    usage, other = json.loads(text("comments-806.json"))
    posted = int(datetime(2026, 9, 30, 15, 33, 31, tzinfo=UTC).timestamp())
    assert comment_row(REPO, 806, usage["body"], posted) == {
        "project": REPO,
        "spec": 806,
        "finished_at": posted,
        "duration_s": (1 * 60 + 51) * 60,
        "tokens_total": 40043719,
        "tokens_input": 376,
        "tokens_cache_write": 506532,
        "tokens_cache_read": 39356958,
        "tokens_output": 179853,
        "messages": 186,
        "source": "comment",
        # a comment of before #2253: no level line, nothing made up
        "model": None,
        "effort": None,
    }
    assert comment_row(REPO, 806, other["body"], posted) is None
    torn = usage["body"].replace(" | 186 |", " | |")
    assert comment_row(REPO, 806, torn, posted) is None  # never a zero made up


def test_clean_records_the_chantier_it_measured(settings: Settings) -> None:
    from tracerworks.recorder import record_chantier

    first = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    last = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)
    usage = ChantierUsage(
        owner=[Part("s", Tokens(10, 20, 300, 4, 2), first, last)],
        subagents=[Part("review", Tokens(1, 2, 30, 1, 1), first, first)],
    )
    assert record_chantier(settings, 836, usage, NOW)
    assert not record_chantier(settings, 836, usage, NOW + 60)  # once
    assert rows(
        settings,
        "SELECT spec, duration_s, tokens_total, messages, source FROM chantier",
    ) == [(836, 5400, 368, 3, "clean")]


def test_a_chantier_without_any_message_is_not_recorded(settings: Settings) -> None:
    from tracerworks.recorder import record_chantier

    assert not record_chantier(settings, 836, ChantierUsage(), NOW)


# --- the one-time import --------------------------------------------------------------


def backfill_world(settings: Settings) -> tuple[FakeRunner, Recorder]:
    fake, recorder, _ = world(settings)
    fake.answer("gh", "api", f"{RUNS}?per_page=100&page=1", stdout=text("runs.json"))
    fake.answer(
        "gh", "api", "-X", "GET", "search/issues", prefix=True,
        stdout=json.dumps(
            {"total_count": 1, "incomplete_results": False, "items": [{"number": 806}]}
        ),
    )  # fmt: skip
    fake.answer(
        "gh", "api", f"repos/{REPO}/issues/806/comments?per_page=100",
        stdout=text("comments-806.json"),
    )  # fmt: skip
    return fake, recorder


def test_the_backfill_imports_each_part_once(settings: Settings) -> None:
    fake, recorder = backfill_world(settings)
    assert recorder.backfill() == {
        "ci": 5,
        "pull_merged": 3,
        "chantier": 1,
        "health_log": 7,
    }
    assert rows(settings, "SELECT name, rows FROM backfill ORDER BY 1") == [
        ("chantier", 1), ("ci", 5), ("health_log", 7), ("pull_merged", 3),
    ]  # fmt: skip
    asked = len(fake.calls)
    assert recorder.backfill() == {}
    assert len(fake.calls) == asked


def test_the_backfill_stops_at_the_first_day(settings: Settings) -> None:
    late = replace(settings, history_since=date(2026, 9, 29))
    _, recorder = backfill_world(late)
    report = recorder.backfill()
    created = sorted(
        r["created_at"] for r in json.loads(text("runs.json"))["workflow_runs"]
    )
    assert all(c >= "2026-09-29" for c in created[-int(report["ci"]) :] or [""])
    assert report["health_log"] == 4  # the lines of the 30th only


def test_a_failed_part_is_not_marked_done_and_is_tried_again(
    settings: Settings,
) -> None:
    fake, recorder = backfill_world(settings)
    fake.answer("gh", "api", f"{RUNS}?per_page=100&page=1", returncode=1)
    report = recorder.backfill()
    assert report["ci"] == "page 1 of the runs unreadable"
    assert ("ci",) not in rows(settings, "SELECT name FROM backfill")
    fake.answer("gh", "api", f"{RUNS}?per_page=100&page=1", stdout=text("runs.json"))
    assert recorder.backfill() == {"ci": 5}


def test_the_backfill_starts_by_itself_at_the_first_tick_and_not_twice(
    settings: Settings,
) -> None:
    started: list[object] = []
    _, recorder, _ = world(settings, spawn=started.append)
    recorder.tick()
    recorder.tick()
    assert len(started) == 1


def test_serve_records_without_any_visitor(settings: Settings) -> None:
    """The cycle writes on its own: ticks, no request."""
    _, recorder, _ = world(settings)
    recorder.tick()
    assert rows(settings, "SELECT count(*) FROM vps_sample") == [(1,)]
    assert rows(settings, "SELECT count(*) FROM ci_run") == [(5,)]


def test_a_source_that_breaks_leaves_the_others_standing(settings: Settings) -> None:
    fake, recorder, err = world(settings)

    def boom(*_: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    fake.on("gh", "pr", "list", prefix=True)(boom)  # type: ignore[arg-type]
    report = recorder.sample(NOW)
    assert "disk I/O error" in str(report["pull_merged"])
    assert report["ci"] == 5
    recorder.sample(NOW + 60)
    assert err.getvalue().count("pull_merged") == 1  # said once, not every minute


def test_a_base_that_cannot_open_never_stops_the_cycle(
    settings: Settings, tmp_path: Path
) -> None:
    (tmp_path / "blocked").write_text("a file where the folder should be")
    broken = replace(settings, history_db=tmp_path / "blocked" / "f.sqlite3")
    _, recorder, err = world(broken)
    recorder.tick()  # does not raise
    assert "history: base: error" in err.getvalue()
    _, recorder, _ = world(settings)
    recorder.tick()
    assert rows(settings, "SELECT count(*) FROM vps_sample") == [(1,)]


# --- what the Qualité tab needs (#840) ----------------------------------------------


def test_failed_tests_are_read_from_pytest_s_summary() -> None:
    log = text("log-109994342193.txt")
    assert failed_tests(log) == {
        "tests/integration/test_browser_widget_journey.py"
        "::test_the_file_moves_without_a_reload"
    }
    # the progress line « …::test FAILED [ 35%] » is not the summary: once
    assert failed_tests("2026-09-30T16:50:21Z tests/a.py::t FAILED [ 35%]") == set()
    assert failed_tests("2026-09-30T16:08:18Z ERROR tests/b.py - ImportError") == {
        "tests/b.py"
    }
    # a job failed for another reason names no test
    assert failed_tests(text("log-109996511504.txt")) == set()


@pytest.mark.parametrize(
    ("line", "named"),
    [
        # #2223: a parameter with spaces — the factory read « no test » on
        # 29 and 30/10 and the red passed for the machine's.
        (
            "2026-10-30T20:39:12Z FAILED tests/integration/test_outbox_api.py"
            "::test_send_and_close_rolls_back[outbox-outbox_emails-insert-"
            "new.org_id = '{org_id}' and new.ticket_id = '{ticket_id}']"
            " - httpx.ReadError: [Errno 104] Connection reset by peer",
            "tests/integration/test_outbox_api.py::test_send_and_close_rolls_back"
            "[outbox-outbox_emails-insert-new.org_id = '{org_id}'"
            " and new.ticket_id = '{ticket_id}']",
        ),
        (
            "FAILED tests/a.py::t[x - y] - AssertionError: [1] != [2]",
            "tests/a.py::t[x - y]",
        ),
        ("FAILED tests/a.py::t[1280-folded]", "tests/a.py::t[1280-folded]"),
        ("FAILED tests/a.py::t[a b] - boom", "tests/a.py::t[a b]"),
    ],
)
def test_a_parameter_with_spaces_is_named_whole(line: str, named: str) -> None:
    assert failed_tests(line) == {named}


def test_each_failed_job_s_log_is_read_once(settings: Settings) -> None:
    fake, recorder, _ = world(settings)
    report = recorder.sample(NOW)
    assert report["tests"] == 4  # the four failed jobs of the recorded runs
    assert rows(settings, "SELECT job_id, test FROM ci_test_failure ORDER BY 1") == [
        (
            109777944301,
            "tests/integration/test_browser_settings.py"
            "::test_an_agent_sees_no_doomed_button_on_a_waiting_ticket_under_the_rule",
        ),
        (
            109994342193,
            "tests/integration/test_browser_widget_journey.py"
            "::test_the_file_moves_without_a_reload",
        ),
    ]
    assert rows(settings, "SELECT count(*), sum(tests) FROM ci_log") == [(4, 2)]
    before = len(fake.calls)
    assert recorder.sample(NOW + 60)["tests"] == 0
    assert not [c for c in fake.calls[before:] if "--allow-escape-sequences" in c.argv]


def test_a_log_github_no_longer_has_is_unknown_not_zero(settings: Settings) -> None:
    fake, recorder, _ = world(settings)
    for log in HISTORY.glob("log-*.txt"):
        fake.answer(
            "gh", "api", "--allow-escape-sequences",
            f"repos/{REPO}/actions/jobs/{log.stem.split('-')[1]}/logs",
            returncode=1, stdout="gh: Not Found (HTTP 404)",
        )  # fmt: skip
    assert recorder.sample(NOW)["tests"] == 4
    assert rows(settings, "SELECT count(*), count(tests) FROM ci_log") == [(4, 0)]


def test_a_log_that_cannot_be_read_now_is_tried_again(settings: Settings) -> None:
    fake, recorder, _ = world(settings)
    fake.answer("gh", "api", "--allow-escape-sequences", prefix=True, returncode=1,
                stdout="connection reset")  # fmt: skip
    assert recorder.sample(NOW)["tests"] == 0
    assert rows(settings, "SELECT count(*) FROM ci_log") == [(0,)]


def test_the_files_of_each_failing_branch_s_pull_request(settings: Settings) -> None:
    _, recorder, _ = world(settings)
    recorder.sample(NOW)  # the failures, then (same turn) their branches
    assert rows(settings, "SELECT branch, pr, files FROM branch_read ORDER BY 1") == [
        (BRANCH_776, 778, 7),
        (BRANCH_826, 831, 12),
    ]
    assert ("tests/tracerworks/test_message.py",) in rows(
        settings, f"SELECT path FROM branch_file WHERE branch = '{BRANCH_826}'"
    )


def test_the_spec_of_a_merged_chantier_is_dated(settings: Settings) -> None:
    _, recorder, _ = world(settings)
    assert recorder.sample(NOW)["specs"] == 1
    assert rows(settings, "SELECT number, created_at FROM spec") == [
        (830, int(datetime(2026, 9, 30, 16, 32, 35, tzinfo=UTC).timestamp()))
    ]


# --- the alerts (#841) --------------------------------------------------------------


def test_without_a_token_the_alerts_are_said_off_once(settings: Settings) -> None:
    _, recorder, err = world(settings)
    recorder.sample(NOW)
    recorder.sample(NOW + 60)
    said = err.getvalue()
    assert said.count("Telegram alerts off: no Telegram token") == 1


def test_the_minute_s_readings_raise_the_alert(settings: Settings) -> None:
    """The recorded df says 74 %: under 85, no alert; a full disk, one."""
    sent: list[str] = []
    fake, recorder, _ = world(settings, sender=lambda: sent.append)
    assert recorder.sample(NOW)["alerts"] == 0  # 74 %, no CI of main recorded
    full = DF.replace("74532212  25954812      75%", "99000000   1503408      99%")
    fake.answer("df", "-Pk", str(settings.worktree_root), stdout=full)
    assert recorder.sample(NOW + 60)["alerts"] == 1
    assert sent == ["🔴 VPS disk at 98% (threshold 90%)"]  # used / (used + free)
    assert recorder.sample(NOW + 120)["alerts"] == 0  # still full: once


def test_the_settings_tab_s_switch_is_read_each_minute(settings: Settings) -> None:
    """#2193: the disk family switched off in the state file — no restart —
    the full disk opens its episode, silenced, and nothing is sent."""
    sent: list[str] = []
    fake, recorder, _ = world(settings, sender=lambda: sent.append)
    recorder.sample(NOW)
    settings.state_file.write_text("alerts:\n  disk: false\n")
    full = DF.replace("74532212  25954812      75%", "99000000   1503408      99%")
    fake.answer("df", "-Pk", str(settings.worktree_root), stdout=full)
    assert recorder.sample(NOW + 60)["alerts"] == 0
    assert sent == []
    assert rows(settings, "SELECT key, silenced FROM alert") == [("disk", 1)]


def test_a_state_file_it_cannot_trust_silences_nothing(settings: Settings) -> None:
    """A hand-written value out of bounds: said in the journal, once, and
    every alert still sent — the workers' token's included."""
    sent: list[str] = []
    fake, recorder, err = world(settings, sender=lambda: sent.append)
    settings.state_file.write_text("max_workers: 9\nalerts:\n  disk: false\n")
    full = DF.replace("74532212  25954812      75%", "99000000   1503408      99%")
    fake.answer("df", "-Pk", str(settings.worktree_root), stdout=full)
    assert recorder.sample(NOW)["alerts"] == 1
    assert sent == ["🔴 VPS disk at 98% (threshold 90%)"]
    recorder.sample(NOW + 60)
    assert err.getvalue().count("max_workers must be a whole number") == 1


def test_only_a_worker_s_needs_human_alerts(settings: Settings) -> None:
    """#853: #841 (ticket of #830) has its chantier's worker at rest, #667 has
    none — its label stayed by oversight. Both shown, only #841 told."""
    sent: list[str] = []
    live: set[int] | None = {830}
    fake, recorder, _ = world(
        settings, sender=lambda: sent.append, resting=lambda: live
    )
    waiting = [{"number": 841, "body": "## Parent\n\nSpec #830."},
               {"number": 667, "body": "## Parent\n\nSpec #600."}]  # fmt: skip
    fake.answer("gh", "issue", "list", prefix=True, stdout=json.dumps(waiting))
    recorder.sample(NOW - 31 * 60)
    recorder.sample(NOW)
    assert rows(settings, "SELECT issue FROM needs_human ORDER BY 1") == [
        (667,),
        (841,),
    ]
    assert [m.split(" — ")[0] for m in sent] == [
        "🔴 #841 waiting for Sam (needs-human) for 31 min"
    ]
    live = None  # the workers unreadable: nothing told either way
    recorder.sample(NOW + 60)
    assert len(sent) == 1
    live = set()  # its worker stopped: over
    recorder.sample(NOW + 120)
    assert sent[1].startswith("✅ resolved: #841")


def test_a_worker_on_a_permission_prompt_is_told_once_after_5_min(
    settings: Settings,
) -> None:
    """#984: counted from the first minute the prompt was seen; unreadable
    windows change nothing; the prompt gone, « resolved »."""
    sent: list[str] = []
    live: dict[int, str] | None = {976: "permission: Bash command: rm node_modules"}
    _, recorder, _ = world(settings, sender=lambda: sent.append, asking=lambda: live)
    recorder.sample(NOW)
    recorder.sample(NOW + 4 * 60)
    assert sent == []
    recorder.sample(NOW + 5 * 60)
    assert [m.rsplit(" — ", 1)[0] for m in sent] == [
        "🔴 #976 blocked for 5 min — permission: Bash command: rm node_modules"
    ]
    live = None
    recorder.sample(NOW + 6 * 60)
    assert len(sent) == 1
    live = {}
    recorder.sample(NOW + 7 * 60)
    assert sent[1].startswith("✅ resolved: #976")


def test_unreadable_windows_are_said_once_and_their_return_too(
    settings: Settings,
) -> None:
    def down() -> dict[int, str]:
        raise OSError("tmux gone")

    reading: Callable[[], dict[int, str] | None] = down
    _, recorder, err = world(settings, asking=lambda: reading())
    recorder.sample(NOW)
    recorder.sample(NOW + 60)
    assert err.getvalue().count("asking: ") == 1
    reading = dict
    recorder.sample(NOW + 120)
    assert "asking: readable again" in err.getvalue()


def test_a_stray_is_told_and_an_unreadable_proc_said_once(
    settings: Settings,
) -> None:
    """16/10: the core a forgotten glob held reaches Telegram; /proc
    unreadable is said once in the journal, and changes no episode."""
    sent: list[str] = []
    glob = Stray(pid=2069634, name="python3", percent=98, minutes=17)

    def up() -> list[Stray]:
        return [glob]

    def down() -> list[Stray]:
        raise OSError("/proc gone")

    reading: Callable[[], list[Stray] | None] = up
    _, recorder, err = world(
        settings, sender=lambda: sent.append, strays=lambda: reading()
    )
    recorder.sample(NOW)
    assert sent == [
        "🔴 pid 2069634 (python3) has held 98% of a CPU core for 17 min, outside"
        " the CI, the services and the workers: kill 2069634 if nobody waits for it"
    ]
    reading = down
    recorder.sample(NOW + 60)
    recorder.sample(NOW + 120)
    assert err.getvalue().count("strays: ") == 1
    assert len(sent) == 1
    reading = list
    recorder.sample(NOW + 180)
    assert "strays: readable again" in err.getvalue()
    assert sent[1].startswith("✅ resolved: pid 2069634")


def test_the_cycle_takes_the_nightly_copy_once(settings: Settings) -> None:
    """#842: at 18:00 UTC, today's copy is due; taken once, not every minute."""
    _, recorder, _ = world(settings)
    recorder.sample(NOW)  # the base exists
    _, cycle, err = world(settings)
    cycle.tick()
    copies = list(settings.backup_dir.glob("factory-*.sqlite3"))
    assert [c.name for c in copies] == ["factory-2026-09-30.sqlite3"]
    stamp = copies[0].stat().st_mtime_ns
    cycle.tick()
    assert copies[0].stat().st_mtime_ns == stamp
    assert "sauvegarde" not in err.getvalue()


def test_a_github_job_without_times_bills_unknown_not_zero() -> None:
    from tracerworks.recorder import job_row

    job = {"id": 1, "run_id": 2, "name": "j", "runner_name": "GitHub Actions 3",
           "labels": ["ubuntu-latest"], "conclusion": "failure"}  # fmt: skip
    assert job_row(REPO, job)["billed_minutes"] is None
    on_vps = {**job, "labels": ["self-hosted"], "runner_name": "vps-1"}
    assert job_row(REPO, on_vps)["billed_minutes"] == 0  # the VPS bills nothing


# --- #851: the CI since the project's first run, every page, no duplicate, no hole --


def _run(run_id: int, created: str) -> dict[str, Any]:
    run = json.loads(text("runs.json"))["workflow_runs"][1]  # a deployment-free one
    return {
        **run,
        "id": run_id,
        "run_attempt": 1,
        "status": "completed",
        "created_at": created,
        "run_started_at": created,
        "updated_at": created,
        "workflow_id": 1,
        "path": ".github/workflows/ci.yml",
        "name": "CI",
    }


def github_runs(
    runs: list[dict[str, Any]], *, shift: dict[str, Any] | None = None
) -> Callable[[Any, bytes | None], Any]:
    """GitHub's run listing, 100 a page, newest first; `shift` is a run
    created while the first page is being read: every later page slides by
    one, as on GitHub. Each attempt's jobs: none."""
    from tracerworks.process import Result

    listed = list(runs)

    def answer(argv: Any, stdin: bytes | None) -> Result:
        url = argv[2]
        if "/jobs?" in url:
            return Result(0, json.dumps({"total_count": 0, "jobs": []}), "")
        if not url.startswith(f"{RUNS}?per_page=100&page="):
            return Result(1, "", "not this test's")
        page = int(url.rsplit("=", 1)[1])
        if shift is not None and page == 2 and listed[0] is not shift:
            listed.insert(0, shift)
        part = listed[(page - 1) * 100 : page * 100]
        body = {"total_count": len(listed), "workflow_runs": part}
        return Result(0, json.dumps(body), "")

    return answer


def august_to_october(count: int) -> list[dict[str, Any]]:
    """`count` runs, newest first, from October back into August."""
    start = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    return [
        _run(
            40_000_000_000 - i,
            datetime.fromtimestamp(start - i * 12_000, UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )
        for i in range(count)
    ]


def test_the_ci_backfill_goes_back_to_the_project_s_first_run(
    settings: Settings,
) -> None:
    """No `[history] since`: every run GitHub has, over every page."""
    runs = august_to_october(250)
    assert runs[-1]["created_at"] < "2026-09-01"  # August is in
    fake, recorder, _ = world(replace(settings, history_since=None))
    fake.on("gh", "api", prefix=True)(github_runs(runs))
    assert recorder.backfill()["ci"] == 250
    assert rows(settings, "SELECT count(*), count(DISTINCT run_id) FROM ci_run") == [
        (250, 250)
    ]
    pages = [
        c.argv[2] for c in fake.calls if "actions/runs?per_page=100&page=" in c.argv[2]
    ]
    assert [p.rsplit("=", 1)[1] for p in pages] == ["1", "2", "3"]


def test_replayed_on_a_base_that_has_it_the_backfill_adds_nothing(
    settings: Settings,
) -> None:
    runs = august_to_october(250)
    fake, recorder, _ = world(replace(settings, history_since=None))
    fake.on("gh", "api", prefix=True)(github_runs(runs))
    recorder.backfill()
    with closing(store.connect(settings.history_db)) as db, db:
        db.execute("DELETE FROM backfill WHERE name = 'ci'")  # as migration 4 does
    assert recorder.backfill()["ci"] == 0
    assert rows(settings, "SELECT count(*), count(DISTINCT run_id) FROM ci_run") == [
        (250, 250)
    ]


def test_a_run_created_during_the_walk_leaves_no_hole(settings: Settings) -> None:
    """A new run slides every later page by one: the run at the bottom of
    page 1 comes back at the top of page 2 — read twice, written once."""
    runs = august_to_october(250)
    newer = _run(40_000_000_001, "2026-10-01T00:30:00Z")
    fake, recorder, _ = world(replace(settings, history_since=None))
    fake.on("gh", "api", prefix=True)(github_runs(runs, shift=newer))
    assert recorder.backfill()["ci"] == 250
    assert rows(settings, "SELECT count(DISTINCT run_id) FROM ci_run") == [(250,)]


def test_a_listing_short_of_github_s_total_is_not_marked_done(
    settings: Settings,
) -> None:
    """GitHub says 250 runs, its pages give 200: a hole — said, retried."""
    runs = august_to_october(250)
    fake, recorder, _ = world(replace(settings, history_since=None))
    answer = github_runs(runs)

    def short(argv: Any, stdin: bytes | None) -> Any:
        result = answer(argv, stdin)
        if argv[2].endswith("&page=3"):
            return type(result)(
                0, json.dumps({"total_count": 250, "workflow_runs": []}), ""
            )
        return result

    fake.on("gh", "api", prefix=True)(short)
    report = recorder.backfill()
    assert (
        report["ci"] == "200 runs listed of the 250 GitHub counts: backfill to do again"
    )
    assert ("ci",) not in rows(settings, "SELECT name FROM backfill")


def test_every_read_of_a_job_s_log_allows_escape_sequences() -> None:
    """#1999: `gh` 2.101 refuses a coloured log without the flag. Every read
    of `…/actions/jobs/<id>/logs` in tracerworks — the queue, the light lane,
    the resume, this recorder — goes through `github.job_log`, which passes
    it; a new read written beside it fails here."""
    package = Path(tracerworks.__file__).parent
    readers = sorted(
        str(source.relative_to(package))
        for source in package.rglob("*.py")
        if re.search(r"actions/jobs/\S*/logs", source.read_text(encoding="utf-8"))
    )
    assert readers == ["github.py"]
    fake = FakeRunner()
    fake.answer("gh", "api", prefix=True, stdout="log")
    job_log(fake, REPO, 4242)
    assert fake.calls[-1].argv == (
        "gh", "api", "--allow-escape-sequences",
        f"repos/{REPO}/actions/jobs/4242/logs",
    )  # fmt: skip
