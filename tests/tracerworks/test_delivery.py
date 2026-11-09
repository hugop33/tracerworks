"""What a delivered pull request cost and took, read from the history base (#860).

The Delivered panel shows it. Anything the base does not know is None — the
page says « unknown » — never a zero.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from itertools import count
from pathlib import Path

import pytest

from tracerworks import store
from tracerworks.delivery import build_delivery, pull_stats

REPO = "owner/repo"
CI = ".github/workflows/ci.yml"
HOUR = 3600
MERGED = 1_790_000_000
_ids = count(1)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(tmp_path / "f.sqlite3")) as base:
        yield base


def pull(db: sqlite3.Connection, number: int, branch: str, spec: int | None,
         size: tuple[int, int, int, int] | None = None) -> None:  # fmt: skip
    files, added, removed, closed = size or (None, None, None, None)
    store.insert(db, "pull_merged", [{"project": REPO, "number": number,
        "title": f"PR {number}", "branch": branch, "spec": spec,
        "created_at": MERGED - 5 * HOUR, "merged_at": MERGED,
        "url": f"https://github.com/{REPO}/pull/{number}", "changed_files": files,
        "additions": added, "deletions": removed,
        "closed_issues": closed}])  # fmt: skip


def run(db: sqlite3.Connection, branch: str, at: float, conclusion: str,
        jobs: Sequence[tuple[str, str | None, int]] = (),
        failed: Sequence[str] = (), path: str = CI,
        logs_read: bool = True) -> int:  # fmt: skip
    run_id = next(_ids)
    store.insert(db, "ci_run", [{"project": REPO, "run_id": run_id, "attempt": 1,
        "workflow": "CI", "workflow_path": path, "event": "pull_request",
        "branch": branch, "head_sha": "s", "conclusion": conclusion,
        "created_at": at, "started_at": at, "finished_at": at + 600, "title": "",
        "url": f"https://github.com/{REPO}/actions/runs/{run_id}"}])  # fmt: skip
    for name, machine, seconds in jobs:
        job_id = next(_ids)
        store.insert(db, "ci_job", [{"project": REPO, "job_id": job_id,
            "run_id": run_id, "attempt": 1, "name": name, "machine": machine,
            "conclusion": "failure" if failed else conclusion, "created_at": at,
            "duration_s": seconds, "url": ""}])  # fmt: skip
        failures = [{"project": REPO, "job_id": job_id, "test": t} for t in failed]
        store.insert(db, "ci_test_failure", failures)
        if failed and logs_read:  # its log read: the tests it names are all
            store.insert(db, "ci_log", [{"project": REPO, "job_id": job_id,
                "read_at": at + 900, "tests": len(failed)}])  # fmt: skip
    return run_id


def a_chantier(db: sqlite3.Connection) -> int:
    """Spec #830 and its PR #832; the id of its last CI run before the merge."""
    store.insert(db, "spec", [{"number": 830, "project": REPO,
        "title": "Spec — Worker states",
        "created_at": MERGED - 30 * HOUR}])  # fmt: skip
    store.insert(db, "chantier", [{"project": REPO, "spec": 830,
        "finished_at": MERGED + HOUR, "duration_s": 3600, "tokens_total": 1000,
        "tokens_input": 10, "tokens_cache_write": 90, "tokens_cache_read": 800,
        "tokens_output": 100, "messages": 9, "source": "clean"}])  # fmt: skip
    pull(db, 832, "agent/830-worker-states", 830, (9, 412, 37, 2))
    branch = "agent/830-worker-states"
    run(db, branch, MERGED - 4 * HOUR, "failure",
        [("tests", "vps", 600), ("lint", "github", 90)],
        ["tests/x.py::test_a"])  # fmt: skip
    run(db, branch, MERGED - 3 * HOUR, "cancelled", [("tests", "vps", 30)])
    last = run(db, branch, MERGED - 2 * HOUR, "success",
        [("tests", "vps", 540), ("lint", "github", 30)])  # fmt: skip
    # another workflow, and a run before the PR opened: not its CI
    run(
        db,
        branch,
        MERGED - HOUR,
        "success",
        [("docs", "github", 600)],
        path=".github/workflows/other.yml",
    )
    run(db, branch, MERGED - 6 * HOUR, "failure", [("tests", "vps", 600)])  # fmt: skip
    run(db, branch, MERGED + HOUR, "success", [("tests", "vps", 999)])  # after: not its
    store.insert(db, "deployment", [{"project": REPO, "run_id": 1, "head_sha": "m",
        "at": MERGED + 2 * HOUR, "url": "d"}])  # fmt: skip
    return last


def test_a_chantier_pr_shows_its_tokens_times_ci_and_size(
    db: sqlite3.Connection,
) -> None:
    last = a_chantier(db)
    stats = pull_stats(db, REPO, CI, 832)
    assert stats is not None
    assert stats["spec"] == {"number": 830, "title": "Spec — Worker states"}
    assert stats["tokens"] == {"total": 1000, "input": 10, "cache_write": 90,
                               "cache_read": 800, "output": 100}  # fmt: skip
    assert stats["times"] == {
        "published": MERGED - 30 * HOUR,
        "opened": MERGED - 5 * HOUR,
        "merged": MERGED,
        "prod": MERGED + 2 * HOUR,
    }
    ci = stats["ci"]
    # two runs judged it (the cancelled one judged nothing): not green at once
    assert (ci["runs"], ci["first_green"]) == (2, False)
    # every job of the branch before the merge, by machine, in minutes
    assert ci["minutes"] == {"github": 2.0, "vps": 19.5}
    assert ci["failed_tests"] == ["tests/x.py::test_a"]
    assert ci["last_run_url"].endswith(f"/runs/{last}")  # last CI run before merge
    assert stats["size"] == {"changed_files": 9, "additions": 412, "deletions": 37,
                             "closed_issues": 2}  # fmt: skip
    assert stats["url"] == f"https://github.com/{REPO}/pull/832"


def test_a_pr_outside_any_chantier_has_unknown_tokens_never_zero(
    db: sqlite3.Connection,
) -> None:
    pull(db, 900, "fix/typo", None)
    stats = pull_stats(db, REPO, CI, 900)
    assert stats is not None
    assert (stats["spec"], stats["tokens"]) == (None, None)
    assert stats["times"]["published"] is None and stats["times"]["prod"] is None
    # no run recorded, no size read: unknown, not zero
    assert stats["ci"] == {"runs": None, "first_green": None, "minutes": None,
                           "failed_tests": [], "last_run_url": None}  # fmt: skip
    assert set(stats["size"].values()) == {None}


def test_failed_tests_are_unknown_while_a_failed_job_s_log_is_unread(
    db: sqlite3.Connection,
) -> None:
    pull(db, 902, "agent/841-y", 841)
    run(db, "agent/841-y", MERGED - HOUR, "failure", [("tests", "vps", 60)],
        ["t.py::test_b"], logs_read=False)  # fmt: skip
    stats = pull_stats(db, REPO, CI, 902)
    assert stats is not None and stats["ci"]["failed_tests"] is None
    assert stats["ci"]["runs"] == 1  # the run itself is known


def test_a_chantier_without_its_usage_reading_has_unknown_tokens(
    db: sqlite3.Connection,
) -> None:
    pull(db, 901, "agent/840-x", 840)
    stats = pull_stats(db, REPO, CI, 901)
    assert stats is not None and stats["tokens"] is None


def test_the_page_s_answer_unknown_pr_and_unreadable_base(tmp_path: Path) -> None:
    path = tmp_path / "f.sqlite3"
    with closing(store.connect(path)) as db:
        a_chantier(db)
    answer = build_delivery(path, REPO, CI, 832)
    assert answer["error"] is None and answer["pull"]["number"] == 832
    assert build_delivery(path, REPO, CI, 7) == {"error": None, "pull": None}
    assert build_delivery(tmp_path / "none.sqlite3", REPO, CI, 832)["error"]
