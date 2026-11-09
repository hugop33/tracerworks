"""The Contrôle post: what the CI does, and for whom (#810).

Recorded world: `control_world.py` (on top of the snapshot's world).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.tracerworks.control_world import (
    PR_FIELDS,
    PULLS,
    REPO,
    check,
    latest_answer,
    pr,
    run,
    runs_answer,
)
from tests.tracerworks.fakes import FakeRunner, recorded_world
from tests.tracerworks.test_snapshot import NOW, github_world, snapshot, workers_of
from tracerworks.control import Memo


def control(fake: FakeRunner, tmp_path: Path, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = snapshot(fake, tmp_path, **kw)["control"]
    return data


def pulls_by_number(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {p["number"]: p for p in data["pull_requests"]}


def test_each_pr_says_its_ci_its_auto_merge_and_its_chantier(tmp_path: Path) -> None:
    pulls = pulls_by_number(control(github_world(recorded_world()), tmp_path))
    assert {n: (p["ci"], p["auto_merge"], p["spec"]) for n, p in pulls.items()} == {
        817: ("running", True, 501),
        814: ("queued", True, None),
        815: ("failure", True, 520),
        816: ("success", False, None),
    }
    assert pulls[817]["ci_since"] == 1790406000  # 2026-09-26T07:00:00Z, first start
    assert pulls[817]["url"] == f"https://github.com/{REPO}/pull/817"


def test_each_pr_links_its_current_or_last_ci_run(tmp_path: Path) -> None:
    """The operator, 01/10 (#859): « Open CI » in a PR's panel — the run started
    last, a queued one when none has started; no check, no link."""
    pulls = pulls_by_number(control(github_world(recorded_world()), tmp_path))
    runs = f"https://github.com/{REPO}/actions/runs"
    assert {n: p["ci_url"] for n, p in pulls.items()} == {
        817: f"{runs}/101",
        814: f"{runs}/100",
        815: None,  # its check names no run
        816: None,
    }


def test_a_pr_in_ci_lists_its_jobs_as_github_does(tmp_path: Path) -> None:
    """The operator, 01/10 (#859): name, state, machine, and what the chrono needs —
    waiting, running since, finished in."""
    pulls = pulls_by_number(control(github_world(recorded_world()), tmp_path))
    jobs = [
        (j["name"], j["state"], j["machine"], j["started_at"], j["completed_at"])
        for j in pulls[817]["ci_jobs"]
    ]
    assert jobs == [
        # every workflow of the PR, the oldest first, as GitHub lists them
        ("docs", "success", "github", 1790405880, 1790405940),
        ("lint", "success", "vps", 1790406000, 1790406090),
        ("front", "skipped", "github", 1790406000, 1790406000),
        ("tests (tranche 1/2)", "running", "vps", 1790406120, None),
        ("tests (tranche 2/2)", "waiting", None, None, None),
    ]
    assert pulls[817]["ci_jobs"][1]["url"].endswith("/job/1")
    assert [j["state"] for j in pulls[814]["ci_jobs"]] == ["waiting"]
    assert pulls[816]["ci_jobs"] == []  # no run: nothing to list


def test_a_finished_run_s_jobs_are_asked_once_while_a_post_shows_it(
    tmp_path: Path,
) -> None:
    """control.py's promise, kept for the PR panel too (#859): #817's
    finished run is asked once; its running one at every snapshot."""
    from tests.tracerworks.control_world import LATEST_RUNS

    memo: Memo = {}
    fake = github_world(recorded_world())
    listed = [{**r, "run_attempt": 1} for r in LATEST_RUNS]
    latest_answer(fake, [*listed, {**run(99, "Docs", "x", [817], "success"),
                                    "run_attempt": 1}])  # fmt: skip
    for _ in range(3):
        snapshot(fake, tmp_path, memo=memo)
    asked = fake.ran("gh", "api", f"repos/{REPO}/actions/runs/99/jobs?per_page=100")
    assert len(asked) == 1
    assert (99, 1) in memo and not [k for k in memo if k[0] == 101]


def test_a_run_the_listing_does_not_give_is_asked_again_while_its_pr_runs(
    tmp_path: Path,
) -> None:
    """Older than the newest hundred, its attempt unknown: it may be a
    rerun (#2079), never served from the memo while its PR's CI runs; a PR
    whose CI is over keeps it."""
    memo: Memo = {}
    fake = github_world(recorded_world())  # run 99 (#817, running) unlisted
    for _ in range(2):
        snapshot(fake, tmp_path, memo=memo)
    asked = fake.ran("gh", "api", f"repos/{REPO}/actions/runs/99/jobs?per_page=100")
    assert len(asked) == 2
    assert (99, None) not in memo


def test_a_new_attempt_listed_before_its_jobs_is_not_kept_as_it(
    tmp_path: Path,
) -> None:
    """GitHub may list attempt 2 while `/jobs` still gives attempt 1's:
    never kept under attempt 2."""
    memo: Memo = {}
    first = [ci_job("integration (1/2)", "failure", 1)]
    fake = rerun_world(2, first)
    control(fake, tmp_path, memo=memo)
    assert (101, 2) not in memo


def test_jobs_waiting_for_a_runner(tmp_path: Path) -> None:
    waiting = control(github_world(recorded_world()), tmp_path)["waiting_jobs"]
    assert [(j["job"], j["pr"], j["branch"]) for j in waiting] == [
        ("tests (tranche 2/2)", 817, "agent/501-console-operateur"),
        ("checks", 814, "fix/scopes"),
    ]
    assert waiting[1]["since"] == 1790406300  # created 07:05


def test_every_runner_of_the_machine_busy_with_which_job(tmp_path: Path) -> None:
    runners = control(github_world(recorded_world()), tmp_path)["runners"]
    assert [(r["name"], r["status"], r["busy"]) for r in runners] == [
        ("vps-1", "online", True),
        ("vps-2", "online", False),
        ("vps-3", "offline", False),
    ]
    busy = runners[0]["job"]
    assert (busy["name"], busy["pr"], busy["run_id"]) == (
        "tests (tranche 1/2)",
        817,
        101,
    )
    assert busy["started_at"] == 1790406120
    assert runners[1]["job"] is None


def test_the_last_five_finished_runs_skipped_ones_left_out(tmp_path: Path) -> None:
    runs = control(github_world(recorded_world()), tmp_path)["recent_runs"]
    assert [(r["id"], r["success"], r["pr"], r["runners"]) for r in runs] == [
        (90, True, 812, ["vps-2"]),
        (91, False, None, ["vps-1", "vps-3"]),
        (80, False, None, ["vps-1"]),  # six days old: still recent
    ]
    assert runs[0]["duration"] == 372  # 06:30:00 → 06:36:12


def test_recent_runs_come_from_the_dated_list_never_the_status_index(
    tmp_path: Path,
) -> None:
    """#825: `actions/runs?status=` goes through a search index that can lag
    or come back out of order — The operator saw five red runs, one a month old. The
    dated listing is the source; tracerworks filters and sorts; nothing older
    than seven days is « recent »."""
    fake = github_world(recorded_world())
    stale_index = [
        run(70, "CI", "main", [], "failure", updated="2026-08-27T09:10:00Z"),
        run(91, "Nightly", "main", [], "failure"),
    ]
    runs_answer(fake, "completed", stale_index)
    runs = control(fake, tmp_path)["recent_runs"]
    assert [r["id"] for r in runs] == [90, 91, 80]  # last finished first
    assert all(r["finished_at"] >= NOW - 7 * 86400 for r in runs)


def test_a_finished_run_s_runners_are_asked_once(tmp_path: Path) -> None:
    memo: Memo = {}
    fake = github_world(recorded_world())
    snapshot(fake, tmp_path, memo=memo)
    snapshot(fake, tmp_path, memo=memo)
    asked = fake.ran("gh", "api", f"repos/{REPO}/actions/runs/90/jobs?per_page=100")
    assert len(asked) == 1
    # The running run is asked again each time: it changes.
    assert (
        len(fake.ran("gh", "api", f"repos/{REPO}/actions/runs/101/jobs?per_page=100"))
        == 2
    )


def test_a_waiting_worker_whose_pr_runs_its_ci_waits_for_the_ci(tmp_path: Path) -> None:
    """Without a question to Sam (#830: a question makes it « blocked »)."""
    from tests.tracerworks.test_worker_states import without_the_question

    fake = without_the_question(github_world(recorded_world()))
    pulls = [*PULLS, pr(818, "agent/510-classification", [check("QUEUED")], auto=True)]
    fake.answer(
        "gh",
        "pr",
        "list",
        "-R",
        REPO,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        PR_FIELDS,
        stdout=json.dumps(pulls),
    )
    worker = workers_of(snapshot(fake, tmp_path))[510]
    assert worker["state"] == "ci"
    assert worker["pull_request"] == {"number": 818, "ci": "queued"}


def test_without_a_running_ci_a_waiting_worker_is_blocked(tmp_path: Path) -> None:
    workers = workers_of(snapshot(github_world(recorded_world()), tmp_path))
    assert workers[510]["state"] == "blocked"
    assert workers[510]["pull_request"] is None
    assert workers[520]["pull_request"] == {"number": 815, "ci": "failure"}
    assert workers[501]["state"] == "active"  # working, whatever its CI


def test_unreadable_ci_says_unknown(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    fake.answer("gh", "pr", "list", prefix=True, returncode=1, stdout="HTTP 502")
    latest_answer(fake, [], returncode=1)
    fake.answer("gh", "api", f"repos/{REPO}/actions/runners", returncode=1, stdout="x")
    data = snapshot(fake, tmp_path)
    assert data["control"]["pull_requests"] is None
    assert data["control"]["waiting_jobs"] is None
    assert data["control"]["runners"] is None
    assert data["control"]["recent_runs"] is None  # one source for both (#825)
    assert workers_of(data)[510]["pull_request"] is None
    assert workers_of(data)[510]["state"] == "blocked"


def test_a_malformed_row_makes_its_part_unknown_not_the_page(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    broken = [*PULLS, {"title": "no number"}]
    fake.answer(
        "gh", "pr", "list", "-R", REPO, "--state", "open", "--limit", "100", "--json",
        PR_FIELDS,
        stdout=json.dumps(broken),
    )  # fmt: skip
    data = snapshot(fake, tmp_path)
    assert data["control"]["pull_requests"] is None
    assert data["control"]["runners"] is not None
    assert data["workers"]


# --- a rerun read again: the card follows the last attempt (#2079) -------------


def rerun_world(attempt: int, jobs: list[dict[str, Any]]) -> FakeRunner:
    """#817's run 101 finished, at `attempt`, with `jobs` — the 29/10
    scenario of #2063: red on its first attempt, green on its second."""
    from tests.tracerworks.control_world import LATEST_RUNS, jobs_answer

    fake = github_world(recorded_world())
    listed = [dict(r) for r in LATEST_RUNS]
    for r in listed:
        r["run_attempt"] = attempt if r["id"] == 101 else 1
        if r["id"] == 101:
            r["status"], r["conclusion"] = "completed", "success"
    latest_answer(fake, listed)
    jobs_answer(fake, 101, jobs)
    return fake


def ci_job(name: str, conclusion: str, attempt: int, **times: str) -> dict[str, Any]:
    from tests.tracerworks.control_world import job

    return {
        **job(7, name, "completed", "vps-1", conclusion, ("self-hosted",), **times),
        "run_attempt": attempt,
    }


def test_a_finished_run_rerun_is_read_again_and_its_dots_follow(
    tmp_path: Path,
) -> None:
    memo: Memo = {}
    first = [ci_job("integration (1/2)", "failure", 1),
             ci_job("integration (2/2)", "failure", 1)]  # fmt: skip
    fake = rerun_world(1, first)
    pulls = pulls_by_number(control(fake, tmp_path, memo=memo))
    assert [
        j["state"] for j in pulls[817]["ci_jobs"] if j["name"].startswith("int")
    ] == [
        "failure",
        "failure",
    ]

    second = [ci_job("integration (1/2)", "success", 2),
              ci_job("integration (2/2)", "success", 2)]  # fmt: skip
    fake = rerun_world(2, second)
    pulls = pulls_by_number(control(fake, tmp_path, memo=memo))
    assert [
        j["state"] for j in pulls[817]["ci_jobs"] if j["name"].startswith("int")
    ] == [
        "success",
        "success",
    ]
    asked = fake.ran("gh", "api", f"repos/{REPO}/actions/runs/101/jobs?per_page=100")
    assert len(asked) == 1, "the second attempt is asked, once"
    control(fake, tmp_path, memo=memo)
    asked = fake.ran("gh", "api", f"repos/{REPO}/actions/runs/101/jobs?per_page=100")
    assert len(asked) == 1, "then kept: finished, its attempt unchanged"


def test_a_rerun_s_ci_since_starts_at_its_current_attempt(tmp_path: Path) -> None:
    """`--failed` reruns the red jobs only: the green one stays the first
    attempt's, started an hour before — it is not the rerun's start."""
    from tests.tracerworks.control_world import job

    fake = rerun_world(
        2,
        [
            ci_job("checks", "success", 1, started="2026-09-26T06:00:00Z",
                   created="2026-09-26T06:00:00Z"),
            {**job(8, "integration (2/2)", "in_progress", "vps-1",
                   started="2026-09-26T07:10:00Z", created="2026-09-26T07:09:00Z"),
             "run_attempt": 2},
        ],
    )  # fmt: skip
    pulls = pulls_by_number(control(fake, tmp_path))
    assert pulls[817]["ci"] == "running"
    assert pulls[817]["ci_since"] == 1790406600  # 07:10, not 06:00 nor 07:00


def test_a_rerun_finished_beside_another_running_is_not_its_start(
    tmp_path: Path,
) -> None:
    """Workflow 99 rerun at 06:00 and green; 101 not rerun, running since
    07:00: the CI runs since 07:00 (its checks), not 06:00."""
    from tests.tracerworks.control_world import jobs_answer

    fake = github_world(recorded_world())
    jobs_answer(fake, 99, [ci_job("docs", "success", 2,
                                  started="2026-09-26T06:00:00Z")])  # fmt: skip
    pulls = pulls_by_number(control(fake, tmp_path))
    assert pulls[817]["ci"] == "running"
    assert pulls[817]["ci_since"] == 1790406000


def test_a_job_waits_for_a_runner_since_its_own_creation(tmp_path: Path) -> None:
    """#814 queued: its job was created at 07:05 — the wait the card shows."""
    pulls = pulls_by_number(control(github_world(recorded_world()), tmp_path))
    assert pulls[814]["ci"] == "queued"
    assert pulls[814]["ci_since"] == 1790406300
