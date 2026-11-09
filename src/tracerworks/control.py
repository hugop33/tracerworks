"""The Contrôle post of the dashboard: what the CI does, and for whom (#810).

Read from GitHub at each snapshot: the open pull requests and the state of
their checks, the runs in progress or queued and their jobs (a queued job
waits for a runner; a running one names its runner), every runner the
project lists, and the last finished runs. A part GitHub could not give is
None — unknown, never an empty list.

A finished run does not change until it is rerun: its jobs are asked once
per ATTEMPT and kept in `memo` (held by the long-running `serve`) while a
post shows it — the runners of the last runs, the jobs of an open PR (#859) —
so a refresh costs a few calls. A run whose attempt changed is read again
(#2079, 29/10: #2063's run 40917525833, green on its second attempt, kept the
two red dots of its first on the card). Its attempt is read with the run, in
GitHub's `run_attempt`.

How long a PR's CI has run, or waited (`ci_since`): a job waiting for a
runner since its own creation; a rerun since its current attempt's first
job — never the first attempt's checks (« waiting for a runner · for 55
min » for a 5 min wait).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import partial
from typing import Any

from tracerworks.conventions import Conventions
from tracerworks.process import Runner

PR_FIELDS = "number,title,headRefName,autoMergeRequest,statusCheckRollup,url,mergeable"
RECENT_RUNS = 30  # as many as the Control post shows (the operator, 01/10)
#: Older than this, a run is never « recent », whatever GitHub hands back.
RECENT_WINDOW = 7 * 86400
#: The dated listing read at once: the active runs and the last finished
#: ones are all among the newest hundred.
LATEST_RUNS = 100
#: A run not finished yet, by its `status` in the dated listing.
_ACTIVE_STATUSES = {"in_progress", "queued", "waiting", "pending", "requested"}
#: Finished runs that did no work: not shown.
_IDLE_CONCLUSIONS = {"skipped", "neutral", "stale"}
_RUNNING = {"IN_PROGRESS"}
_WAITING = {"QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
_RED = {
    "FAILURE",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "ERROR",
}

#: a finished run's jobs, by run id and attempt (None: not in the listing)
Memo = dict[tuple[int, int | None], list[dict[str, Any]]]
Attempts = dict[int, int]  # each listed run's current attempt


def epoch(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def guarded[T](read: Callable[[], T]) -> T | None:
    """A reading whose payload had an unexpected shape (a row without its
    number, a string for an id) is unknown — never a crash of the whole
    snapshot, whose other parts stand."""
    try:
        return read()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def read_json(runner: Runner, argv: Sequence[str]) -> Any:
    result = runner(argv)
    if not result.ok:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def ci_state(checks: Sequence[dict[str, Any]]) -> str:
    """running | queued (waits for a runner) | failure | success | none."""
    states = [str(c.get("status") or c.get("state") or "").upper() for c in checks]
    conclusions = [
        str(c.get("conclusion") or c.get("state") or "").upper() for c in checks
    ]
    if not checks:
        return "none"
    if any(s in _RUNNING for s in states):
        return "running"
    if any(s in _WAITING for s in states):
        return "queued"
    if any(c in _RED for c in conclusions):
        return "failure"
    return "success"


_RUN_URL = re.compile(r"^https://[^/]+/[^/]+/[^/]+/actions/runs/(\d+)(?:/|$)")


def ci_runs(checks: Sequence[dict[str, Any]]) -> list[tuple[int, str]]:
    """The Actions runs a PR's checks belong to — one per workflow — as (id,
    url), started last first; a queued one, not started, counts as oldest."""
    started: dict[int, int] = {}
    for check in checks:
        if match := _RUN_URL.match(str(check.get("detailsUrl") or "")):
            run_id = int(match.group(1))
            at = epoch(check.get("startedAt")) or 0
            started[run_id] = max(started.get(run_id, 0), at)
    ordered = sorted(started, key=lambda r: (started[r], r), reverse=True)
    return [(r, _run_url(checks, r)) for r in ordered]


def _run_url(checks: Sequence[dict[str, Any]], run_id: int) -> str:
    url = next(
        str(c["detailsUrl"])
        for c in checks
        if (m := _RUN_URL.match(str(c.get("detailsUrl") or "")))
        and int(m.group(1)) == run_id
    )
    return url[: url.index(f"/runs/{run_id}") + len(f"/runs/{run_id}")]


def _pull_requests(
    runner: Runner, repo: str, conventions: Conventions
) -> list[dict[str, Any]] | None:
    payload = read_json(
        runner,
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            PR_FIELDS,
        ],
    )
    if not isinstance(payload, list):
        return None
    pulls = []
    for item in payload:
        checks = item.get("statusCheckRollup") or []
        branch = str(item.get("headRefName") or "")
        parsed = conventions.parse_branch(branch)
        started = [s for c in checks if (s := epoch(c.get("startedAt"))) is not None]
        ended = [e for c in checks if (e := epoch(c.get("completedAt"))) is not None]
        state = ci_state(checks)
        pulls.append(
            {
                "number": int(item["number"]),
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "branch": branch,
                "spec": parsed[0] if parsed else None,
                "ci": state,
                # « Open CI » (#859): the run started last, else one queued
                "ci_url": runs[0][1] if (runs := ci_runs(checks)) else None,
                "ci_run_ids": [r for r, _ in runs],
                "ci_since": min(started)
                if started and state in ("running", "queued")
                else None,
                # when its CI ended red: its last job's end (#2057)
                "ci_red_at": max(ended) if ended and state == "failure" else None,
                "auto_merge": bool(item.get("autoMergeRequest")),
                "conflicting": item.get("mergeable") == "CONFLICTING",
            }
        )
    return pulls


def workflow_runs(runner: Runner, path: str) -> list[dict[str, Any]] | None:
    """`gh api <path>` of a runs listing → its runs; None when unreadable."""
    payload = read_json(runner, ["gh", "api", path])
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    return list(runs) if isinstance(runs, list) else None


def latest_runs(runner: Runner, repo: str) -> list[dict[str, Any]] | None:
    """The newest runs, by date — NEVER `?status=` (#825): that filter goes
    through a search index that can lag or come back out of order (five red
    runs shown, one a month old). Filtering and sorting are tracerworks's."""
    return workflow_runs(runner, f"repos/{repo}/actions/runs?per_page={LATEST_RUNS}")


def _jobs(runner: Runner, repo: str, run_id: int) -> list[dict[str, Any]] | None:
    payload = read_json(
        runner, ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return None
    return list(payload["jobs"])


_WAITING_JOB = ("queued", "waiting", "pending", "requested")


def machine_of(job: dict[str, Any]) -> str | None:
    """A self-hosted runner is the VPS, anything else GitHub's; a job no
    runner took (skipped, waiting) has none."""
    if not job.get("runner_name"):
        return None
    labels = [str(label) for label in job.get("labels") or []]
    return "vps" if "self-hosted" in labels else "github"


def _job_state(job: dict[str, Any]) -> str:
    """waiting | running | how it ended (success, failure, skipped…)."""
    status = str(job.get("status") or "")
    if status in _WAITING_JOB:
        return "waiting"
    if status == "in_progress":
        return "running"
    return str(job.get("conclusion") or status)


def _run_jobs(
    runner: Runner,
    repo: str,
    run_id: int,
    fetched: Fetched,
    memo: Memo,
    attempts: Attempts,
    live: frozenset[int] = frozenset(),
) -> list[dict[str, Any]] | None:
    """A run's jobs: asked this snapshot already, or kept once that attempt
    finished. A run whose attempt the listing does not give (older than
    its newest hundred: a rerun keeps its creation date) is never served
    from the memo while its PR's CI runs (`live`): it may be a rerun."""
    if run_id in fetched:
        return fetched[run_id]
    attempt = attempts.get(run_id)
    key = (run_id, attempt)
    if attempt is None and run_id in live:
        memo.pop(key, None)
    elif key in memo:  # seen this snapshot: kept
        fetched[run_id] = memo[key]
        return memo[key]
    jobs = _jobs(runner, repo, run_id)
    if jobs is not None:
        fetched[run_id] = jobs
        over = all(str(j.get("status")) == "completed" for j in jobs)
        # GitHub may list the new attempt before its jobs: never kept as it
        latest = attempt is None or max(map(_attempt, jobs), default=1) == attempt
        if over and latest and not (attempt is None and run_id in live):
            memo[key] = jobs
    return jobs


def _attempt(job: dict[str, Any]) -> int:
    value = job.get("run_attempt")
    return value if isinstance(value, int) else 1


def _pr_jobs(
    runner: Runner,
    repo: str,
    run_ids: list[int],
    fetched: Fetched,
    memo: Memo,
    attempts: Attempts,
    live: frozenset[int] = frozenset(),
) -> list[dict[str, Any]] | None:
    """The jobs of a PR's runs, as GitHub lists them (#859): their state,
    their machine, their times, and whether they belong to their run's
    current attempt (#2079: a job `--failed` did not rerun stays the first
    attempt's); None when unreadable, none without a run."""
    rows = []
    for run_id in reversed(run_ids):  # oldest workflow first, as GitHub does
        jobs = _run_jobs(runner, repo, run_id, fetched, memo, attempts, live)
        if jobs is None:
            return None
        current = max((_attempt(j) for j in jobs), default=1)
        active = any(str(j.get("status")) != "completed" for j in jobs)
        rows += [
            {
                "name": str(job.get("name") or ""),
                "state": _job_state(job),
                "machine": machine_of(job),
                "runner": job.get("runner_name") or None,
                "started_at": epoch(job.get("started_at")),
                "completed_at": epoch(job.get("completed_at")),
                "created_at": epoch(job.get("created_at")),
                "attempt": _attempt(job),
                # what `ci_since` reads (#2079); the page reads none of them
                "current": _attempt(job) == current,
                "active_run": active,
                "url": str(job.get("html_url") or ""),
            }
            for job in jobs
        ]
    return rows


def ci_since(
    state: str, jobs: Sequence[dict[str, Any]], checks_since: int | None
) -> int | None:
    """Since when a PR's CI waits or runs (#2079), from its jobs' rows:
    queued, since its oldest job waiting for a runner was created; running,
    when every run still going is a rerun, since the first job of their
    attempts after the first; else the first start of its checks."""
    current = [j for j in jobs if j["current"]]
    if state == "queued":
        waiting = [j["created_at"] for j in current if j["state"] == "waiting"]
        return min((at for at in waiting if at is not None), default=checks_since)
    going = [j for j in current if j["active_run"]]
    if state == "running" and going and all(j["attempt"] > 1 for j in going):
        times = [j["started_at"] or j["created_at"] for j in going]
        return min((at for at in times if at is not None), default=checks_since)
    return checks_since


def _pr_of(run: dict[str, Any], by_branch: dict[str, int]) -> int | None:
    numbers = [p.get("number") for p in run.get("pull_requests") or []]
    if numbers and isinstance(numbers[0], int):
        return numbers[0]
    return by_branch.get(str(run.get("head_branch") or ""))


def _runner_names(names: str) -> list[str]:
    return [n.strip() for n in names.split(",") if n.strip()]


def build_control(
    runner: Runner,
    repo: str,
    conventions: Conventions,
    runner_names: str,
    memo: Memo | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    memo = {} if memo is None else memo
    pulls = guarded(lambda: _pull_requests(runner, repo, conventions))
    by_branch = {p["branch"]: p["number"] for p in pulls or []}
    runs = latest_runs(runner, repo)
    fetched: Fetched = {}  # jobs by run, asked once per snapshot
    attempts: Attempts = {
        int(r["id"]): r["run_attempt"]
        for r in runs or []
        if isinstance(r.get("id"), int) and isinstance(r.get("run_attempt"), int)
    }
    active = (
        None
        if runs is None
        else guarded(lambda: _active_jobs(runner, repo, runs, by_branch, fetched))
    )
    waiting_jobs, running_jobs = active if active is not None else (None, {})
    for pull in pulls or []:
        run_ids: list[int] = pull.pop("ci_run_ids")
        live = frozenset(run_ids if pull["ci"] in ("running", "queued") else ())
        jobs = guarded(
            partial(_pr_jobs, runner, repo, run_ids, fetched, memo, attempts, live)
        )
        pull["ci_jobs"] = jobs
        if jobs is not None and pull["ci"] in ("running", "queued"):
            pull["ci_since"] = ci_since(pull["ci"], jobs, pull["ci_since"])
    recent = (
        None
        if runs is None
        else guarded(
            lambda: _recent(runner, repo, runs, by_branch, fetched, memo, now, attempts)
        )
    )
    for stale in [key for key in memo if key[0] not in fetched]:
        del memo[stale]  # kept while a post shows it
    return {
        "pull_requests": pulls,
        "waiting_jobs": waiting_jobs,
        "runners": guarded(
            lambda: _runners(
                runner, repo, runner_names, running_jobs, waiting_jobs is not None
            )
        ),
        "recent_runs": recent,
    }


Jobs = tuple[list[dict[str, Any]] | None, dict[str, dict[str, Any]]]
Fetched = dict[int, list[dict[str, Any]]]


def _active_jobs(
    runner: Runner,
    repo: str,
    runs: list[dict[str, Any]],
    by_branch: dict[str, int],
    fetched: Fetched,
) -> Jobs:
    """The jobs waiting for a runner (None: unknown), and the running job of
    each busy runner, from the runs not finished yet."""
    waiting_jobs: list[dict[str, Any]] = []
    running_jobs: dict[str, dict[str, Any]] = {}
    active = [r for r in runs if str(r.get("status")) in _ACTIVE_STATUSES]
    for run in active:
        jobs = _jobs(runner, repo, int(run["id"]))
        if jobs is None:
            return None, running_jobs
        fetched[int(run["id"])] = jobs
        where = {
            "run_id": int(run["id"]),
            "pr": _pr_of(run, by_branch),
            "branch": str(run.get("head_branch") or ""),
        }
        for job in jobs:
            status = str(job.get("status") or "")
            if status in _WAITING_JOB:
                waiting_jobs.append(
                    {
                        "job": str(job.get("name") or ""),
                        "url": str(job.get("html_url") or ""),
                        "since": epoch(job.get("created_at")),
                        **where,
                    }
                )
            elif status == "in_progress" and job.get("runner_name"):
                running_jobs[str(job["runner_name"])] = {
                    "name": str(job.get("name") or ""),
                    "url": str(job.get("html_url") or ""),
                    "started_at": epoch(job.get("started_at")),
                    **where,
                }

    return waiting_jobs, running_jobs


def _runners(
    runner: Runner,
    repo: str,
    names: str,
    running_jobs: dict[str, dict[str, Any]],
    jobs_known: bool,
) -> list[dict[str, Any]] | None:
    payload = read_json(runner, ["gh", "api", f"repos/{repo}/actions/runners"])
    if not isinstance(payload, dict) or not isinstance(payload.get("runners"), list):
        return None
    known = {str(r.get("name")): r for r in payload["runners"]}
    shown = []
    for name in _runner_names(names):
        found = known.get(name)
        busy = bool(found.get("busy")) if found else False
        shown.append(
            {
                "name": name,
                "status": str(found.get("status")) if found else "absent",
                "busy": busy,
                # A busy runner whose job could not be read: its job is unknown.
                "job": running_jobs.get(name) if jobs_known or not busy else None,
            }
        )
    return shown


def _recent(
    runner: Runner,
    repo: str,
    runs: list[dict[str, Any]],
    by_branch: dict[str, int],
    fetched: Fetched,
    memo: Memo,
    now: float | None,
    attempts: Attempts,
) -> list[dict[str, Any]]:
    """The last finished runs that did some work, last finished first; none
    finished more than `RECENT_WINDOW` ago."""
    since = None if now is None else now - RECENT_WINDOW
    done = [
        (at, r)
        for r in runs
        if r.get("status") == "completed"
        and str(r.get("conclusion")) not in _IDLE_CONCLUSIONS
        and (at := epoch(r.get("updated_at"))) is not None
        and (since is None or at >= since)
    ]
    done.sort(key=lambda pair: pair[0], reverse=True)
    shown = [r for _, r in done[:RECENT_RUNS]]
    recent = []
    for run in shown:
        run_id = int(run["id"])
        jobs = _run_jobs(runner, repo, run_id, fetched, memo, attempts)
        started, finished = (
            epoch(run.get("run_started_at")),
            epoch(run.get("updated_at")),
        )
        recent.append(
            {
                "id": run_id,
                "workflow": str(run.get("name") or ""),
                "title": str(run.get("display_title") or ""),
                "url": str(run.get("html_url") or ""),
                "conclusion": str(run.get("conclusion") or ""),
                "success": run.get("conclusion") == "success",
                "pr": _pr_of(run, by_branch),
                "branch": str(run.get("head_branch") or ""),
                "started_at": started,
                "finished_at": finished,
                "duration": finished - started
                if started is not None and finished is not None
                else None,
                "runners": None
                if jobs is None
                else sorted(
                    {str(j["runner_name"]) for j in jobs if j.get("runner_name")}
                ),
            }
        )
    return recent
