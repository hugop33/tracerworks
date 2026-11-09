"""What feeds the history base (#836, #837): readings taken by `serve`'s cycle.

`Recorder.tick` is asked every second by `serve`, whether anyone watches the
dashboard or not; each source says when it is due:

- every minute: the VPS (memory, CPU, disk), the Claude quota as the workers'
  status line last saw it, the issues waiting for the operator, the finished
  CI runs and their jobs, the merged pull requests (a deployment is a
  successful run of the deployment workflow);
- every 5 minutes: the VPS load the health log's 5-minute readings say
  (#2279: the CPU over the period, and each consumer's share of it);
- every 15 minutes: the month's GitHub Actions minutes (read at most that
  often by `quotas.actions_quota` anyway).

A chantier's usage is written by `tracerworks clean` when it removes its
worktree (`record_chantier`).

A source that cannot be read writes NOTHING that turn — never a zero, never
an old value passed off as new — and says why once, on `err` (the service's
journal). A finished CI run never changes: it is read once, with its jobs,
and a run whose jobs cannot be read waits for the next turn, never half
written.

The first start imports, once, what came before (`backfill`): the CI runs, the
merged pull requests and the usage comments of the chantiers since
`[history] since` — without it, since the project's start (#851) — and the
VPS health journal. Each part is marked done in
the base only when it went through whole; a part that failed is tried again
an hour later. Every row is `INSERT OR IGNORE`: importing twice adds nothing.
"""

from __future__ import annotations

import math
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from tracerworks import (
    budget,
    conflicts,
    merge_queue,
    oom,
    store,
    texts,
    worktree_watch,
)
from tracerworks.alerts import (
    AlertError,
    Send,
    conditions,
    muted,
    send_pending,
    step,
    telegram,
)
from tracerworks.backup import BackupError, due, take
from tracerworks.cleanup import UNKNOWN, USAGE_MARKER
from tracerworks.control import epoch, machine_of, read_json
from tracerworks.conventions import Conventions
from tracerworks.disk import filesystem
from tracerworks.frontier import BodyRules
from tracerworks.github import job_log
from tracerworks.observe import available_memory_mb
from tracerworks.process import Runner
from tracerworks.pytest_log import failed_tests
from tracerworks.quotas import (
    WINDOWS,
    Fetch,
    actions_quota,
    claude_quota,
    fetch_billing,
    included_minutes,
)
from tracerworks.settings import Settings
from tracerworks.snapshot import memory_total_mb
from tracerworks.state_file import State, StateFileError
from tracerworks.state_file import load as load_state
from tracerworks.strays import Stray
from tracerworks.usage import ChantierUsage
from tracerworks.waits import specs_recorded, stale_parked

MINUTE = 60.0
ACTIONS_EVERY = 15 * 60.0
LOAD_EVERY = 5 * 60.0
BACKFILL_RETRY = 3600.0
LATEST = 100  # the newest runs and merged pull requests, read each minute
BACKFILL_LIMIT = 10_000  # merged PRs listed by the backfill, from the project's start
MAX_RUN_PAGES = 1000  # 100,000 runs: beyond, the backfill is refused, never cut
#: The heading `usage_comment` writes: what the search finds (the marker is
#: an HTML comment, not indexed).
USAGE_HEADING = "Consommation du chantier"

Row = dict[str, object]
CpuReading = tuple[float, float]  # (when, percent busy over the minute before)


# --- the machine ----------------------------------------------------------------------


def cpu_times(stat: Path) -> tuple[int, int] | None:
    """(busy, total) jiffies of all CPUs since boot, from `/proc/stat`."""
    try:
        first = stat.read_text().splitlines()[0].split()
    except (OSError, IndexError):
        return None
    if first[:1] != ["cpu"] or len(first) < 5:
        return None
    try:
        values = [int(v) for v in first[1:]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return sum(values) - idle, sum(values)


def cpu_percent(
    before: tuple[int, int] | None, now: tuple[int, int] | None
) -> float | None:
    """The CPU busy between two readings; None without two, or a counter reset."""
    if before is None or now is None:
        return None
    busy, total = now[0] - before[0], now[1] - before[1]
    if total <= 0 or busy < 0:
        return None
    return round(busy / total * 100, 1)


def vps_row(
    runner: Runner, settings: Settings, cpu: float | None, now: float
) -> Row | None:
    """The minute's reading; None when nothing at all could be read."""
    disk = filesystem(runner, settings.worktree_root)
    row: Row = {
        "at": int(now),
        "source": "live",
        "memory_available_mb": available_memory_mb(settings.meminfo),
        "memory_total_mb": memory_total_mb(settings.meminfo),
        "cpu_percent": cpu,
        "disk_used_percent": disk.used_percent if disk else None,
        "disk_available_kb": disk.available_kb if disk else None,
        "disk_total_kb": disk.total_kb if disk else None,
    }
    if all(v is None for k, v in row.items() if k not in ("at", "source")):
        return None
    return row


_HEALTH = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
_MEM = re.compile(r"\bmem_dispo=(\d+)MB\b")
_DISK = re.compile(r"\bdisque=(\d+(?:\.\d+)?)%")


def start_of(since: date | None) -> int:
    """`[history] since` as an epoch; none: 0, the project's whole history."""
    if since is None:
        return 0
    return int(datetime.combine(since, datetime.min.time(), UTC).timestamp())


def health_rows(text: str, since: date | None) -> list[Row]:
    """The VPS health journal: one line every 15 min (30 at first), in the
    machine's local time, as the cron script writes it. A line that names
    neither the memory nor the disk is skipped, not zeroed."""
    start = start_of(since)
    rows = []
    for line in text.splitlines():
        match = _HEALTH.match(line.strip())
        if not match:
            continue
        try:
            at = int(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            continue
        memory, disk = _MEM.search(match.group(2)), _DISK.search(match.group(2))
        if at < start or (memory is None and disk is None):
            continue
        rows.append(
            {
                "at": at,
                "source": "health_log",
                "memory_available_mb": int(memory.group(1)) if memory else None,
                "disk_used_percent": float(disk.group(1)) if disk else None,
            }
        )
    return rows


#: The load reading's fields (`scripts/vps-load.py`), each its column.
_LOAD = {
    "cpu": "cpu_percent",
    "iowait": "iowait_percent",
    "load1": "load1",
    "load5": "load5",
    "load15": "load15",
    "cores": "cores",
    "mem_dispo": "memory_available_mb",
    "cpu_workers": "cpu_workers",
    "cpu_ci": "cpu_ci",
    "cpu_factory": "cpu_factory",
    "cpu_containers": "cpu_containers",
    "cpu_rest": "cpu_rest",
}
_INTEGERS = {"cores", "memory_available_mb"}
_FIGURE = re.compile(r"^(\d+(?:\.\d+)?)(?:%|MB)?$")


def load_rows(text: str, since: date | None, after: int = 0) -> list[Row]:
    """The 5-minute load readings of the health journal (#2279), in the
    machine's local time. A line is one when it names its cores and its
    load; a figure it says `?` is NULL, never a zero. Only those past
    `after` (the last one already in the base). A last line without its
    newline is being written: read next time, never kept half."""
    start = start_of(since)
    rows = []
    lines = text.splitlines()
    if not text.endswith("\n"):
        lines = lines[:-1]
    for line in lines:
        match = _HEALTH.match(line.strip())
        if not match:
            continue
        said = dict(
            part.split("=", 1) for part in match.group(2).split() if "=" in part
        )
        if "cores" not in said or "load1" not in said:
            continue
        try:
            at = int(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            continue
        if at < start or at <= after:
            continue
        row: Row = {"at": at}
        for field, column in _LOAD.items():
            figure = _FIGURE.match(said.get(field, ""))
            value = float(figure.group(1)) if figure else None
            row[column] = (
                int(value) if value is not None and column in _INTEGERS else value
            )
        if any(v is not None for k, v in row.items() if k != "at"):
            rows.append(row)
    return rows


# --- the quotas -----------------------------------------------------------------------


def claude_rows(quota: dict[str, Any]) -> list[Row]:
    """Each window the status line saw, keyed by WHEN it saw it: the same
    reading read again a minute later adds nothing."""
    return [
        {
            "period": name,
            "seen_at": int(window["seen_at"]),
            "used_percent": float(window["used_percent"]),
            "resets_at": int(window["resets_at"]),
        }
        for name in WINDOWS
        if (window := quota.get(name)) is not None
    ]


def actions_row(read: dict[str, Any], account: str) -> Row | None:
    if read.get("error") is not None or read.get("used_minutes") is None:
        return None
    at = datetime.fromtimestamp(float(read["read_at"]), UTC)
    return {
        "read_at": int(read["read_at"]),
        "account": account,
        "year": at.year,
        "month": at.month,
        "used_minutes": float(read["used_minutes"]),
        "included_minutes": int(read["included_minutes"]),
    }


# --- CI -------------------------------------------------------------------------------


def run_row(project: str, run: dict[str, Any]) -> Row:
    pulls = [p.get("number") for p in run.get("pull_requests") or []]
    return {
        "project": project,
        "run_id": int(run["id"]),
        "attempt": int(run.get("run_attempt") or 1),
        "workflow": str(run.get("name") or ""),
        "workflow_path": str(run.get("path") or ""),
        "event": str(run.get("event") or ""),
        "branch": str(run.get("head_branch") or ""),
        "head_sha": str(run.get("head_sha") or ""),
        "pr": pulls[0] if pulls and isinstance(pulls[0], int) else None,
        "conclusion": str(run.get("conclusion") or ""),
        "created_at": epoch(run.get("created_at")),
        "started_at": epoch(run.get("run_started_at")),
        "finished_at": epoch(run.get("updated_at")),
        "title": str(run.get("display_title") or ""),
        "url": str(run.get("html_url") or ""),
    }


def _span(start: int | None, end: int | None) -> int | None:
    return (
        end - start if start is not None and end is not None and end >= start else None
    )


def job_row(project: str, job: dict[str, Any]) -> Row:
    """A job, with the machine that ran it: a self-hosted runner is the VPS,
    anything else GitHub's; a job no runner took (skipped) has none. GitHub
    bills its own runners' minutes per job, each rounded up."""
    machine = machine_of(job)
    created, started, completed = (
        epoch(job.get(k)) for k in ("created_at", "started_at", "completed_at")
    )
    duration = _span(started, completed)
    return {
        "project": project,
        "job_id": int(job["id"]),
        "run_id": int(job["run_id"]),
        "attempt": int(job.get("run_attempt") or 1),
        "name": str(job.get("name") or ""),
        "runner_name": str(job.get("runner_name") or "") or None,
        "machine": machine,
        "conclusion": str(job.get("conclusion") or ""),
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
        "wait_s": _span(created, started) if machine else None,
        "duration_s": duration,
        # GitHub's machines only; unknown when the job's times are
        "billed_minutes": 0
        if machine != "github"
        else None
        if duration is None
        else math.ceil(duration / 60),
        "url": str(job.get("html_url") or ""),
    }


def deployment_row(project: str, run: Row, deploy_workflow: str) -> Row | None:
    path = str(run["workflow_path"])
    if run["conclusion"] != "success" or run["finished_at"] is None:
        return None
    if path != deploy_workflow and not path.endswith(f"/{deploy_workflow}"):
        return None
    return {
        "project": project,
        "run_id": run["run_id"],
        "head_sha": run["head_sha"],
        "at": run["finished_at"],
        "url": run["url"],
    }


def _jobs(runner: Runner, repo: str, run_id: int, attempt: int) -> list[Any] | None:
    """Every job of one attempt of a run; None when any page is unreadable."""
    jobs: list[Any] = []
    for page in range(1, 11):
        payload = read_json(
            runner,
            [
                "gh", "api",
                f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                f"?per_page=100&page={page}",
            ],
        )  # fmt: skip
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            return None
        jobs += payload["jobs"]
        total = payload.get("total_count")
        if not isinstance(total, int) or len(jobs) >= total or not payload["jobs"]:
            return jobs
    return None


def _attempt(
    runner: Runner, repo: str, run_id: int, attempt: int
) -> dict[str, Any] | None:
    payload = read_json(
        runner, ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"]
    )
    return payload if isinstance(payload, dict) and "id" in payload else None


def record_runs(
    db: sqlite3.Connection,
    runner: Runner,
    settings: Settings,
    runs: list[dict[str, Any]],
) -> tuple[int, int]:
    """Write every finished attempt of these runs not in the base yet, each
    with its jobs, in one transaction; (attempts written, attempts left for
    later because their jobs could not be read)."""
    project = settings.github_repo
    known = {
        (run_id, attempt)
        for run_id, attempt in db.execute(
            "SELECT run_id, attempt FROM ci_run WHERE project = ?", (project,)
        )
    }
    written = missed = 0
    for run in runs:
        if run.get("status") != "completed":
            continue
        run_id, last = int(run["id"]), int(run.get("run_attempt") or 1)
        for attempt in range(1, last + 1):
            if (run_id, attempt) in known:
                continue
            seen = (
                run if attempt == last else _attempt(runner, project, run_id, attempt)
            )
            jobs = None if seen is None else _jobs(runner, project, run_id, attempt)
            if seen is None or jobs is None:
                missed += 1
                continue
            row = run_row(project, seen)
            deployment = deployment_row(project, row, settings.deploy_workflow)
            with transaction(db):
                store.insert(db, "ci_run", [row])
                store.insert(db, "ci_job", [job_row(project, j) for j in jobs])
                if deployment is not None:
                    store.insert(db, "deployment", [deployment])
            known.add((run_id, attempt))
            written += 1
    return written, missed


# --- pull requests, the operator, chantiers -------------------------------------------


def merged_rows(
    project: str, payload: list[dict[str, Any]], conventions: Conventions
) -> list[Row]:
    rows = []
    for pull in payload:
        merged = epoch(pull.get("mergedAt"))
        if merged is None:
            continue
        branch = str(pull.get("headRefName") or "")
        parsed = conventions.parse_branch(branch)
        closing = pull.get("closingIssuesReferences")
        rows.append(
            {
                "project": project,
                "number": int(pull["number"]),
                "title": str(pull.get("title") or ""),
                "branch": branch,
                "spec": parsed[0] if parsed else None,
                "created_at": epoch(pull.get("createdAt")),
                "merged_at": merged,
                "url": str(pull.get("url") or ""),
                "changed_files": _count(pull.get("changedFiles")),
                "additions": _count(pull.get("additions")),
                "deletions": _count(pull.get("deletions")),
                "closed_issues": len(closing) if isinstance(closing, list) else None,
            }
        )
    return rows


def _count(value: object) -> int | None:
    """A size GitHub gave, or None: unknown, never zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _merged(runner: Runner, repo: str, limit: int) -> list[dict[str, Any]] | None:
    payload = read_json(
        runner,
        [
            "gh", "pr", "list", "-R", repo, "--state", "merged",
            "--limit", str(limit),
            "--json", "number,title,headRefName,createdAt,mergedAt,url,"
            "additions,deletions,changedFiles,closingIssuesReferences",
        ],
    )  # fmt: skip
    return payload if isinstance(payload, list) else None


def record_needs_human(
    db: sqlite3.Connection, project: str, asking: set[int], now: float
) -> tuple[int, int]:
    """Open an episode for each issue newly carrying the label, close the
    episode of each that lost it; (opened, closed). Known to the minute."""
    open_: dict[int, int] = dict(
        db.execute(
            "SELECT issue, started_at FROM needs_human"
            " WHERE project = ? AND ended_at IS NULL",
            (project,),
        ).fetchall()
    )
    with transaction(db):
        for issue in set(open_) - asking:
            db.execute(
                "UPDATE needs_human SET ended_at = ?"
                " WHERE project = ? AND issue = ? AND started_at = ?",
                (int(now), project, issue, open_[issue]),
            )
        store.insert(
            db,
            "needs_human",
            [
                {"project": project, "issue": i, "started_at": int(now)}
                for i in sorted(asking - set(open_))
            ],
        )
    return len(asking - set(open_)), len(set(open_) - asking)


def _asking(
    runner: Runner, repo: str, label: str, rules: BodyRules
) -> dict[int, int | None] | None:
    """The open issues carrying the label, each with the spec its body names
    (None for a spec itself)."""
    payload = read_json(
        runner,
        [
            "gh", "issue", "list", "-R", repo, "--state", "open",
            "--label", label, "--limit", "500", "--json", "number,body",
        ],
    )  # fmt: skip
    if not isinstance(payload, list):
        return None
    return {int(i["number"]): rules.parent(i.get("body") or "") for i in payload}


def blocked_issues(asking: dict[int, int | None], resting: set[int]) -> set[int]:
    """The issues among `asking` whose chantier's worker is at rest (#853):
    the spec itself, or a ticket naming it."""
    return {i for i, spec in asking.items() if i in resting or spec in resting}


def usage_row(project: str, spec: int, usage: ChantierUsage, now: float) -> Row | None:
    total = usage.total()
    span = usage.span()
    model, effort = usage.seen()
    if total.messages == 0:
        return None
    return {
        "project": project,
        "spec": spec,
        "finished_at": int(now),
        "duration_s": int((span[1] - span[0]).total_seconds()) if span else None,
        "tokens_total": total.total,
        "tokens_input": total.input,
        "tokens_cache_write": total.cache_write,
        "tokens_cache_read": total.cache_read,
        "tokens_output": total.output,
        "messages": total.messages,
        "source": "clean",
        "model": model,
        "effort": effort,
    }


_CHANTIER_ROW = re.compile(r"^\|\s*\*\*chantier\*\*\s*\|(.*)\|\s*$", re.MULTILINE)
_DURATION = re.compile(r"^Durée : (\d+) h (\d+)", re.MULTILINE)
_LEVEL = re.compile(r"^Modèle · effort : (\S+) · (\S+) ", re.MULTILINE)


def comment_row(project: str, spec: int, body: str, posted_at: int) -> Row | None:
    """The usage comment `clean` posts on a spec, read back; None when it is
    not one, or its total line cannot be read whole."""
    if USAGE_MARKER not in body:
        return None
    line = _CHANTIER_ROW.search(body)
    if line is None:
        return None
    cells = [re.sub(r"\D", "", c) for c in line.group(1).split("|")]
    if len(cells) != 6 or not all(cells):
        return None
    total, fresh, write, read, output, messages = (int(c) for c in cells)
    duration = _DURATION.search(body)
    level = _LEVEL.search(body)
    model, effort = (
        (None, None)
        if level is None
        else tuple(None if v == UNKNOWN else v for v in level.groups())
    )
    return {
        "project": project,
        "spec": spec,
        "finished_at": posted_at,
        "duration_s": (int(duration.group(1)) * 60 + int(duration.group(2))) * 60
        if duration
        else None,
        "tokens_total": total,
        "tokens_input": fresh,
        "tokens_cache_write": write,
        "tokens_cache_read": read,
        "tokens_output": output,
        "messages": messages,
        "source": "comment",
        "model": model,
        "effort": effort,
    }


def record_chantier(
    settings: Settings, spec: int, usage: ChantierUsage, now: float
) -> bool:
    """`clean`'s record of a finished chantier; whether a row was written."""
    row = usage_row(settings.github_repo, spec, usage, now)
    if row is None:
        return False
    with closing(store.connect(settings.history_db)) as db:
        return store.insert(db, "chantier", [row]) > 0


# --- what the Qualité tab needs (#840) ------------------------------------------------

#: How many logs, commits and specs one minute reads at most: the first turns
#: after the upgrade catch up on September in a few minutes, never in one go.
BATCH = 30
#: GitHub's answer for a log that no longer exists (logs expire).
_GONE = ("HTTP 404", "HTTP 410")


def _gone(result: Any) -> bool:
    return any(code in (result.stderr or "") + (result.stdout or "") for code in _GONE)


def record_test_failures(
    db: sqlite3.Connection, runner: Runner, project: str, now: float, limit: int
) -> int:
    """Read the log of each failed job not read yet, newest first; a log
    GitHub no longer has is marked read with no count (unknown, not zero)."""
    jobs = db.execute(
        "SELECT j.job_id FROM ci_job j LEFT JOIN ci_log l"
        " ON l.project = j.project AND l.job_id = j.job_id"
        " WHERE j.project = ? AND j.conclusion = 'failure' AND l.job_id IS NULL"
        " ORDER BY j.completed_at DESC LIMIT ?",
        (project, limit),
    ).fetchall()
    read = 0
    for (job_id,) in jobs:
        result = job_log(runner, project, job_id)
        if not result.ok and not _gone(result):
            continue  # tried again next minute
        tests = failed_tests(result.stdout) if result.ok else None
        with transaction(db):
            store.insert(
                db,
                "ci_test_failure",
                [
                    {"project": project, "job_id": job_id, "test": t}
                    for t in tests or ()
                ],
            )
            store.insert(
                db,
                "ci_log",
                [
                    {
                        "project": project,
                        "job_id": job_id,
                        "read_at": int(now),
                        "tests": None if tests is None else len(tests),
                    }
                ],
            )
        read += 1
    return read


def record_branch_files(
    db: sqlite3.Connection, runner: Runner, settings: Settings, now: float, limit: int
) -> int:
    """The files the pull request of a failing branch changes: whether a
    failure touched its test's module. A branch without a pull request (or
    the base branch) is marked read with no count: unknown, not « none »."""
    project = settings.github_repo
    branches = db.execute(
        "SELECT DISTINCT r.branch FROM ci_test_failure f"
        " JOIN ci_job j USING (project, job_id)"
        " JOIN ci_run r ON r.project = j.project AND r.run_id = j.run_id"
        " AND r.attempt = j.attempt"
        " LEFT JOIN branch_read b ON b.project = r.project AND b.branch = r.branch"
        " WHERE f.project = ? AND b.branch IS NULL LIMIT ?",
        (project, limit),
    ).fetchall()
    read = 0
    for (branch,) in branches:
        pull: dict[str, Any] | None = None
        if branch != settings.base_branch:
            payload = read_json(
                runner,
                [
                    "gh", "pr", "list", "-R", project, "--head", branch,
                    "--state", "all", "--limit", "1", "--json", "number,files",
                ],
            )  # fmt: skip
            if not isinstance(payload, list):
                continue  # tried again next minute
            pull = payload[0] if payload and isinstance(payload[0], dict) else None
        files = (
            [str(f["path"]) for f in pull.get("files") or [] if isinstance(f, dict)]
            if pull
            else None
        )
        with transaction(db):
            store.insert(
                db,
                "branch_file",
                [
                    {"project": project, "branch": branch, "path": f}
                    for f in files or ()
                ],
            )
            store.insert(
                db,
                "branch_read",
                [
                    {
                        "project": project,
                        "branch": branch,
                        "read_at": int(now),
                        "pr": pull.get("number") if pull else None,
                        "files": None if files is None else len(files),
                    }
                ],
            )
        read += 1
    return read


def record_specs(
    db: sqlite3.Connection, runner: Runner, project: str, limit: int
) -> int:
    """When each delivered chantier's spec was published, and its title."""
    missing = db.execute(
        "SELECT n FROM (SELECT spec AS n FROM pull_merged WHERE project = ?"
        " AND spec IS NOT NULL UNION SELECT spec FROM chantier WHERE project = ?)"
        " WHERE n NOT IN (SELECT number FROM spec WHERE project = ?)"
        " ORDER BY n DESC LIMIT ?",
        (project, project, project, limit),
    ).fetchall()
    rows = []
    for (number,) in missing:
        issue = read_json(runner, ["gh", "api", f"repos/{project}/issues/{number}"])
        created = epoch(issue.get("created_at")) if isinstance(issue, dict) else None
        if not isinstance(issue, dict) or created is None:
            continue
        rows.append(
            {
                "number": number,
                "project": project,
                "title": str(issue.get("title") or ""),
                "created_at": created,
            }
        )
    return store.insert(db, "spec", rows)


# --- the base -------------------------------------------------------------------------


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _in_a_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="history-backfill", daemon=True).start()


class Recorder:
    def __init__(
        self,
        settings: Settings,
        runner: Runner,
        clock: Callable[[], float],
        stat: Path = Path("/proc/stat"),
        billing: Fetch = fetch_billing,
        err: IO[str] | None = None,
        spawn: Callable[[Callable[[], None]], None] = _in_a_thread,
        sender: Callable[[], Send] | None = None,
        resting: Callable[[], set[int] | None] | None = None,
        asking: Callable[[], dict[int, str] | None] | None = None,
        strays: Callable[[], list[Stray] | None] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._clock = clock
        self._stat = stat
        self._billing = billing
        self._err = err if err is not None else sys.stderr
        self._spawn = spawn
        self._sender = sender or (
            lambda: telegram(settings.alerts_token_file, settings.alerts_chat_id)
        )
        # the specs whose worker is at rest in its window; None: unknown
        self._resting = resting or (lambda: None)
        # each blocked issue, since when seen blocked (a restart of serve
        # starts the count again: an alert late, never early)
        self._blocked_since: dict[int, float] = {}
        self._blocked: dict[int, float] | None = None  # this turn's, for the alerts
        # the specs whose worker is stopped on a permission prompt (#984),
        # what it asks; None: unknown — and since when each was seen so
        self._asking = asking or (lambda: None)
        self._asking_since: dict[int, float] = {}
        # the processes holding a core outside the CI and the workers; None: unknown
        self._strays = strays or (lambda: None)
        self._cpu: tuple[int, int] | None = None
        self.cpu: CpuReading | None = None  # the last one, for the dashboard
        self._minute_at: float | None = None
        self._actions_at: float | None = None
        self._load_at: float | None = None
        self._actions_read: dict[str, Any] | None = None  # this turn's, for the net
        self._backfill_at: float | None = None
        self._backfilling = False
        self._backup_tried: float | None = None
        self._lock = threading.Lock()
        self._said: dict[str, str | None] = {}

    # the cycle ----------------------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        self._backup(now)
        if self._minute_at is None or now - self._minute_at >= MINUTE:
            self._minute_at = now
            try:
                self.sample(now)
                self._say(texts.SOURCE_BASE, 0)
            except Exception as exc:  # the base itself: said, tried next minute
                self._say(texts.SOURCE_BASE, texts.ERROR.format(error=repr(exc)))
        with self._lock:
            backfill = not self._backfilling and (
                self._backfill_at is None or now - self._backfill_at >= BACKFILL_RETRY
            )
            if backfill:
                self._backfilling, self._backfill_at = True, now
        if backfill:
            self._spawn(self._backfill_then_release)

    def _backup(self, now: float) -> None:
        """The nightly copy (#842), at the first turn past its hour; a failure
        is said, and tried again at the next turn's hour check (a minute)."""
        folder = self._settings.backup_dir
        if self._backup_tried is not None and now - self._backup_tried < MINUTE:
            return
        self._backup_tried = now
        if not due(folder, now):
            return
        try:
            take(self._settings.history_db, folder, now)
            self._say(texts.SOURCE_BACKUP, 0)
        except BackupError as exc:
            self._say(texts.SOURCE_BACKUP, texts.ERROR.format(error=exc))

    def keep_recording(self, stop: threading.Event, every: float = 1.0) -> None:
        self.tick()
        while not stop.wait(every):
            self.tick()

    def sample(self, now: float) -> dict[str, int | str]:
        """One turn of every due source: rows written, or why not."""
        report: dict[str, int | str] = {}
        with closing(store.connect(self._settings.history_db)) as db:
            sources: list[tuple[str, Callable[[], int | str]]] = [
                ("vps", lambda: self._vps(db, now)),
                ("claude_quota", lambda: self._claude(db, now)),
                ("needs_human", lambda: self._needs_human(db, now)),
                ("ci", lambda: self._ci(db)),
                ("pull_merged", lambda: self._pulls(db, LATEST)),
                ("tests", lambda: self._quality(db, now, record_test_failures)),
                ("branch_files", lambda: self._branches(db, now)),
                ("specs", lambda: record_specs(db, self._runner, self._project, BATCH)),
            ]
            if self._load_at is None or now - self._load_at >= LOAD_EVERY:
                self._load_at = now
                sources.append(("vps_load", lambda: self._load(db)))
            if self._actions_at is None or now - self._actions_at >= ACTIONS_EVERY:
                self._actions_at = now
                sources.append(("actions", lambda: self._actions(db, now)))
                sources.append(("ci_budget", lambda: self._budget(now)))
                sources.append(("oom", lambda: self._oom(db, now)))
            # last: the alerts read what this turn just wrote
            sources.append(("alerts", lambda: self._alerts(db, now)))
            for name, read in sources:
                try:
                    report[name] = read()
                except Exception as exc:  # one source down, the others stand
                    report[name] = texts.ERROR.format(error=repr(exc))
                self._say(name, report[name])
        return report

    def _say(self, source: str, outcome: int | str) -> None:
        """A failure once when it starts, and once when it ends."""
        failing = outcome if isinstance(outcome, str) else None
        with self._lock:  # the history and the backfill threads both say
            if self._said.get(source) == failing:
                return
            self._said[source] = failing
        text = failing or texts.READABLE_AGAIN
        said = texts.HISTORY_SAID.format(source=source, text=text)
        print(said, file=self._err, flush=True)

    def _state(self) -> State:
        """The operator's state file, read again; one it cannot trust is said
        (`state` in the journal, once) and its defaults hold: every alert on
        and sent, the worker token's included, the project file's minutes."""
        try:
            state = load_state(self._settings.state_file)
        except (OSError, StateFileError) as exc:
            self._say("state", str(exc))
            return State()
        self._say("state", 0)
        return state

    # each source --------------------------------------------------------------------

    def _vps(self, db: sqlite3.Connection, now: float) -> int | str:
        times = cpu_times(self._stat)
        cpu, self._cpu = cpu_percent(self._cpu, times), times
        self.cpu = None if cpu is None else (now, cpu)
        row = vps_row(self._runner, self._settings, cpu, now)
        if row is None:
            return texts.VPS_UNREADABLE
        return store.insert(db, "vps_sample", [row])

    def _load(self, db: sqlite3.Connection) -> int | str:
        """The readings newer than the last one in the base: the journal is
        read whole (20 days, some 6,000 lines), written from where it left."""
        try:
            text = self._settings.health_log.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            return texts.HEALTH_LOG_UNREADABLE.format(error=exc)
        (latest,) = db.execute("SELECT coalesce(max(at), 0) FROM vps_load").fetchone()
        read = load_rows(text, self._settings.history_since, int(latest))
        return store.insert(db, "vps_load", read)

    def _oom(self, db: sqlite3.Connection, now: float) -> int | str:
        rows = oom.read(self._runner, now, self._settings.conventions.unit_prefix)
        if rows is None:
            return texts.OOM_UNREADABLE
        return store.insert(db, "oom_kill", rows)

    def _claude(self, db: sqlite3.Connection, now: float) -> int | str:
        read = claude_quota(self._settings.claude_quota_file, now)
        rows = claude_rows(read)
        if not rows and read["error"]:
            return str(read["error"])
        return store.insert(db, "claude_quota_sample", rows)

    def _actions(self, db: sqlite3.Connection, now: float) -> int | str:
        settings = self._settings
        account = settings.github_repo.split("/", 1)[0]
        read = actions_quota(
            settings.billing_token_file,
            account,
            # the Settings tab's (#2192), read again each time: the gauge, the
            # samples and the CI net follow it without a restart
            included_minutes(settings, self._state()),
            settings.cache_dir / "actions-quota.json",
            now,
            self._billing,
        )
        self._actions_read = read
        row = actions_row(read, account)
        if row is None:
            return str(read.get("error") or texts.ACTIONS_UNREADABLE)
        return store.insert(db, "actions_minutes_sample", [row])

    def _budget(self, now: float) -> int | str:
        """The GitHub minutes' net for `ci-github` CI (#1296), from the reading
        `_actions` just made: a switch said in the journal, 1; else 0."""
        read, self._actions_read = self._actions_read, None
        variable = self._settings.budget_variable
        if variable is None:
            return 0
        if read is None:
            return texts.ACTIONS_UNREADABLE
        said = budget.apply(
            self._runner, self._settings.github_repo, variable, read, now
        )
        if said is None:
            return 0
        print(texts.HISTORY_SAID.format(source="ci_budget", text=said),
              file=self._err, flush=True)  # fmt: skip
        return 1

    def _needs_human(self, db: sqlite3.Connection, now: float) -> int | str:
        settings = self._settings
        project = settings.github_repo
        self._blocked = None
        rules = BodyRules(settings.parent_keywords, settings.blocker_keywords)
        asking = _asking(self._runner, project, settings.human_label, rules)
        if asking is None:
            return texts.WAITING_UNREADABLE
        resting = self._resting()
        if resting is not None:
            self._blocked_since = {
                i: self._blocked_since.get(i, now)
                for i in blocked_issues(asking, resting)
            }
            self._blocked = dict(self._blocked_since)
        opened, ended = record_needs_human(db, project, set(asking), now)
        return opened + ended

    def _ci(self, db: sqlite3.Connection) -> int | str:
        project = self._settings.github_repo
        payload = read_json(
            self._runner,
            ["gh", "api", f"repos/{project}/actions/runs?per_page={LATEST}"],
        )
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            return texts.RUNS_UNREADABLE
        written, _ = record_runs(db, self._runner, self._settings, runs)
        return written  # a run left for later is read again next minute

    def _pulls(self, db: sqlite3.Connection, limit: int, since: int = 0) -> int | str:
        settings = self._settings
        payload = _merged(self._runner, settings.github_repo, limit)
        if payload is None:
            return texts.PULLS_UNREADABLE
        if len(payload) >= limit and limit == BACKFILL_LIMIT:
            return texts.PULLS_TRUNCATED.format(limit=limit)
        rows = merged_rows(settings.github_repo, payload, settings.conventions)
        rows = [r for r in rows if int(str(r["merged_at"])) >= since]
        added = store.insert(db, "pull_merged", rows)
        size = ", ".join(f"{k} = ?" for k in store.PULL_SIZE)
        for row in rows:  # recorded before its size was (#860): filled in once
            db.execute(
                f"UPDATE pull_merged SET {size}"
                " WHERE project = ? AND number = ? AND changed_files IS NULL",
                (*(row[k] for k in store.PULL_SIZE), row["project"], row["number"]),
            )
        return added

    @property
    def _project(self) -> str:
        return self._settings.github_repo

    def _quality(
        self,
        db: sqlite3.Connection,
        now: float,
        read: Callable[[sqlite3.Connection, Runner, str, float, int], int],
    ) -> int:
        return read(db, self._runner, self._project, now, BATCH)

    def _branches(self, db: sqlite3.Connection, now: float) -> int:
        return record_branch_files(db, self._runner, self._settings, now, BATCH)

    def _alerts(self, db: sqlite3.Connection, now: float) -> int:
        settings = self._settings
        try:
            send: Send | None = self._sender()
        except AlertError as exc:
            send = None
            self._say("telegram", texts.ALERTS_OFF.format(error=exc))
        else:
            self._say("telegram", 0)
        prompts = self._prompts(now)
        alarming, known = conditions(
            db,
            settings.github_repo,
            settings.base_branch,
            settings.ci_workflow,
            now,
            operator=settings.operator,
            human_label=settings.human_label,
            blocked=self._blocked,
            asking=prompts,
            budget_variable=settings.budget_variable,
            strays=self._read_strays(),
            worker_token_file=settings.worker_token_file,
            # parked red past its deadline (#1730), from the chantiers' history
            stale_parked=stale_parked(
                db,
                settings.github_repo,
                specs_recorded(db, settings.github_repo),
                int(now),
                settings.waits.parked_red_minutes,
            ),
            # a conflict no worker carries (#1791), told by a comment
            conflicts=conflicts.silent(db, settings.github_repo),
            # a worktree gone outside `clean` (#2077), for a day
            vanished=worktree_watch.vanished(db, settings.github_repo, now),
            # a green pull request the queue could not merge (#2156), for a day
            merge_failed=merge_queue.merge_failures(db, settings.github_repo, now),
        )
        # the Settings tab's switches (#2193), read again each minute
        state = self._state()
        silent = muted(state.alerts_telegram, state.alerts_off)
        with transaction(db):
            step(db, alarming, known, now, silent)
        return len(send_pending(db, now, send, silent))

    def _read_strays(self) -> list[Stray] | None:
        """The strays now; None when /proc could not be read (nothing closes)."""
        try:
            found = self._strays()
        except Exception as exc:  # said, and the episodes stand
            self._say("strays", texts.ERROR.format(error=repr(exc)))
            return None
        self._say("strays", 0)
        return found

    def _prompts(self, now: float) -> dict[int, tuple[float, str]] | None:
        """Each worker on a permission prompt, since when and what it asks;
        None when the windows could not be read (nothing closes then)."""
        try:
            asking = self._asking()
        except Exception as exc:  # tmux down: said, and the episodes stand
            self._say("asking", texts.ERROR.format(error=repr(exc)))
            return None
        self._say("asking", 0)
        if asking is None:
            return None
        self._asking_since = {
            spec: self._asking_since.get(spec, now) for spec in asking
        }
        return {spec: (self._asking_since[spec], asking[spec]) for spec in asking}

    # the one-time import ------------------------------------------------------------

    def _backfill_then_release(self) -> None:
        try:
            self.backfill()
        except Exception as exc:  # said; tried again in an hour
            self._say(texts.SOURCE_BACKFILL, texts.ERROR.format(error=repr(exc)))
        finally:
            with self._lock:
                self._backfilling = False

    def backfill(self) -> dict[str, int | str]:
        """Each part not done yet, marked done once it went through whole."""
        since = start_of(self._settings.history_since)
        parts: dict[str, Callable[[sqlite3.Connection], int | str]] = {
            "ci": lambda db: self._backfill_ci(db, since),
            "pull_merged": lambda db: self._pulls(db, BACKFILL_LIMIT, since),
            "chantier": lambda db: self._backfill_chantiers(db, since),
            "health_log": self._backfill_health,
        }
        report: dict[str, int | str] = {}
        with closing(store.connect(self._settings.history_db)) as db:
            done = {name for (name,) in db.execute("SELECT name FROM backfill")}
            for name, part in parts.items():
                if name in done:
                    continue
                try:
                    report[name] = part(db)
                except Exception as exc:
                    report[name] = texts.ERROR.format(error=repr(exc))
                if isinstance(report[name], int):
                    store.insert(
                        db,
                        "backfill",
                        [
                            {
                                "name": name,
                                "done_at": int(self._clock()),
                                "rows": report[name],
                            }
                        ],
                    )
                self._say(f"{texts.SOURCE_BACKFILL} {name}", report[name])
        return report

    def _backfill_ci(self, db: sqlite3.Connection, since: int) -> int | str:
        """Every run created since `since`, newest first, page by page until
        GitHub has no more — never `?created=` or `?status=`, which go
        through a search index that can lag (#825) and stop at 1,000 runs.

        No hole (#851): from the project's start, the runs listed must reach
        the `total_count` GitHub gave on the first page. A run created during
        the walk slides the later pages by one, so a run is read twice —
        never missed; it is written once (`run_id`, `attempt`)."""
        project = self._settings.github_repo
        runs: dict[int, dict[str, Any]] = {}
        total: int | None = None
        for page in range(1, MAX_RUN_PAGES + 1):
            payload = read_json(
                self._runner,
                [
                    "gh", "api",
                    f"repos/{project}/actions/runs?per_page=100&page={page}",
                ],
            )  # fmt: skip
            listed = payload.get("workflow_runs") if isinstance(payload, dict) else None
            if not isinstance(listed, list):
                return texts.RUNS_PAGE_UNREADABLE.format(page=page)
            if total is None:
                counted = (
                    payload.get("total_count") if isinstance(payload, dict) else None
                )
                total = counted if isinstance(counted, int) else 0
            kept = [r for r in listed if (epoch(r.get("created_at")) or 0) >= since]
            runs.update((int(r["id"]), r) for r in kept)
            if len(kept) < len(listed) or len(listed) < 100:
                break
        else:
            return texts.TOO_MANY_RUNS
        if since == 0 and total is not None and len(runs) < total:
            return texts.RUNS_SHORT.format(seen=len(runs), total=total)
        # A run still going is recorded by the minute's turn once finished.
        written, missed = record_runs(
            db, self._runner, self._settings, list(runs.values())
        )
        if missed:
            return texts.RUNS_WITHOUT_JOBS.format(n=missed)
        return written

    def _backfill_chantiers(self, db: sqlite3.Connection, since: int) -> int | str:
        project = self._settings.github_repo
        found = read_json(
            self._runner,
            [
                "gh", "api", "-X", "GET", "search/issues",
                "-f", f'q=repo:{project} "{USAGE_HEADING}" in:comments',
                "-f", "per_page=100",
            ],
        )  # fmt: skip
        items = found.get("items") if isinstance(found, dict) else None
        if not isinstance(items, list) or found.get("incomplete_results"):
            return texts.USAGE_SEARCH_UNREADABLE
        if int(found.get("total_count") or 0) > len(items):
            return texts.TOO_MANY_COMMENTED
        rows = []
        for item in items:
            spec = int(item["number"])
            comments = read_json(
                self._runner,
                ["gh", "api", f"repos/{project}/issues/{spec}/comments?per_page=100"],
            )
            if not isinstance(comments, list):
                return texts.COMMENTS_UNREADABLE.format(n=spec)
            if len(comments) >= 100:
                return texts.TOO_MANY_COMMENTS.format(n=spec)
            for comment in comments:
                posted = epoch(comment.get("created_at"))
                row = comment_row(
                    project, spec, str(comment.get("body") or ""), posted or 0
                )
                if row is not None and posted is not None and posted >= since:
                    rows.append(row)
        return store.insert(db, "chantier", rows)

    def _backfill_health(self, db: sqlite3.Connection) -> int | str:
        try:
            text = self._settings.health_log.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            return texts.HEALTH_LOG_UNREADABLE.format(error=exc)
        return store.insert(
            db, "vps_sample", health_rows(text, self._settings.history_since)
        )
