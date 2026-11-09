"""What the dashboard calls a worker at rest (#830).

The operator saw #813 « bloqué » while it waited for its CI — its pull request even
merged already. « bloqué » is kept for a worker that asked the operator
(`needs-human`); the others at rest are « attend la CI », « fini » (its pull
request merged) or « au repos ». In the recorded world, #510 is at rest on
`agent/510-classification`, its ticket #511 carrying `needs-human`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.tracerworks.control_world import PR_FIELDS, PULLS, check, pr
from tests.tracerworks.fakes import FakeRunner, recorded_world
from tests.tracerworks.test_snapshot import (
    FIELDS,
    OPEN,
    REPO,
    WAITING_AND_QUEUE,
    github_world,
    merged_answer,
    snapshot,
    workers_of,
)

BRANCH = "agent/510-classification"


def without_the_question(fake: FakeRunner) -> FakeRunner:
    """#511 no longer carries needs-human: the worker asks nothing."""
    answered: list[dict[str, Any]] = json.loads(json.dumps(OPEN + WAITING_AND_QUEUE))
    for issue in answered:
        issue["labels"] = [lb for lb in issue["labels"] if lb["name"] != "needs-human"]
    fake.answer(
        "gh", "issue", "list", "-R", REPO, "--state", "open", "--limit", "1000",
        "--json", FIELDS, stdout=json.dumps(answered),
    )  # fmt: skip
    return fake


def with_ci(fake: FakeRunner, *checks: dict[str, Any]) -> FakeRunner:
    pulls = [*PULLS, pr(818, BRANCH, list(checks), auto=True)]
    fake.answer(
        "gh", "pr", "list", "-R", REPO, "--state", "open", "--limit", "100",
        "--json", PR_FIELDS,
        stdout=json.dumps(pulls),
    )  # fmt: skip
    return fake


def state(fake: FakeRunner, tmp_path: Path) -> dict[str, Any]:
    worker: dict[str, Any] = workers_of(snapshot(fake, tmp_path))[510]
    return worker


def test_a_worker_that_asked_operator_is_blocked_even_while_its_ci_runs(
    tmp_path: Path,
) -> None:
    worker = state(
        with_ci(github_world(recorded_world()), check("IN_PROGRESS")), tmp_path
    )
    assert worker["state"] == "blocked"
    assert worker["blocked_by"]["number"] == 511


def test_a_ci_underway_is_waited_for_not_blocked(tmp_path: Path) -> None:
    fake = with_ci(
        without_the_question(github_world(recorded_world())), check("QUEUED")
    )
    worker = state(fake, tmp_path)
    assert (worker["state"], worker["blocked_by"]) == ("ci", None)


def test_a_merged_pull_request_is_done(tmp_path: Path) -> None:
    fake = without_the_question(github_world(recorded_world()))
    merged_answer(fake, BRANCH, 824)
    worker = state(fake, tmp_path)
    assert worker["state"] == "done"
    assert worker["pull_request"] == {"number": 824, "ci": "merged"}


def test_anything_else_is_at_rest(tmp_path: Path) -> None:
    """No question, no CI underway, nothing merged — a finished CI included."""
    fake = with_ci(
        without_the_question(github_world(recorded_world())),
        check("COMPLETED", "FAILURE", "2026-09-26T05:00:00Z"),
    )
    assert state(fake, tmp_path)["state"] == "idle"
    assert (
        state(without_the_question(github_world(recorded_world())), tmp_path)["state"]
        == "idle"
    )


def test_an_unreadable_merge_lookup_is_at_rest_never_done(tmp_path: Path) -> None:
    fake = without_the_question(github_world(recorded_world()))
    fake.answer(
        "gh", "pr", "list", "--repo", REPO, prefix=True, returncode=1, stdout="502"
    )
    assert state(fake, tmp_path)["state"] == "idle"
