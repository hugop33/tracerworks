"""A fake GitHub for the merge queue (#1714): pull requests, the base branch's
history, the CI's runs — and every move tracerworks makes on them, in order.

It answers the `gh` calls of `merge_queue.GitHub` through a `FakeRunner`; an
unexpected call still fails the test. The CI is driven by the test: `finish`
ends the run of a head commit, as GitHub would.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from tests.tracerworks.fakes import GH_REFUSES_ESCAPES, FakeRunner
from tracerworks.process import Result

REPO = "owner/repo"
TESTING = "merge-queue-testing"
NIGHT_EVENT = "workflow_dispatch"
WORKFLOW = "ci.yml"
#: The fixture's `operator_login`, its one trusted login.
TRUSTED = "operator-login"

Check = dict[str, str]
RunRow = dict[str, Any]
Move = tuple[object, ...]


def check(
    name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED", *, run: int = 1
) -> Check:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "COMPLETED" else "",
        "detailsUrl": f"https://github.com/owner/repo/actions/runs/{run}/job/2",
    }


def cancelled_rollup(run: int) -> list[Check]:
    """A run cancelled: its tranche cancelled, and its `if: always()`
    aggregate FAILURE — as GitHub showed #2091's run 40972045718 (30/10)."""
    return [
        check("integration (1/2)", "CANCELLED", run=run),
        check("integration (HTTP end-to-end, real infra)", "FAILURE", run=run),
    ]


GREEN = [check("checks (lint, types, unit tests)"), check("integration")]
DOCS = [check("changes"), check("checks (lint, types, unit tests)", "SKIPPED")]
RED = [check("checks (lint, types, unit tests)"), check("integration", "FAILURE")]
RUNNING = [check("checks (lint, types, unit tests)", status="IN_PROGRESS")]

#: A tranche's log with a failed test, and one a machine dropped.
TEST_RED = (
    "2026-10-24T10:00:00Z FAILED tests/integration/test_widgets.py::test_reply"
    " - AssertionError\n2026-10-24T10:00:01Z ##[error]Process completed with exit"
    " code 1.\n"
)
# The red of #1973 in the queue (run 40730728470, 28/10): coloured, as
# GitHub's logs nearly always are.
COLORED_TEST_RED = (
    "2026-10-28T17:19:50Z \x1b[31mFAILED\x1b[0m tests/integration/"
    "test_browser_widget_queue.py::test_a_user_sorts_their_list - AssertionError\n"
    "2026-10-28T17:19:52Z ##[error]Process completed with exit code 1.\n"
)
MACHINE_RED = (
    "2026-10-24T10:00:00Z ##[error]The self-hosted runner: vps-2 lost"
    " communication with the server.\n"
)


@dataclass
class PR:
    number: int
    head: str
    rollup: list[Check]
    labels: set[str] = field(default_factory=set)
    open: bool = True
    conflict_on_update: bool = False
    auto_merge: bool = False
    #: GitHub still says MERGEABLE (computed before the base moved)
    stale_mergeable: bool = False
    #: who opened it, a draft, and an auto-merge that stays set (`sticks`) or
    #: whose removal fails (#1783, #1801)
    author: str = TRUSTED
    draft: bool = False
    auto_merge_sticks: bool = False
    auto_merge_off_fails: bool = False
    #: its head branch, and every head it had (#1844: the runs of its
    #: earlier heads)
    branch: str = ""
    shas: list[str] = field(default_factory=list)
    #: heads whose merge GitHub refuses, « not mergeable » (#2154: main
    #: moved by a « nothing » merge touching the same file)
    unmergeable: set[str] = field(default_factory=set)
    #: the files it modifies, as GitHub lists them (#2130)
    files: list[str] = field(default_factory=list)

    @property
    def mergeable(self) -> str:
        conflicting = self.conflict_on_update and not self.stale_mergeable
        return "CONFLICTING" if conflicting else "MERGEABLE"


@dataclass
class Hub:
    """The world: `main`, the parents of every commit, the pull requests."""

    main: str = "m0"
    parents: dict[str, list[str]] = field(default_factory=lambda: {"m0": []})
    pulls: dict[int, PR] = field(default_factory=dict)
    #: the pull request each merge commit of the base merged
    merged_by: dict[str, int] = field(default_factory=dict)
    #: the merged pull requests' rollups (read by `moved_by_nothing`)
    rollups: dict[int, list[Check]] = field(default_factory=dict)
    runs: dict[str, list[RunRow]] = field(default_factory=dict)
    night: bool = False
    #: `gh pr comment` refused (HTTP 502)
    comment_fails: bool = False
    #: `force-cancel` refused (HTTP 502)
    cancel_fails: bool = False
    clock: float = 1_800_000_000.0
    moves: list[Move] = field(default_factory=list)
    ids: count[int] = field(default_factory=lambda: count(100))
    #: the failed jobs of each run (id → name) and each job's log (None: gone)
    jobs: dict[int, dict[int, str]] = field(default_factory=dict)
    logs: dict[int, str | None] = field(default_factory=dict)
    #: when each failed job ended, and from when GitHub gives its log (#1919)
    ended: dict[int, float] = field(default_factory=dict)
    log_from: dict[int, float] = field(default_factory=dict)
    #: each failed job's failed step, as the jobs API says it (#2080)
    steps: dict[int, str] = field(default_factory=dict)
    steps_fail: bool = False  # the jobs API refuses (HTTP 502)
    #: `gh pr merge` fails for another reason (network, rights): its stderr
    merge_error: str | None = None
    #: the open issues labelled `bug`: number → title (#2130)
    bugs: dict[int, str] = field(default_factory=dict)
    bug_ids: count[int] = field(default_factory=lambda: count(900))

    # --- the test's hand ---------------------------------------------------- #

    def open(self, number: int, rollup: Sequence[Check], *, on: str = "m0") -> PR:
        """A pull request branched from `on`, its light lane as `rollup`."""
        head = f"h{number}"
        self.parents[head] = [on]
        pr = PR(number, head, list(rollup), shas=[head])
        self.pulls[number] = pr
        self.runs[head] = [self._run(head, "completed", "success")]
        return pr

    def push(self, number: int, sha: str, conclusion: str | None = None) -> None:
        """The worker pushes `sha` on top of the head: its light lane starts
        (`conclusion`: it ended so)."""
        pr = self.pulls[number]
        self.parents[sha] = [pr.head]
        pr.head = sha
        pr.shas.append(sha)
        if conclusion is None:
            self.runs[sha] = [self._run(sha, "in_progress")]
            pr.rollup = RUNNING
        else:
            self.runs[sha] = [self._run(sha, "completed", conclusion)]
            pr.rollup = GREEN if conclusion == "success" else RED

    def rerun_by_hand(self, number: int) -> int:
        """Someone reruns the last run of the head (an operator, `parking`):
        its next attempt starts. Its run's id."""
        pr = self.pulls[number]
        run = self.runs[pr.head][-1]
        run["status"], run["conclusion"] = "in_progress", ""
        run["attempt"] = int(run.get("attempt", 1)) + 1
        pr.rollup = [check("integration (1/2)", status="QUEUED", run=run["databaseId"])]
        return int(run["databaseId"])

    def push_to_main(self, name: str, *, merging: int | None = None) -> None:
        """The base moves: a merge of pull request `merging`, or a push."""
        side = [f"{name}-side"] if merging is not None else []
        for sha in side:
            self.parents[sha] = [self.main]
        self.parents[name] = [self.main, *side]
        if merging is not None:
            self.merged_by[name] = merging
        self.main = name

    def finish(
        self,
        sha: str,
        conclusion: str = "success",
        *,
        log: str | None = TEST_RED,
        log_after: float = 0.0,
        step: str = "pytest (this tranche)",
    ) -> None:
        """The CI's last run on `sha` ends; red, its one failed job's log is
        `log` (a failed test by default; None: GitHub has none), given by
        GitHub `log_after` seconds after the job's end."""
        run = self.runs[sha][-1]
        run["status"], run["conclusion"] = "completed", conclusion
        pr = next(p for p in self.pulls.values() if p.head == sha)
        pr.rollup = GREEN if conclusion == "success" else RED
        if conclusion != "success":
            job = next(self.ids)
            self.jobs[int(run["databaseId"])] = {job: "integration (1/2)"}
            self.logs[job] = log
            self.ended[job] = self.clock
            self.log_from[job] = self.clock + log_after
            self.steps[job] = step

    def _run(self, sha: str, status: str, conclusion: str = "") -> RunRow:
        number = next(self.ids)
        self.clock += 60
        return {
            "databaseId": number,
            "headSha": sha,
            "status": status,
            "conclusion": conclusion,
            "startedAt": _iso(self.clock),
            "url": f"https://github.com/{REPO}/actions/runs/{number}",
            "attempt": 1,
        }

    def ancestors(self, sha: str) -> set[str]:
        seen, todo = set(), [sha]
        while todo:
            current = todo.pop()
            if current not in seen:
                seen.add(current)
                todo.extend(self.parents.get(current, []))
        return seen

    # --- what GitHub answers ------------------------------------------------ #

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Result:
        args = list(argv[1:])
        if args[:2] == ["pr", "list"]:
            # Every open pull request: no label is asked for (#1801).
            assert "--label" not in args, args
            return _ok([
                {"number": p.number, "headRefOid": p.head,
                 "mergeable": p.mergeable, "statusCheckRollup": p.rollup,
                 "labels": [{"name": n} for n in sorted(p.labels)],
                 "autoMergeRequest": {"mergeMethod": "MERGE"} if p.auto_merge else None,
                 "isDraft": p.draft, "author": {"login": p.author},
                 "headRefName": p.branch}
                for p in self.pulls.values()
                if p.open
            ])  # fmt: skip
        if args[:2] == ["pr", "view"] and args[-1] == "autoMergeRequest":
            pr = self.pulls[int(args[2])]
            self.moves.append(("auto-read", pr.number))
            return _ok({"autoMergeRequest": {"m": "MERGE"} if pr.auto_merge else None})
        if args[:2] == ["pr", "view"] and args[-1] == "mergeable":
            pr = self.pulls[int(args[2])]
            refused = pr.head in pr.unmergeable
            return _ok({"mergeable": "CONFLICTING" if refused else pr.mergeable})
        if args[:2] == ["pr", "view"]:
            return _ok({"statusCheckRollup": self.rollups.get(int(args[2]), [])})
        if args[:2] == ["pr", "edit"]:
            pr, verb, label = self.pulls[int(args[2])], args[5], args[6]
            assert label == TESTING, label
            (pr.labels.add if verb == "--add-label" else pr.labels.discard)(label)
            self.moves.append(("label", pr.number, verb == "--add-label"))
            return _ok("")
        if args[:2] == ["pr", "comment"]:
            assert stdin is not None and "--body-file" in args
            if self.comment_fails:
                return Result(1, "", "gh: could not comment (HTTP 502)")
            self.moves.append(("comment", int(args[2]), stdin.decode()))
            return _ok("")
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args:
            pr = self.pulls[int(args[2])]
            if pr.auto_merge_off_fails:
                return Result(1, "", "gh: could not disable auto-merge (HTTP 502)")
            pr.auto_merge = pr.auto_merge_sticks
            self.moves.append(("auto-off", pr.number))
            return _ok("")
        if args[:2] == ["pr", "merge"]:
            pr = self.pulls[int(args[2])]
            assert args[args.index("--match-head-commit") + 1] == pr.head
            assert "--merge" in args
            if self.merge_error is not None:
                self.moves.append(("merge-failed", pr.number))
                return Result(1, "", self.merge_error)
            if pr.head in pr.unmergeable:
                self.moves.append(("merge-refused", pr.number))
                answer = (
                    f"X Pull request {REPO}#{pr.number} is not mergeable: the merge"
                    " commit cannot be cleanly created.\n"
                )
                return Result(1, "", answer)
            pr.open = False
            self.push_to_main(f"merge-{pr.number}")
            self.parents[self.main] = [self.parents[self.main][0], pr.head]
            self.moves.append(("merge", pr.number, pr.head))
            return _ok("")
        if (
            args[:2] == ["run", "list"]
            and "--branch" in args
            and (args[args.index("--event") + 1] == "pull_request")
        ):
            branch = args[args.index("--branch") + 1]
            pr = next(p for p in self.pulls.values() if p.branch == branch)
            return _ok([r for sha in pr.shas for r in self.runs.get(sha, [])])
        if args[:2] == ["run", "cancel"]:
            run_id = int(args[2])
            for sha, runs in self.runs.items():
                for run in runs:
                    if run["databaseId"] == run_id:
                        run["status"], run["conclusion"] = "completed", "cancelled"
                        pr = next(p for p in self.pulls.values() if p.head == sha)
                        pr.rollup = [check("integration", "CANCELLED")]
                        self.moves.append(("cancel", pr.number, sha))
                        return _ok("")
            raise AssertionError(f"no run {run_id}")
        if args[:2] == ["run", "list"]:
            if "--event" in args:
                assert args[args.index("--event") + 1] == NIGHT_EVENT
                status = "in_progress" if self.night else "completed"
                return _ok([{"status": status}])
            assert args[args.index("--workflow") + 1] == WORKFLOW
            return _ok(self.runs.get(args[args.index("--commit") + 1], []))
        if args[:2] == ["run", "rerun"]:
            run_id = int(args[2])
            for sha, runs in self.runs.items():
                if any(r["databaseId"] == run_id for r in runs):
                    fresh = self._run(sha, "in_progress")
                    attempt = max(int(r.get("attempt", 1)) for r in runs) + 1
                    runs[:] = [{**fresh, "databaseId": run_id, "attempt": attempt}]
                    pr = next(p for p in self.pulls.values() if p.head == sha)
                    pr.rollup = RUNNING
                    failed = "--failed" in args
                    kind = "rerun-failed" if failed else "rerun"
                    self.moves.append((kind, pr.number, sha))
                    return _ok("")
            raise AssertionError(f"no run {run_id}")
        if args[:2] == ["issue", "list"]:
            assert args[args.index("--label") + 1] == "bug", args
            assert args[args.index("--state") + 1] == "open", args
            return _ok([{"number": n, "title": t} for n, t in self.bugs.items()])
        if args[:2] == ["issue", "create"]:
            assert stdin is not None and "--body-file" in args
            assert args[args.index("--label") + 1] == "bug", args
            title, number = args[args.index("--title") + 1], next(self.bug_ids)
            self.bugs[number] = title
            self.moves.append(("bug", number, title, stdin.decode()))
            return _ok(f"https://github.com/{REPO}/issues/{number}\n")
        if args[:2] == ["issue", "comment"]:
            assert stdin is not None and "--body-file" in args
            self.moves.append(("bug-comment", int(args[2]), stdin.decode()))
            return _ok("")
        if args[0] == "api":
            return self._api(args[1:])
        raise AssertionError(f"unexpected gh call: {argv}")

    def _api(self, args: list[str]) -> Result:
        escapes = args[:1] == ["--allow-escape-sequences"]
        args = args[1:] if escapes else args
        if args[:2] == ["-X", "PUT"]:
            number = int(args[2].split("/")[4])
            pr = self.pulls[number]
            assert args[4] == f"expected_head_sha={pr.head}", args
            if pr.conflict_on_update:
                refused = "gh: merge conflict between base and head (HTTP 422)"
                self.moves.append(("update-refused", number))
                return Result(1, "", refused)
            new = f"{pr.head}+{self.main}"
            self.parents[new] = [pr.head, self.main]
            pr.head, pr.rollup = new, RUNNING
            pr.shas.append(new)
            self.runs[new] = [self._run(new, "in_progress")]
            self.moves.append(("update", number, self.main))
            return _ok({"message": "Updating pull request branch."})
        if args[:2] == ["-X", "POST"] and args[2].endswith("/force-cancel"):
            run_id = int(args[2].split("/")[-2])
            if self.cancel_fails:
                return Result(1, "", "gh: could not cancel (HTTP 502)")
            for sha, runs in self.runs.items():
                for run in runs:
                    if run["databaseId"] == run_id:
                        run["status"], run["conclusion"] = "completed", "cancelled"
                        pr = next(p for p in self.pulls.values() if p.head == sha)
                        pr.rollup = cancelled_rollup(run_id)
                        self.moves.append(("force-cancel", pr.number, sha))
                        return _ok("")
            raise AssertionError(f"no run {run_id}")
        if args[:2] == ["-X", "POST"]:
            sha = args[2].rsplit("/", 1)[1]
            fields = dict(a.split("=", 1) for a in args[3:] if "=" in a)
            pr = next(p for p in self.pulls.values() if p.head == sha)
            self.moves.append(("status", pr.number, fields["state"], fields["context"]))
            return _ok({})
        path = args[0]
        if "/pulls/" in path and path.split("?")[0].endswith("/files"):
            assert "--paginate" in args, args
            pr = self.pulls[int(path.split("/")[4])]
            return Result(0, "".join(f"{f}\n" for f in pr.files))
        if "/actions/runs/" in path and "/jobs?filter=latest" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            failed = self.jobs.get(run_id, {})
            return _ok({"jobs": [
                {"id": job, "name": name, "conclusion": "failure",
                 "completed_at": _iso(self.ended[job])}
                for job, name in failed.items()
            ]})  # fmt: skip
        if "/actions/jobs/" in path and args[1:2] == ["--jq"]:
            if self.steps_fail:
                return Result(1, "", "gh: Bad Gateway (HTTP 502)")
            job = int(path.rsplit("/", 1)[1])
            return Result(0, f"{self.steps.get(job, 'null')}\n")
        if path.endswith("/logs") and "/actions/jobs/" in path:
            job = int(path.split("/")[-2])
            given = self.clock >= self.log_from.get(job, 0.0)
            log = self.logs.get(job) if given else None
            if log and "\x1b" in log and not escapes:  # gh ≥ 2.101 (#1999)
                return Result(1, "", GH_REFUSES_ESCAPES)
            return Result(0, log) if log else Result(1, "", "gh: Not Found (HTTP 404)")
        if path.endswith("/git/ref/heads/main"):
            return Result(0, f"{self.main}\n")
        if "/compare/" in path:
            base, head = path.rsplit("/", 1)[1].split("...")
            if args[2] == ".behind_by":
                behind = len(self.ancestors(base) - self.ancestors(head))
                return Result(0, f"{behind}\n")
            fresh = self.ancestors(head) - self.ancestors(base)
            return _ok([{"sha": s, "parents": self.parents[s]} for s in fresh])
        if path.endswith("/pulls") and "/commits/" in path:
            sha = path.split("/")[4]
            return _ok([self.merged_by[sha]] if sha in self.merged_by else [])
        raise AssertionError(f"unexpected gh api call: {args}")


def _ok(payload: object) -> Result:
    return Result(0, payload if isinstance(payload, str) else json.dumps(payload))


def _iso(stamp: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wire(hub: Hub, fake: FakeRunner | None = None) -> FakeRunner:
    fake = fake or FakeRunner()
    fake.on("gh", prefix=True)(hub)
    return fake
