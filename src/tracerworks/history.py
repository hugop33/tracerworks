"""Livré and the events of the last 48 hours (#811).

The dashboard's main use: coming back after ten hours and seeing whether it
moved. The snapshot carries DATED events; the browser keeps its own last
visit and filters them — the server keeps nothing about anyone's visit.

Kinds: `merged` (a PR), `deployed` (a successful run of the deployment
workflow), `ticket_closed`, `spec_created`, `chantier_started` (read in the
queue relauncher's journal), `run_failed`. A source that could not be read
is named in `events_unknown`: its events are unknown, not zero.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tracerworks.control import epoch, guarded, latest_runs, read_json, workflow_runs
from tracerworks.frontier import Issue
from tracerworks.process import Runner

WINDOW_SECONDS = 48 * 3600
DELIVERED_LIMIT = 100  # deployment runs listed (skipped ones included: one per push)
#: Merged PRs and red runs listed. A list this full whose oldest item is still
#: inside the window may be missing some: that kind of event is unknown.
EVENTS_LIMIT = 100
#: The end of the journal is enough for 48 h (it grows ~4 kB an hour).
LOG_TAIL_BYTES = 512 * 1024

_STAMP = re.compile(r"^--- (\S+)$")
# `texts.LAUNCHED`, and its French form in the journals written before #850;
# since #2251 the provider is followed by the model and the effort passed
_LAUNCHED = re.compile(r"^(\S+) (?:launched|lancé) \((\w+)(?:; [^\n]*)?\)$")


def _merged(runner: Runner, repo: str) -> list[dict[str, Any]] | None:
    payload = read_json(
        runner,
        [
            "gh", "pr", "list", "-R", repo, "--state", "merged",
            "--limit", str(EVENTS_LIMIT), "--json", "number,title,mergedAt,url",
        ],
    )  # fmt: skip
    if not isinstance(payload, list):
        return None
    return [
        {
            "kind": "pr",
            "number": int(p["number"]),
            "title": str(p.get("title") or ""),
            "url": str(p.get("url") or ""),
            "at": epoch(p.get("mergedAt")),
        }
        for p in payload
    ]


def _run_item(kind: str, run: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "number": int(run["id"]),
        "title": str(run.get("display_title") or run.get("name") or ""),
        "workflow": str(run.get("name") or ""),
        "url": str(run.get("html_url") or ""),
        "at": epoch(run.get("updated_at")),
    }


def launches(
    journal: Path, spec_of_window: Callable[[str], int | None]
) -> list[tuple[int, int]] | None:
    """(spec, epoch) of every chantier the relauncher launched, from the end
    of its journal; None when it cannot be read."""
    try:
        with journal.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    found, stamp = [], None
    for line in text.splitlines():
        if match := _STAMP.match(line):
            stamp = epoch(match.group(1))
        elif (match := _LAUNCHED.match(line)) and stamp is not None:
            spec = spec_of_window(match.group(1))
            if spec is not None:
                found.append((spec, stamp))
    return found


def build_history(
    runner: Runner,
    repo: str,
    deploy_workflow: str,
    journal: Path,
    spec_of_window: Callable[[str], int | None],
    now: float,
) -> dict[str, Any]:
    """Livré, and the dated events of GitHub and the journal (issues apart:
    `events_of_issues`)."""
    merged = guarded(lambda: _merged(runner, repo))
    runs = f"repos/{repo}/actions"
    deploys = workflow_runs(
        runner, f"{runs}/workflows/{deploy_workflow}/runs?per_page={DELIVERED_LIMIT}"
    )
    # The dated list, filtered here — never `?status=failure`, whose search
    # index can lag or come back out of order (#825).
    latest = latest_runs(runner, repo)
    failed = (
        None
        if latest is None
        else [r for r in latest if r.get("conclusion") == "failure"]
    )
    launched = launches(journal, spec_of_window)
    deployed = guarded(
        lambda: [
            _run_item("deploy", r)
            for r in deploys or []
            if r.get("conclusion") == "success"
        ]
    )
    if deploys is None:
        deployed = None
    red = (
        None
        if failed is None
        else guarded(lambda: [_run_item("run", r) for r in failed or []])
    )
    delivered = None
    if merged is not None or deployed is not None:
        delivered = sorted(
            [*(merged or []), *(deployed or [])], key=lambda d: -(d["at"] or 0)
        )
    events: list[dict[str, Any]] = []
    unknown = []
    # Truncation is judged on what was LISTED (the deployments list holds
    # failed runs too), against the limit that list was asked with.
    for kind, items, listed, limit in (
        ("merged", merged, merged, EVENTS_LIMIT),
        ("deployed", deployed, deploys, DELIVERED_LIMIT),
        ("run_failed", red, failed, EVENTS_LIMIT),
    ):
        if items is None or _maybe_truncated(listed or [], limit, now):
            unknown.append(kind)
        else:
            events += [{**item, "kind": kind} for item in items]
    if launched is None:
        unknown.append("chantier_started")
    else:
        events += [
            {
                "kind": "chantier_started",
                "number": spec,
                "title": "",
                "url": "",
                "at": at,
            }
            for spec, at in launched
        ]
    return {"delivered": delivered, "events": events, "events_unknown": unknown}


def _maybe_truncated(listed: Sequence[dict[str, Any]], limit: int, now: float) -> bool:
    """A full list whose oldest item is still in the window may miss some."""
    stamps = [epoch(i.get("updated_at")) if "at" not in i else i["at"] for i in listed]
    return len(listed) >= limit and all(
        t is not None and t >= now - WINDOW_SECONDS for t in stamps
    )


def events_of_issues(
    open_issues: Sequence[Issue] | None,
    closed: Sequence[Issue] | None,
    ready_label: str,
    issue_url: Callable[[int], str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Specs created (open, ready, not a ticket) and tickets closed."""
    events, unknown = [], []

    def event(kind: str, issue: Issue, at: int | None) -> dict[str, Any]:
        return {"kind": kind, "number": issue.number, "title": issue.title,
                "url": issue_url(issue.number), "at": at}  # fmt: skip

    if open_issues is None:
        unknown.append("spec_created")
    else:
        events += [
            event("spec_created", i, i.created_at)
            for i in open_issues
            if i.parent is None and ready_label in i.labels
        ]
    if closed is None:
        unknown.append("ticket_closed")
    else:
        events += [event("ticket_closed", i, i.closed_at) for i in closed]
    return events, unknown


def recent(events: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """The events of the last 48 h, most recent first."""
    kept = [
        e for e in events if e["at"] is not None and now - e["at"] <= WINDOW_SECONDS
    ]
    return sorted(kept, key=lambda e: -e["at"])
