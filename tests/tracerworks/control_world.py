"""The CI of the recorded world, as GitHub answers it (payloads trimmed to
the fields read, shapes recorded on 30/09 from acme-ops's repository).

- PR #817, branch of #501 (working): one job running on vps-1, one
  waiting for a runner; auto-merge set;
- PR #814, `fix/scopes`: its run queued, no job started — waits for a runner;
- PR #815, branch of #520: CI red;
- PR #816, `docs/runbook`: CI green, auto-merge not set;
- runners: vps-1 busy, vps-2 idle, vps-3 offline;
- completed runs: #90 green (PR #812, vps-2), #91 red (main, nightly,
  vps-1 and vps-3), #92 a skipped Deploy (never shown).
"""

from __future__ import annotations

import json
from typing import Any

from tests.tracerworks.fakes import FakeRunner

REPO = "owner/repo"
PR_FIELDS = "number,title,headRefName,autoMergeRequest,statusCheckRollup,url,mergeable"


def check(
    status: str,
    conclusion: str = "",
    started: str | None = None,
    run_id: int | None = None,
    completed: str | None = None,
) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": "tests",
        "status": status,
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed,
        "workflowName": "CI",
        **(
            {}
            if run_id is None
            else {
                "detailsUrl": f"https://github.com/{REPO}/actions/runs/{run_id}/job/7"
            }
        ),
    }


def pr(n: int, branch: str, checks: list[dict[str, Any]], auto: bool) -> dict[str, Any]:
    return {
        "number": n,
        "title": f"PR {n}",
        "headRefName": branch,
        "autoMergeRequest": {"enabledAt": "2026-09-26T07:00:00Z"} if auto else None,
        "statusCheckRollup": checks,
        "url": f"https://github.com/{REPO}/pull/{n}",
    }


PULLS = [
    pr(
        817,
        "agent/501-console-operateur",
        [
            check("COMPLETED", "SUCCESS", "2026-09-26T07:00:00Z", run_id=99),
            check("IN_PROGRESS", "", "2026-09-26T07:02:00Z", run_id=101),
            check("QUEUED", run_id=101),
        ],
        auto=True,
    ),
    pr(814, "fix/scopes", [check("QUEUED", run_id=100)], auto=True),
    pr(
        815,
        "agent/520-dashboard",
        [
            check(
                "COMPLETED",
                "FAILURE",
                "2026-09-26T06:00:00Z",
                completed="2026-09-26T06:05:00Z",
            )
        ],
        auto=True,
    ),
    pr(
        816,
        "docs/runbook",
        [check("COMPLETED", "SUCCESS", "2026-09-26T05:00:00Z")],
        auto=False,
    ),
]


def run(
    run_id: int,
    name: str,
    branch: str,
    prs: list[int],
    conclusion: str | None = None,
    *,
    status: str = "completed",
    started: str = "2026-09-26T06:30:00Z",
    updated: str = "2026-09-26T06:36:12Z",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "display_title": f"{name} on {branch}",
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion,
        "pull_requests": [{"number": n} for n in prs],
        "created_at": started,
        "run_started_at": started,
        "updated_at": updated,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }


#: The dated listing (`actions/runs`, no `status=`), newest created first —
#: the one source of the Contrôle's runs and the failed-run events (#825).
LATEST_RUNS = [
    run(
        101,
        "CI",
        "agent/501-console-operateur",
        [817],
        status="in_progress",
        started="2026-09-26T07:00:00Z",
        updated="2026-09-26T07:02:00Z",
    ),
    run(
        100,
        "CI",
        "fix/scopes",
        [814],
        status="queued",
        started="2026-09-26T06:59:00Z",
        updated="2026-09-26T06:59:00Z",
    ),
    run(
        92,
        "Deploy",
        "main",
        [],
        "skipped",
        started="2026-09-26T06:40:00Z",
        updated="2026-09-26T06:40:02Z",
    ),
    run(
        91,
        "Nightly",
        "main",
        [],
        "failure",
        started="2026-09-26T06:00:00Z",
        updated="2026-09-26T06:20:00Z",
    ),
    run(
        90,
        "CI",
        "agent/512-x",
        [812],
        "success",
        started="2026-09-26T06:30:00Z",
        updated="2026-09-26T06:36:12Z",
    ),
    run(
        80,
        "CI",
        "main",
        [],
        "failure",
        started="2026-09-20T05:50:00Z",
        updated="2026-09-20T06:00:00Z",
    ),
    # a month old: never « recent », whatever an index says (#825)
    run(
        70,
        "CI",
        "main",
        [],
        "failure",
        started="2026-08-27T09:00:00Z",
        updated="2026-08-27T09:10:00Z",
    ),
]


def latest_answer(fake: FakeRunner, runs: list[dict[str, Any]], **kw: Any) -> None:
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/actions/runs?per_page=100",
        stdout=json.dumps({"workflow_runs": runs}),
        **kw,
    )


def job(
    job_id: int,
    name: str,
    status: str,
    runner: str | None,
    conclusion: str | None = None,
    labels: tuple[str, ...] = ("self-hosted",),
    **times: str,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "runner_name": runner,
        "labels": list(labels),
        "created_at": times.get("created", "2026-09-26T07:01:00Z"),
        "started_at": times.get("started"),
        "completed_at": times.get("completed"),
        "html_url": f"https://github.com/{REPO}/actions/runs/1/job/{job_id}",
    }


def runs_answer(
    fake: FakeRunner, status: str, runs: list[dict[str, Any]], **kw: Any
) -> None:
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/actions/runs?status={status}&per_page="
        + ("20" if status == "completed" else "100"),
        stdout=json.dumps({"workflow_runs": runs}),
        **kw,
    )


def jobs_answer(fake: FakeRunner, run_id: int, jobs: list[dict[str, Any]]) -> None:
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=100",
        stdout=json.dumps({"jobs": jobs}),
    )


def control_world(fake: FakeRunner) -> FakeRunner:
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
        stdout=json.dumps(PULLS),
    )
    jobs_answer(
        fake,
        101,
        [
            job(
                1,
                "lint",
                "completed",
                "vps-2",
                "success",
                started="2026-09-26T07:00:00Z",
                completed="2026-09-26T07:01:30Z",
            ),
            job(
                10,
                "front",
                "completed",
                "GitHub Actions 7",
                "skipped",
                labels=("ubuntu-latest",),
                started="2026-09-26T07:00:00Z",
                completed="2026-09-26T07:00:00Z",
            ),
            job(
                2,
                "tests (tranche 1/2)",
                "in_progress",
                "vps-1",
                started="2026-09-26T07:02:00Z",
            ),
            job(3, "tests (tranche 2/2)", "queued", None),
        ],
    )
    jobs_answer(
        fake, 100, [job(4, "checks", "queued", None, created="2026-09-26T07:05:00Z")]
    )
    # #817's other workflow, finished
    jobs_answer(
        fake,
        99,
        [
            job(
                11,
                "docs",
                "completed",
                "GitHub Actions 3",
                "success",
                labels=("ubuntu-latest",),
                started="2026-09-26T06:58:00Z",
                completed="2026-09-26T06:59:00Z",
            )
        ],
    )
    latest_answer(fake, LATEST_RUNS)
    jobs_answer(fake, 90, [job(5, "tests", "completed", "vps-2")])
    jobs_answer(fake, 80, [job(8, "tests", "completed", "vps-1")])
    jobs_answer(fake, 70, [job(9, "tests", "completed", "vps-1")])
    jobs_answer(
        fake,
        91,
        [job(6, "a", "completed", "vps-3"), job(7, "b", "completed", "vps-1")],
    )
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/actions/runners",
        stdout=json.dumps(
            {
                "runners": [
                    {"name": "vps-1", "status": "online", "busy": True},
                    {"name": "vps-2", "status": "online", "busy": False},
                    {"name": "vps-3", "status": "offline", "busy": False},
                    {"name": "someone-elses", "status": "online", "busy": True},
                ]
            }
        ),
    )
    return fake
