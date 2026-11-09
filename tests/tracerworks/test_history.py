"""Livré and « depuis ta dernière visite » (#811).

The snapshot carries DATED events over 48 h; the browser filters them by its
own last visit. Nothing about a visit is kept by the server.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import tracerworks.serve
import tracerworks.snapshot
from tests.tracerworks.fakes import recorded_world
from tests.tracerworks.history_world import MERGED_FIELDS, REPO, merged
from tests.tracerworks.test_snapshot import github_world, snapshot


def at(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp())


def data(tmp_path: Path, **kw: Any) -> dict[str, Any]:
    return snapshot(github_world(recorded_world()), tmp_path, **kw)


def test_each_kind_of_event_with_its_time_and_nothing_older_than_48_h(
    tmp_path: Path,
) -> None:
    events = data(tmp_path)["events"]
    assert sorted((e["kind"], e["number"], e["at"]) for e in events) == sorted(
        [
            ("merged", 812, at("2026-09-26T06:00:00+00:00")),
            ("deployed", 300, at("2026-09-26T06:10:00+00:00")),
            ("ticket_closed", 502, at("2026-09-25T20:00:00+00:00")),
            ("spec_created", 600, at("2026-09-26T01:00:00+00:00")),
            ("chantier_started", 620, at("2026-09-26T03:05:00+00:00")),
            ("run_failed", 91, at("2026-09-26T06:20:00+00:00")),
        ]
    )
    merged = next(e for e in events if e["kind"] == "merged")
    assert merged["url"] == f"https://github.com/{REPO}/pull/812"
    assert merged["title"] == "PR 812"


def test_delivered_is_merged_prs_and_deployments_most_recent_first(
    tmp_path: Path,
) -> None:
    delivered = data(tmp_path)["delivered"]
    assert [(d["kind"], d["number"]) for d in delivered] == [
        ("deploy", 300),
        ("pr", 812),
        ("pr", 790),
        ("deploy", 302),
    ]


def test_what_moved_is_dated_where_it_is_shown(tmp_path: Path) -> None:
    snap = data(tmp_path)
    [waiting] = snap["waiting"]["specs"]
    assert waiting["created_at"] == at("2026-09-26T01:00:00+00:00")
    worker = next(w for w in snap["workers"] if w["spec"] == 501)
    done = next(t for t in worker["tickets"] if t["number"] == 502)
    assert done["closed_at"] == at("2026-09-25T20:00:00+00:00")


def test_an_unreadable_source_is_named_never_counted_as_zero(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    fake.answer(
        "gh",
        "pr",
        "list",
        "-R",
        REPO,
        "--state",
        "merged",
        prefix=True,
        returncode=1,
        stdout="HTTP 502",
    )
    snap = snapshot(fake, tmp_path, log=None)
    assert snap["events_unknown"] == ["chantier_started", "merged"]
    assert all(e["kind"] not in ("merged", "chantier_started") for e in snap["events"])
    assert snap["delivered"] is not None  # the deployments are still known


def test_no_visit_is_kept_by_the_server() -> None:
    for module in (tracerworks.serve, tracerworks.snapshot):
        source = inspect.getsource(module).lower()
        assert "last_visit" not in source and "cookie" not in source


def test_a_full_list_still_inside_the_window_is_unknown_not_complete(
    tmp_path: Path,
) -> None:
    fake = github_world(recorded_world())
    full = [merged(1000 + i, "2026-09-26T05:00:00Z") for i in range(100)]
    fake.answer(
        "gh", "pr", "list", "-R", REPO, "--state", "merged", "--limit", "100",
        "--json", MERGED_FIELDS, stdout=json.dumps(full),
    )  # fmt: skip
    snap = snapshot(fake, tmp_path)
    assert "merged" in snap["events_unknown"]
    assert len(snap["delivered"]) > 100  # Livré still lists them


def test_a_full_deployments_list_inside_the_window_is_unknown(tmp_path: Path) -> None:
    from tests.tracerworks.history_world import deploy

    fake = github_world(recorded_world())
    runs = [deploy(400 + i, "success", "2026-09-26T05:00:00Z") for i in range(100)]
    fake.answer(
        "gh", "api", f"repos/{REPO}/actions/workflows/deploy.yml/runs?per_page=100",
        stdout=json.dumps({"workflow_runs": runs}),
    )  # fmt: skip
    assert "deployed" in snapshot(fake, tmp_path)["events_unknown"]


def test_failed_runs_come_from_the_dated_list_never_the_status_index(
    tmp_path: Path,
) -> None:
    """#825: the `status=failure` index can lag — a red run it has not
    indexed yet must still be an event: the dated list is the source."""
    fake = github_world(recorded_world())
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/actions/runs?status=failure&per_page=100",
        stdout=json.dumps({"workflow_runs": []}),
    )
    events = snapshot(fake, tmp_path)["events"]
    assert [e["number"] for e in events if e["kind"] == "run_failed"] == [91]
