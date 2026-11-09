"""The Qualité tab (#840): each indicator, and the flaky test, on fixed data."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

import pytest

from tracerworks import store
from tracerworks.delivery import pull_stats
from tracerworks.measures import first_green as measured_first_green
from tracerworks.quality import (
    build_quality,
    cost_per_pull,
    first_green,
    flaky_tests,
    green_at_first_try,
    touches,
)

REPO = "owner/repo"
NOW = 1_790_000_000
HOUR, DAY = 3600, 86400
_ids = count(1)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(tmp_path / "f.sqlite3")) as base:
        yield base


def run(
    db: sqlite3.Connection,
    branch: str,
    sha: str,
    at: float,
    conclusion: str = "success",
    path: str = ".github/workflows/ci.yml",
    attempt: int = 1,
    run_id: int | None = None,
    jobs: dict[str, str] | None = None,
    failed: dict[str, list[str]] | None = None,
) -> int:
    """A finished run, its jobs ({name: conclusion}) and the tests each
    failed job names ({job name: [tests]})."""
    run_id = run_id or next(_ids)
    store.insert(db, "ci_run", [{
        "project": REPO, "run_id": run_id, "attempt": attempt, "workflow": "CI",
        "workflow_path": path, "event": "pull_request", "branch": branch,
        "head_sha": sha, "conclusion": conclusion, "created_at": at,
        "started_at": at, "finished_at": at + 60, "title": "",
        "url": f"https://x/runs/{run_id}"}])  # fmt: skip
    for name, outcome in (jobs or {}).items():
        job_id = next(_ids)
        store.insert(db, "ci_job", [{
            "project": REPO, "job_id": job_id, "run_id": run_id, "attempt": attempt,
            "name": name, "conclusion": outcome, "created_at": at,
            "completed_at": at + 60, "billed_minutes": 0, "url": ""}])  # fmt: skip
        tests = (failed or {}).get(name, [])
        store.insert(
            db, "ci_test_failure",
            [{"project": REPO, "job_id": job_id, "test": t} for t in tests],
        )  # fmt: skip
    return run_id


def branch_files(db: sqlite3.Connection, branch: str, files: list[str] | None) -> None:
    store.insert(db, "branch_read", [{"project": REPO, "branch": branch,
        "read_at": NOW, "files": None if files is None else len(files)}])  # fmt: skip
    store.insert(
        db, "branch_file",
        [{"project": REPO, "branch": branch, "path": f} for f in files or []],
    )  # fmt: skip


T = "tests/integration/test_browser_widget_journey.py::test_the_file_moves"
DOCS = ["docs/RUNBOOK.md"]


# --- the flaky test -------------------------------------------------------------------


def test_a_test_that_fails_then_passes_on_the_same_commit_is_flaky(
    db: sqlite3.Connection,
) -> None:
    run(db, "b1", "s1", NOW - HOUR, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]}, run_id=500)  # fmt: skip
    run(db, "b1", "s1", NOW - 30 * 60, jobs={"browser": "success"}, run_id=500,
        attempt=2)  # fmt: skip
    [found] = flaky_tests(db, REPO, "main")
    assert found["test"] == T and found["rules"] == ["même commit"]
    assert found["failures"] == 1 and found["last_url"] == "https://x/runs/500"


def test_passing_on_another_commit_is_not_the_same_commit_rule(
    db: sqlite3.Connection,
) -> None:
    """Contre-épreuve: fixed by a new commit, the test was not flaky."""
    run(db, "b1", "s1", NOW - HOUR, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]})  # fmt: skip
    run(db, "b1", "s2", NOW - 30 * 60, jobs={"browser": "success"})
    assert flaky_tests(db, REPO, "main") == []


def test_failing_on_two_pull_requests_that_do_not_touch_it_is_flaky(
    db: sqlite3.Connection,
) -> None:
    for branch, at in (("b1", NOW - 3 * DAY), ("b2", NOW - DAY)):
        run(db, branch, branch, at, "failure", jobs={"browser": "failure"},
            failed={"browser": [T]})  # fmt: skip
        branch_files(db, branch, DOCS)
    [found] = flaky_tests(db, REPO, "main")
    assert found["rules"] == ["2 PR"] and found["failures"] == 2


@pytest.mark.parametrize(
    ("second", "files", "gap"),
    [
        ("b2", ["src/app/widget/routes.py"], DAY),  # b2 touches the module
        ("b2", ["tests/integration/test_browser_widget_journey.py"], DAY),
        ("b2", None, DAY),  # b2's files are unknown: not counted
        ("b2", DOCS, 10 * DAY),  # 10 days apart: not within 7
        ("b1", DOCS, DAY),  # the same pull request twice
        ("main", DOCS, DAY),  # the base branch is no pull request
    ],
)
def test_two_failures_that_do_not_make_a_flaky_test(
    db: sqlite3.Connection, second: str, files: list[str] | None, gap: int
) -> None:
    run(db, "b1", "s1", NOW - gap - HOUR, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]})  # fmt: skip
    branch_files(db, "b1", DOCS)
    run(db, second, "s2", NOW - HOUR, "failure", jobs={"browser": "failure"},
        failed={"browser": [T]})  # fmt: skip
    if second != "b1":
        branch_files(db, second, files)
    assert flaky_tests(db, REPO, "main") == []


def test_what_touches_a_test_s_module() -> None:
    assert touches(T, ["src/app/widget/routes.py"])  # a folder named by the test
    assert touches(T, ["front/src/journey.ts"])  # a file named by the test
    assert touches(T, ["tests/integration/test_browser_widget_journey.py"])
    assert not touches(T, ["src/app/api/routes.py", "docs/widgets-notes.md"])
    assert not touches(T, ["tests/integration/test_browser_settings.py"])


# --- the indicators -----------------------------------------------------------------


def pull(db: sqlite3.Connection, number: int, branch: str, spec: int | None,
         merged_at: float) -> None:  # fmt: skip
    store.insert(db, "pull_merged", [{"project": REPO, "number": number,
        "title": f"PR {number}", "branch": branch, "spec": spec,
        "created_at": merged_at - DAY, "merged_at": merged_at,
        "url": f"https://x/pull/{number}"}])  # fmt: skip


def test_the_indicators_of_a_fixed_month(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        # 1. cost: 1000 tokens for 2 PRs, 300 for 1 → 500 and 300, mean 400
        for spec, tokens in ((10, 1000), (11, 300)):
            store.insert(db, "chantier", [{"project": REPO, "spec": spec,
                "finished_at": NOW - DAY, "tokens_total": tokens, "tokens_input": 0,
                "tokens_cache_write": 0, "tokens_cache_read": 0, "tokens_output": 0,
                "messages": 1, "source": "clean"}])  # fmt: skip
        pull(db, 1, "agent/10-a", 10, NOW - 2 * DAY)
        pull(db, 2, "agent/10-b", 10, NOW - 2 * DAY + 2 * HOUR)
        pull(db, 3, "agent/11-a", 11, NOW - 2 * DAY)
        # 2. CI: #1 green at once; #2 two attempts; #3 one red then merged
        run(db, "agent/10-a", "a", NOW - 3 * DAY)
        run(db, "agent/10-b", "b", NOW - 3 * DAY, "failure")
        run(db, "agent/10-b", "b2", NOW - 3 * DAY + HOUR)
        run(db, "agent/11-a", "c", NOW - 3 * DAY, "failure")
        run(db, "agent/10-a", "a", NOW - 3 * DAY, path=".github/workflows/deploy.yml")
        run(db, "agent/10-a", "a", NOW - 3 * DAY, "skipped")
        run(db, "agent/10-a", "z", NOW - DAY)  # after the merge: not its CI
        run(db, "agent/10-a", "a0", NOW - 4 * DAY, "cancelled")  # judged nothing
        # 3. cycle: spec 10 published 1 day before its last merge, deployed 1 h after
        store.insert(
            db,
            "spec",
            [
                {
                    "number": 10,
                    "project": REPO,
                    "title": "S10",
                    "created_at": NOW - 3 * DAY + 2 * HOUR,
                },
                {
                    "number": 11,
                    "project": REPO,
                    "title": "S11",
                    "created_at": NOW - 4 * DAY,
                },
            ],
        )
        store.insert(
            db,
            "deployment",
            [
                {
                    "project": REPO,
                    "run_id": 9,
                    "head_sha": "d",
                    "at": NOW - 2 * DAY + 3 * HOUR,
                    "url": "",
                }
            ],
        )
        # 4. the operator: 1 h done, 30 min open, one long before the window
        store.insert(
            db,
            "needs_human",
            [
                {
                    "project": REPO,
                    "issue": 7,
                    "started_at": NOW - 2 * HOUR,
                    "ended_at": NOW - HOUR,
                },
                {"project": REPO, "issue": 8, "started_at": NOW - 30 * 60},
                {
                    "project": REPO,
                    "issue": 6,
                    "started_at": NOW - 60 * DAY,
                    "ended_at": NOW - 59 * DAY,
                },
            ],
        )
        # 5. runners: GitHub waits 10, 20, 30 s; the VPS 100 s
        host = run(db, "other", "o", NOW - HOUR, path="x.yml")
        for machine, wait in (
            ("github", 10),
            ("github", 20),
            ("github", 30),
            ("vps", 100),
        ):
            store.insert(db, "ci_job", [{"project": REPO, "job_id": next(_ids),
                "run_id": host, "attempt": 1, "name": "j", "machine": machine,
                "conclusion": "success", "created_at": NOW - HOUR, "wait_s": wait,
                "billed_minutes": 0, "url": ""}])  # fmt: skip
    q = build_quality(path, REPO, "ci.yml", "main", NOW)
    assert q["error"] is None
    assert q["cost"]["mean"] == 400
    assert {i["spec"]: i["cost"] for i in q["cost"]["items"]} == {10: 500, 11: 300}
    fg = q["first_green"]
    assert (fg["pulls"], fg["first_green"], fg["share"], fg["mean_attempts"]) == (
        3,
        1,
        33.3,
        1.33,
    )
    assert [p["number"] for p in fg["worst"]] == [2, 3], "those with a red"
    cycle = q["cycle"]
    by_spec = {i["spec"]: i for i in cycle["items"]}
    assert by_spec[10]["to_merge"] == DAY and by_spec[10]["to_prod"] == HOUR
    assert by_spec[11]["to_prod"] == 3 * HOUR  # the same deployment ships it
    assert cycle["not_in_prod"] == 0
    op = q["operator"]
    assert op["total"] == 1.5 * HOUR and op["open"] == [
        {"issue": 8, "since": NOW - 30 * 60}
    ]
    assert (
        q["runners"]["github"]["median"] == 20 and q["runners"]["vps"]["median"] == 100
    )
    assert q["runners"]["github"]["jobs"] == 3


def test_an_empty_base_says_unknown_not_zero(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    store.connect(path).close()
    q = build_quality(path, REPO, "ci.yml", "main", NOW)
    assert q["cost"]["mean"] is None and q["first_green"]["share"] is None
    assert q["cycle"]["median_to_merge"] is None and q["flaky"] == []
    assert q["runners"]["github"]["median"] is None and q["alerts"] == []


def test_the_alerts_of_the_window_and_those_still_open(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        store.insert(db, "alert", [
            {"key": "disk", "opened_at": NOW - HOUR, "text": "disque à 90 %",
             "closed_at": NOW - 60, "opened_sent_at": NOW - HOUR},
            {"key": "claude seven_day", "opened_at": NOW - 40 * DAY, "text": "quota"},
            {"key": "disk", "opened_at": NOW - 40 * DAY, "text": "vieux",
             "closed_at": NOW - 39 * DAY}])  # fmt: skip
    alerts = build_quality(path, REPO, "ci.yml", "main", NOW)["alerts"]
    assert [(a["key"], a["sent"]) for a in alerts] == [
        ("disk", True),
        ("claude seven_day", False),  # 40 days old, but still open
    ]


def test_the_cost_per_merged_pr_is_drawn_by_day_over_30_days(tmp_path: Path) -> None:
    """The operator, 01/10 (#853): one bar per day — the mean of the specs finished
    that day; a day without a finished spec has no bar (absent, not zero)."""
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        finished = (
            (20, 1000, NOW - DAY),
            (21, 300, NOW - DAY + HOUR),
            (22, 600, NOW - 5 * DAY),
            (23, 9000, NOW - 30 * DAY + HOUR),
        )  # fmt: skip  (31st day: out)
        for number, (spec, tokens, at) in enumerate(finished, start=1):
            store.insert(db, "chantier", [{"project": REPO, "spec": spec,
                "finished_at": at, "tokens_total": tokens,
                "tokens_input": tokens // 100, "tokens_cache_write": tokens // 10,
                "tokens_cache_read": tokens // 2,
                "tokens_output": tokens - tokens // 100 - tokens // 10 - tokens // 2,
                "messages": 1, "source": "clean"}])  # fmt: skip
            pull(db, number, f"agent/{spec}-a", spec, at - HOUR)
        days = cost_per_pull(db, REPO, NOW)["days"]

    def day(t: float) -> str:
        return datetime.fromtimestamp(t, UTC).date().isoformat()

    # then the same mean by category (#859): input, cache written, read, output
    assert days == [
        [day(NOW - 5 * DAY), 600, 1, 6, 60, 300, 234],
        [day(NOW - DAY), 650, 2, 6.5, 65, 325, 253.5],
    ]


# --- green at first try: one rule, three views (#2293) ------------------------------


@pytest.mark.parametrize(
    ("verdicts", "green"),
    [
        (["success"], True),
        # the light lane, then the queue's full suite: no red, green at once
        (["success", "success"], True),
        (["failure", "success", "success"], False),
        (["success", "failure", "success"], False),
        (["cancelled", "success"], True),  # a cancelled run judged nothing
        (["skipped", "success", "cancelled"], True),
        (["cancelled", "skipped"], None),  # no run judged: unknown, never false
        ([], None),
    ],
)
def test_green_at_first_try_means_no_red_before_the_merge(
    verdicts: list[str], green: bool | None
) -> None:
    assert green_at_first_try(verdicts) is green


def test_quality_measures_and_the_card_agree_on_each_pull(
    db: sqlite3.Connection,
) -> None:
    """#1 light lane green, queue suite green; #2 one red, then two greens;
    #3 one cancelled, then one green."""
    for number in (1, 2, 3):
        pull(db, number, f"agent/{number}", None, NOW - DAY)
    opened = NOW - 2 * DAY  # each opened a day before its merge
    run(db, "agent/1", "a", opened + HOUR)  # its light lane
    run(db, "agent/1", "a2", opened + 2 * HOUR)  # the queue's full suite
    run(db, "agent/2", "b", opened + HOUR, "failure")
    run(db, "agent/2", "b2", opened + 2 * HOUR)
    run(db, "agent/2", "b3", opened + 3 * HOUR)
    run(db, "agent/3", "c", opened + HOUR, "cancelled")
    run(db, "agent/3", "c2", opened + 2 * HOUR)
    expected = {1: True, 2: False, 3: True}
    quality = first_green(db, REPO, "ci.yml", NOW)
    assert (quality["pulls"], quality["first_green"]) == (3, 2)
    assert [p["number"] for p in quality["worst"]] == [2], "only the one with a red"
    measured = sorted(
        v for _, v in measured_first_green(db, REPO, "ci.yml", NOW - 7 * DAY)
    )
    assert measured == sorted(100.0 if g else 0.0 for g in expected.values())
    for number, green in expected.items():
        stats = pull_stats(db, REPO, "ci.yml", number)
        assert stats is not None and stats["ci"]["first_green"] is green, number
