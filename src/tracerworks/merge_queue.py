"""The merge queue in front of the base branch, the green path (#1714, spec #1708).

Every pull request runs, on each push, a LIGHT lane: what its change touches
— no label asks for it (#1801, spec #1799). The full suite runs once, here,
on the base branch of the moment — like bors (Rust) and Tide (Kubernetes) —
and tracerworks alone merges.

- **Entering**: an open pull request, ready (not a draft), of a trusted
  author (`trusted_logins`), its checks green on its head commit (none
  running, none failed), no conflict with the base. The
  order is the order of entry: one row per stay in the history base, so the
  queue survives a restart.
- **Testing, one at a time**: tracerworks puts the testing label on (the CI reads
  the labels live and runs everything, as at night), then brings the branch
  up to date with the base — GitHub's « update branch », a merge commit,
  pushed by GitHub: nothing is checked out here and no hook runs — which
  starts the run on the base of the moment; a branch already up to date has
  its head run rerun instead.
- **Merging**: the full run green and the branch still containing the base,
  tracerworks posts the status on the head commit (success, the run's link) and
  merges with a merge commit, on that head only. If the base moved meanwhile
  by anything but documentation-only pull requests, the branch is updated and
  tested again.
- **Documentation only**: a pull request for which the CI ran nothing (its
  `nothing_check` skipped) merges on its turn, without a full suite. The base
  moving « by documentation » is read the same way: each merge on it is a
  pull request whose CI ran nothing — the CI's own decision (`changes`), read
  back, never a second copy of its rules.
- **The night**: no full suite starts while the night's runs.
- **The same code diff** (#1844, spec #1843): a head without its own green
  light lane enters on an earlier head's, when its code diff is the same and
  the base touched none of its code files in between (`reuse`). It is said
  once on the pull request. Its light lane still running is not cancelled at
  the entry; at its turn, up to date with the base, the queue cancels it and
  reruns that run under the testing label — the full suite replaces it.

The queue moves on each pass of the relauncher (`clean`, then `start-next`,
every minute): `clean` calls `advance` last. No service of its own.

THE RED PATHS (#1715). A pull request that does not pass leaves with its
reason, and the queue takes the next one in the same pass:

- **A test's red**: the testing label comes off and the stay leaves; the
  resume of today (`parking`, #1025) wakes the worker with the red jobs —
  told by `resume_note` that the queue sent it back, and that GitHub moved
  its branch. Its next green light lane brings it back, last.
- **A test's red its worker judges unrelated** (#2078, 29/10: #2063's
  flaky test reran the whole suite 24 min outside the queue, on the old base,
  then came back last): the « red unrelated » mark on the head the queue
  tested sends the pull request back INTO the queue, at the head of those
  waiting — no `rerun_failed`: the queue's test on the base of the moment is
  the one rerun. Its entry accepts that red only, on that head; it is
  written once per head (`merge_queue_readmit`) and said once on the pull
  request. Red again on that head: the worker, as before (`parking`). The
  mark on a light lane's red keeps its rerun.
- **A machine's red**: a run red where no failed job names a failed test
  (`pytest_log.failed_tests`: a lost runner, a killed process, a log that does
  not exist) has its failed jobs rerun ONCE, without the worker, and it is
  written on the stay (`machine_rerun_at`). Red again: a test's red. A log
  GitHub does not give yet is waited for, `machine_red.LOG_WAIT_S` after the
  jobs at most (#1919), before the red is read as a machine's. A job failed
  on a step of the code's (`code_steps`: mypy, ruff, the front's build…) is
  a CODE red, never rerun (#2080); and a head whose machine's red its light
  lane re-ran already is not re-run again: one re-run per head in all.
- **A conflict** with the base, at the update or later: it leaves, nothing
  pushed — tracerworks never resolves one — and the resume says « conflict ».
- **A refused merge** (#2156, spec #2155; #2154, 30/10: #2151, factory only,
  merged « by nothing » into #2146's file while #2146 was in test; the queue
  skipped the update, and `gh pr merge` was refused the same way each minute
  for 97 min). GitHub refusing the green one as not mergeable — said by `gh
  pr merge`, read back CONFLICTING, or listed so — takes the update path of a
  base that moved: updated and tested again, or out at a conflict, the next
  one taking the turn. `moved_by_nothing` still spares a test, never a
  refusal. Another merge error (network, rights) keeps it in test, said each
  pass, `merge_error_passes` passes running (`merge_errors` on the stay);
  then it leaves with the error, and the alert goes (`merge_failures`). A
  documentation merge failing so never ends the pass of the others.
- **A short test's red** (#2130, spec #2127; 30/10: #2099, factory only,
  sent back for one flaky test of #2125, its worker woken, then a whole
  suite again at its turn): at most `flaky_max_tests` pytest tests named
  (`tracerworks.toml`: 3; 0 the rule off), none in a file the pull request
  modifies (GitHub's list of its files), has the machine's treatment: its
  failed jobs rerun ONCE, without the worker, still in test — on the stay
  (`machine_rerun_at`) and in `machine_rerun` (`by` = flaky, its tests),
  what `status` and the card say (« rerun once: N flaky test(s) »). The
  same one re-run per head: a machine's red re-run, or the light lane's,
  and the test's red goes to the worker; red again, a test's red. Green:
  merged as any, and each test gets its `bug` — opened, or the open one
  whose title names it commented; GitHub refusing it is said, never holds
  the merge. More tests, or one of a file it modifies: the worker, as today.

A PULL REQUEST WAITING RUNS NOTHING THE QUEUE WILL REDO (#2109, spec #2108).
GitHub serves the jobs waiting for the heavy runners in the order they were
created, whatever their run: on 30/10 the queue's run for #2065 waited 70 min
for one while reruns of #2086 and #2091 — both waiting in the queue — held
them. So each pass force-cancels, on the head of each pull request waiting,
every run still alive that a rerun started (attempt 2 and more: an operator's,
`parking`'s, an old test of the queue's) — and, for a head entered on an
earlier head's green (#1844), its light lane too. Never the run in test, nor
the first light lane of a fresh push: that one says whether the head is
green. Once per run and attempt (table `merge_queue_cancel`), said on the
pull request and in `status`. A run so cancelled is NO RED until the queue
tests the pull request again (`cancelled_runs`): its `if: always()` aggregate
reads FAILURE once its tranches are cancelled (#2091, 30/10), yet neither the
queue sends it back, nor `parking` resumes its worker, nor #1790 revives it —
even once its stay is over without a test (gone, a draft). At its turn the
queue reruns everything: that test's red is a red.
THE QUEUE IS THE ONLY PATH (#1783, then #1801). Each pass takes GitHub's
auto-merge off every open pull request without `off_label`, READS IT BACK
gone, and says it on the pull request once (`merge_queue_adoption`): a green
light lane must never be enough to merge. An auto-merge that does not come
off keeps its pull request out of the queue, and `status` says it. The one
exception is `off_label` (`hors-file`), an emergency the operator sets: never
the queue's, its auto-merge left alone — the path of before.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tracerworks import low_memory, machine_red, pytest_log, reuse, store, texts
from tracerworks.cleanup import Checks, checks_of, run_of
from tracerworks.github import TRACERWORKS_SIGNATURE, job_log
from tracerworks.process import CommandError, Runner, checked
from tracerworks.settings import Settings

#: A rerun's `startedAt` is GitHub's clock, `started_at` ours: a run that
#: started this long before the test is still the test's.
RUN_SKEW_S = 120
#: Runs of a workflow still going (or not yet started), as `gh run list` says.
UNFINISHED = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
#: « Update branch » answers at once and pushes later: past this, it never will.
UPDATE_TIMEOUT_S = 15 * 60
#: GitHub's « update branch » refusing a conflict (HTTP 422).
CONFLICT = "merge conflict"
#: `gh pr merge` refusing a pull request in conflict (#2154: « X Pull request
#: …#2146 is not mergeable: the merge commit cannot be cleanly created. »).
#: Not « not mergeable » alone: a branch policy says it too, and an update
#: would not help it.
NOT_MERGEABLE = "the merge commit cannot be cleanly created"
#: How long a pull request sent back for its merge errors alerts (#2156).
MERGE_FAILED_ALERT_S = 24 * 3600


@dataclass(frozen=True)
class Pull:
    """An open pull request, as listed."""

    number: int
    head: str
    mergeable: str  # MERGEABLE, CONFLICTING, UNKNOWN
    checks: Checks
    seen: int  # checks in the rollup: none at all is not green
    nothing: bool  # the CI ran nothing: documentation only
    auto_merge: bool = False  # GitHub's auto-merge is set on it
    draft: bool = False
    author: str = ""
    off: bool = False  # it carries `off_label`: the path of before
    branch: str = ""  # its head branch: the runs of its earlier heads
    #: its status rollup as listed: read again without the runs the queue
    #: cancelled (#2109)
    rollup: tuple[dict[str, Any], ...] = field(default=(), compare=False, repr=False)

    @property
    def conflicting(self) -> bool:
        return self.mergeable == "CONFLICTING"

    @property
    def green(self) -> bool:
        """Its light lane is over and green on its head, without conflict."""
        return (
            self.seen > 0
            and not self.checks.running
            and not self.checks.failed
            and self.mergeable == "MERGEABLE"
        )


@dataclass(frozen=True)
class Run:
    id: int
    status: str
    conclusion: str
    started_at: float | None
    url: str
    attempt: int = 1


@dataclass(frozen=True)
class Stay:
    """One stay of a pull request in the queue (a row of `merge_queue`)."""

    id: int
    pull: int
    entered_at: int
    state: str  # waiting, testing, out
    tested_main: str | None = None
    #: the head before « update branch »: until the head moves, GitHub has
    #: not pushed the update yet
    updated_from: str | None = None
    tested_head: str | None = None
    started_at: int | None = None
    run_url: str | None = None
    #: its failed jobs rerun once for a machine's red (#1715)
    machine_rerun_at: int | None = None
    left_at: int | None = None
    outcome: str | None = None  # merged, sent_back, gone
    reason: str | None = None
    #: the light lane cancelled at the start, rerun once over: the full
    #: suite (#1844)
    replaced_run: int | None = None
    #: back at the head of those waiting, for a red judged unrelated (#2078)
    ahead: int = 0
    #: passes in a row its merge failed, not for « not mergeable » (#2156)
    merge_errors: int = 0


_COLUMNS = (
    "id, pull, entered_at, state, tested_main, updated_from, tested_head,"
    " started_at, run_url, machine_rerun_at, left_at, outcome, reason,"
    " replaced_run, ahead, merge_errors"
)


def _when(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def ran_nothing(rollup: Sequence[dict[str, Any]], check: str) -> bool:
    """The CI's `nothing_check` was skipped by the head's LATEST run. An
    earlier run of the same head skipping it (a draft's, everything
    skipped) never stands for a later one that ran it (#1924: #1902, code,
    read as documentation and merged without its full suite)."""
    found = [c for c in rollup if str(c.get("name") or "").startswith(check)]
    if not found:
        return False
    return max(found, key=_run_order).get("conclusion") == "SKIPPED"


def _run_order(check: dict[str, Any]) -> tuple[int, str]:
    """A check's run, newest last: its run's id (GitHub numbers them in
    order), then when it started."""
    return (run_of(check) or 0, str(check.get("startedAt") or ""))


class GitHub:
    """What the queue reads and does on GitHub, every call naming the repository."""

    def __init__(self, runner: Runner, settings: Settings) -> None:
        self.run = runner
        self.repo = settings.github_repo
        self.queue = settings.merge_queue
        self.workflow = settings.ci_workflow
        self.base = settings.base_branch

    def _gh(self, *argv: str) -> str:
        return checked(self.run(["gh", *argv]), f"gh {' '.join(argv[:2])}")

    def _json(self, *argv: str) -> Any:
        payload = self._gh(*argv)
        try:
            return json.loads(payload)
        except ValueError as exc:
            what = f"gh {' '.join(argv[:2])}"
            raise CommandError(texts.UNREADABLE_ANSWER.format(command=what)) from exc

    def open_pulls(self, fields: str) -> Any:
        """Every open pull request, with `fields` (#1790's revival)."""
        return self._json(
            "pr", "list", "--repo", self.repo, "--state", "open", "--limit", "100",
            "--json", fields,
        )  # fmt: skip

    def auto_merge_on(self, pull: int) -> bool:
        """Read back: GitHub's auto-merge still set on `pull`."""
        view = self._json(
            "pr", "view", str(pull), "--repo", self.repo, "--json", "autoMergeRequest"
        )
        return bool(view.get("autoMergeRequest"))

    def comment(self, pull: int, body: str) -> None:
        """The body through stdin, never an argument; signed tracerworks's
        (`TRACERWORKS_SIGNATURE`, #2175)."""
        checked(
            self.run(
                ["gh", "pr", "comment", str(pull), "--repo", self.repo,
                 "--body-file", "-"],
                stdin=(body + TRACERWORKS_SIGNATURE).encode("utf-8"),
            ),
            f"gh pr comment {pull}",
        )  # fmt: skip

    def pulls(self) -> list[Pull]:
        """Every open pull request, whatever its labels (#1801)."""
        listed = self._json(
            "pr", "list", "--repo", self.repo, "--state", "open", "--limit", "100",
            "--json",
            "number,headRefOid,mergeable,statusCheckRollup,autoMergeRequest,labels,"
            "isDraft,author,headRefName",
        )  # fmt: skip
        try:
            return [
                Pull(
                    number=int(p["number"]),
                    head=str(p["headRefOid"]),
                    mergeable=str(p.get("mergeable") or "UNKNOWN"),
                    checks=checks_of(p.get("statusCheckRollup") or []),
                    seen=len(p.get("statusCheckRollup") or []),
                    nothing=ran_nothing(
                        p.get("statusCheckRollup") or [], self.queue.nothing_check
                    ),
                    auto_merge=bool(p.get("autoMergeRequest")),
                    draft=bool(p.get("isDraft")),
                    author=str((p.get("author") or {}).get("login") or ""),
                    off=self.queue.off_label
                    in {str(x["name"]) for x in p.get("labels") or []},
                    branch=str(p.get("headRefName") or ""),
                    rollup=tuple(p.get("statusCheckRollup") or []),
                )
                for p in listed
            ]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            command = "gh pr list (merge queue)"
            raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc

    def disable_auto_merge(self, pull: int) -> None:
        self._gh("pr", "merge", str(pull), "--repo", self.repo, "--disable-auto")

    def failed_jobs(self, run: int) -> tuple[dict[int, str], float | None]:
        """The failed jobs of a run's latest attempt: id → name; and when
        the last of them ended (None: GitHub does not say)."""
        path = f"repos/{self.repo}/actions/runs/{run}/jobs?filter=latest&per_page=100"
        listed = self._json("api", path)
        failed = {"failure", "timed_out", "cancelled", "startup_failure"}
        jobs = [
            j
            for j in listed.get("jobs") or []
            if str(j.get("conclusion") or "") in failed
        ]
        ended = [at for j in jobs if (at := _when(j.get("completed_at")))]
        return (
            {int(j["id"]): str(j.get("name") or "?") for j in jobs},
            max(ended, default=None),
        )

    def step(self, job: int) -> str | None:
        """The failed step of a job (#2080); None: none. Unreadable raises
        (`CommandError`): never read as « no step », the red waits."""
        name = self._gh(
            "api", f"repos/{self.repo}/actions/jobs/{job}",
            "--jq", '[.steps[]? | select(.conclusion == "failure") | .name][0]',
        ).strip()  # fmt: skip
        return name if name and name != "null" else None

    def log(self, job: int) -> str | None:
        """A job's log; None when GitHub does not give it."""
        result = job_log(self.run, self.repo, job)
        return result.stdout if result.ok and result.stdout.strip() else None

    def rerun_failed(self, run: int) -> None:
        self._gh("run", "rerun", str(run), "--failed", "--repo", self.repo)

    def files(self, pull: int) -> frozenset[str]:
        """The files the pull request modifies, as GitHub lists them (#2130)."""
        path = f"repos/{self.repo}/pulls/{pull}/files?per_page=100"
        listed = self._gh("api", path, "--paginate", "--jq", ".[].filename")
        return frozenset(line.strip() for line in listed.splitlines() if line.strip())

    def open_bugs(self) -> list[tuple[int, str]]:
        """The open issues labelled `bug`: (number, title)."""
        listed = self._json(
            "issue", "list", "--repo", self.repo, "--label", "bug",
            "--state", "open", "--limit", "500", "--json", "number,title",
        )  # fmt: skip
        return [(int(i["number"]), str(i.get("title") or "")) for i in listed]

    def open_bug(self, title: str, body: str) -> int:
        """A new issue labelled `bug`, its body through stdin: its number."""
        made = checked(
            self.run(
                ["gh", "issue", "create", "--repo", self.repo, "--label", "bug",
                 "--title", title, "--body-file", "-"],
                stdin=body.encode("utf-8"),
            ),
            "gh issue create",
        )  # fmt: skip
        try:
            return int(made.strip().rstrip("/").rsplit("/", 1)[1])
        except (IndexError, ValueError) as exc:
            what = "gh issue create"
            raise CommandError(texts.UNREADABLE_ANSWER.format(command=what)) from exc

    def comment_issue(self, number: int, body: str) -> None:
        checked(
            self.run(
                ["gh", "issue", "comment", str(number), "--repo", self.repo,
                 "--body-file", "-"],
                stdin=body.encode("utf-8"),
            ),
            f"gh issue comment {number}",
        )  # fmt: skip

    def close_issue(self, number: int, body: str) -> None:
        """Close an issue with its comment, in one call (#2224): never the
        comment said twice when the close itself failed."""
        self._gh("issue", "close", str(number), "--repo", self.repo, "--comment", body)

    def main(self) -> str:
        ref = f"repos/{self.repo}/git/ref/heads/{self.base}"
        return self._gh("api", ref, "--jq", ".object.sha").strip()

    def behind(self, base: str, head: str) -> int:
        """Commits of `base` that `head` does not contain."""
        compare = f"repos/{self.repo}/compare/{base}...{head}"
        return int(self._gh("api", compare, "--jq", ".behind_by").strip())

    def moved_by_nothing(self, old: str, new: str) -> bool:
        """Whether the base went from `old` to `new` only by merges of pull
        requests whose CI ran nothing. Anything unreadable, a commit pushed
        straight to the base, a merge of no pull request: code."""
        compare = f"repos/{self.repo}/compare/{old}...{new}"
        commits = self._json(
            "api", compare, "--jq", "[.commits[] | {sha, parents: [.parents[].sha]}]"
        )
        parents = {str(c["sha"]): [str(p) for p in c["parents"]] for c in commits}
        sha = new
        while sha != old:
            if sha not in parents or len(parents[sha]) < 2:
                return False
            if not self._merged_nothing(sha):
                return False
            sha = parents[sha][0]
        return True

    def _merged_nothing(self, sha: str) -> bool:
        numbers = self._json(
            "api", f"repos/{self.repo}/commits/{sha}/pulls", "--jq", "[.[].number]"
        )
        if not numbers:
            return False
        view = self._json(
            "pr", "view", str(numbers[0]), "--repo", self.repo,
            "--json", "statusCheckRollup",
        )  # fmt: skip
        rollup = view.get("statusCheckRollup") or []
        return ran_nothing(rollup, self.queue.nothing_check)

    def label(self, pull: int, *, on: bool) -> None:
        verb = "--add-label" if on else "--remove-label"
        self._gh(
            "pr", "edit", str(pull), "--repo", self.repo,
            verb, self.queue.testing_label,
        )  # fmt: skip

    def update(self, pull: int, head: str) -> bool:
        """GitHub merges the base into the branch and pushes; False on a
        conflict, nothing pushed."""
        result = self.run(
            [
                "gh", "api", "-X", "PUT",
                f"repos/{self.repo}/pulls/{pull}/update-branch",
                "-f", f"expected_head_sha={head}",
            ]
        )  # fmt: skip
        if not result.ok and CONFLICT in (result.stderr + result.stdout).lower():
            return False
        checked(result, f"gh api update-branch #{pull}")
        return True

    def runs(self, sha: str) -> list[Run]:
        listed = self._json(
            "run", "list", "--repo", self.repo, "--workflow", self.workflow,
            "--commit", sha, "--limit", "10",
            "--json", "databaseId,status,conclusion,startedAt,url,attempt",
        )  # fmt: skip
        return [
            Run(
                int(r["databaseId"]),
                str(r.get("status") or ""),
                str(r.get("conclusion") or ""),
                _when(r.get("startedAt")),
                str(r.get("url") or ""),
                int(r.get("attempt") or 1),
            )
            for r in listed
        ]

    def rerun(self, run: int) -> None:
        self._gh("run", "rerun", str(run), "--repo", self.repo)

    def cancel(self, run: int) -> None:
        self._gh("run", "cancel", str(run), "--repo", self.repo)

    def force_cancel(self, run: int) -> None:
        """Cancelled with its `if: always()` jobs (#2013): a plain cancel
        leaves those aggregates queued for a runner."""
        self._gh(
            "api", "-X", "POST", f"repos/{self.repo}/actions/runs/{run}/force-cancel"
        )

    def earlier(self, branch: str, head: str) -> list[reuse.Earlier]:
        """The earlier heads of a pull request's branch, newest first, each
        with its last light lane (#1844)."""
        listed = self._json(
            "run", "list", "--repo", self.repo, "--workflow", self.workflow,
            "--branch", branch, "--event", "pull_request", "--limit", "50",
            "--json", "databaseId,headSha,status,conclusion,startedAt,url",
        )  # fmt: skip
        last: dict[str, dict[str, Any]] = {}
        for r in sorted(listed, key=lambda r: _when(r.get("startedAt")) or 0):
            last[str(r["headSha"])] = r
        newest = sorted(
            last.values(), key=lambda r: _when(r.get("startedAt")) or 0, reverse=True
        )
        return [
            reuse.Earlier(
                sha=str(r["headSha"]),
                finished=r.get("status") == "completed",
                green=r.get("status") == "completed"
                and r.get("conclusion") == "success",
                conclusion=str(r.get("conclusion") or ""),
                url=str(r.get("url") or ""),
            )
            for r in newest
            if str(r["headSha"]) != head
        ]

    def night(self) -> bool:
        """The night's suite is running (or waiting for its runner)."""
        listed = self._json(
            "run", "list", "--repo", self.repo, "--workflow", self.workflow,
            "--event", self.queue.night_event, "--branch", self.base,
            "--limit", "5", "--json", "status",
        )  # fmt: skip
        return any(str(r.get("status")) in UNFINISHED for r in listed)

    def merge(self, pull: int, head: str, url: str, description: str) -> str | None:
        """The status on the head, then the merge commit — of that head only.
        None once merged; GitHub's words when it refuses it for a conflict
        (#2156): said so by `gh pr merge`, or read back CONFLICTING. Any other
        failure raises."""
        link = ["-f", f"target_url={url}"] if url else []
        self._gh(
            "api", "-X", "POST", f"repos/{self.repo}/statuses/{head}",
            "-f", "state=success", "-f", f"context={self.queue.status_context}",
            *link, "-f", f"description={description}",
        )  # fmt: skip
        result = self.run(
            [
                "gh", "pr", "merge", str(pull), "--repo", self.repo, "--merge",
                "--match-head-commit", head,
            ]
        )  # fmt: skip
        if result.ok:
            return None
        said = " ".join((result.stderr or result.stdout).split())
        if NOT_MERGEABLE in said.lower():
            return said
        try:
            if self.mergeable(pull) == "CONFLICTING":
                return said
        except CommandError:
            pass  # the merge's own error is the one said
        raise CommandError(f"gh pr merge #{pull} failed: {said}")

    def mergeable(self, pull: int) -> str:
        """MERGEABLE, CONFLICTING or UNKNOWN, read now."""
        view = self._json(
            "pr", "view", str(pull), "--repo", self.repo, "--json", "mergeable"
        )
        return str(view.get("mergeable") or "UNKNOWN")


# --- the record -------------------------------------------------------------- #


def stays(settings: Settings, *, open_only: bool = True) -> list[Stay]:
    """The stays of this project, in the order of entry — those sent back to
    the head first (#2078)."""
    where = " AND state != 'out'" if open_only else ""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            f"SELECT {_COLUMNS} FROM merge_queue WHERE project = ?{where}"
            " ORDER BY ahead DESC, entered_at, id",
            (settings.github_repo,),
        ).fetchall()
    return [Stay(*row) for row in rows]


def _enter(settings: Settings, pull: int, now: float) -> None:
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state)"
            " VALUES (?, ?, ?, 'waiting')",
            (settings.github_repo, pull, int(now)),
        )


def _set(settings: Settings, stay: Stay, **values: object) -> None:
    columns = ", ".join(f"{name} = ?" for name in values)  # the code's names
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            f"UPDATE merge_queue SET {columns} WHERE id = ?",
            (*values.values(), stay.id),
        )


def _leave(
    settings: Settings, stay: Stay, outcome: str, reason: str, now: float
) -> None:
    _set(settings, stay, state="out", outcome=outcome, reason=reason, left_at=int(now))


# --- auto-merge off every pull request but `off_label`'s (#1783, #1801) --- #

#: French: written on the pull request, for the operator (texts.py keeps
#: English for what tracerworks says).
AUTO_OFF_COMMENT = (
    "tracerworks a retiré la fusion automatique de cette PR : la file de fusion est"
    " le seul chemin vers `{base}`. Chaque push ne lance que la voie légère ;"
    " verte, la file teste la PR une fois sur le `{base}` du moment, puis la"
    " fusionne. Seule exception, une urgence de prod : `{off}`, posée par"
    " l'opérateur.\n"
)


def _auto_off_records(settings: Settings, pull: int) -> list[tuple[str, str | None]]:
    """`pull`'s records, newest first: (outcome, reason) — `adopted`: its
    auto-merge taken off and said; `refused`: it would not come off."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT outcome, reason FROM merge_queue_adoption"
            " WHERE project = ? AND pull = ? ORDER BY at DESC, id DESC",
            (settings.github_repo, pull),
        ).fetchall()
    return [(str(o), r) for o, r in rows]


def _record_auto_off(
    settings: Settings, pull: int, outcome: str, reason: str | None, now: float
) -> None:
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue_adoption (project, pull, at, outcome, reason)"
            " VALUES (?, ?, ?, ?, ?)",
            (settings.github_repo, pull, int(now), outcome, reason),
        )


def auto_merge_off(
    gh: GitHub, settings: Settings, pulls: Sequence[Pull], now: float
) -> tuple[list[str], set[int]]:
    """GitHub's auto-merge off every pull request without `off_label`, read
    back gone, said on it once. What it did, one line each — and the pull
    requests whose auto-merge would not come off: they stay out of the queue."""
    said: list[str] = []
    stuck: set[int] = set()
    for pull in pulls:
        if pull.off or not pull.auto_merge:
            continue
        try:
            gh.disable_auto_merge(pull.number)
            still = gh.auto_merge_on(pull.number)
        except CommandError as exc:
            still, why = True, str(exc)
        else:
            why = texts.QUEUE_AUTO_STILL
        if still:
            # Said every pass, written once per refusal.
            if _auto_off_records(settings, pull.number)[:1] != [("refused", why)]:
                _record_auto_off(settings, pull.number, "refused", why, now)
            said.append(texts.QUEUE_AUTO_STUCK.format(pull=pull.number, why=why))
            stuck.add(pull.number)
            continue
        if not any(o == "adopted" for o, _ in _auto_off_records(settings, pull.number)):
            gh.comment(
                pull.number,
                AUTO_OFF_COMMENT.format(
                    base=settings.base_branch, off=settings.merge_queue.off_label
                ),
            )
        _record_auto_off(settings, pull.number, "adopted", None, now)
        said.append(texts.QUEUE_AUTO_OFF.format(pull=pull.number))
    return said, stuck


# --- a light lane's green reused for the same code diff (#1844) ------------- #

#: French: written on the pull request, for the operator.
REUSE_COMMENT = (
    "tracerworks : voie légère reprise du commit `{sha}` (même diff de code) —"
    " {url}\n\nLe commit `{head}` ne change pas le code de cette PR : elle entre"
    " dans la file sans attendre sa propre voie légère. La suite complète de la"
    " file tourne sur sa tête, à jour avec `{base}`, avant toute fusion.\n"
)


def _reuse_record(settings: Settings, pull: int, head: str) -> tuple[str | None] | None:
    """The verdict on `pull` at `head`: (reused_from,); None: not judged."""
    with closing(store.connect(settings.history_db)) as db:
        row = db.execute(
            "SELECT reused_from FROM merge_queue_reuse"
            " WHERE project = ? AND pull = ? AND head = ?",
            (settings.github_repo, pull, head),
        ).fetchone()
    return None if row is None else (row[0],)


def _record_reuse(
    settings: Settings, pull: int, head: str, verdict: reuse.Verdict, now: float
) -> None:
    """Written once per head."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT OR IGNORE INTO merge_queue_reuse"
            " (project, pull, head, at, reused_from, why) VALUES (?, ?, ?, ?, ?, ?)",
            (
                settings.github_repo, pull, head, int(now),
                verdict.reused_from, verdict.why,
            ),
        )  # fmt: skip


def reused_heads(
    gh: GitHub,
    settings: Settings,
    candidates: Sequence[Pull],
    now: float,
    said: list[str],
) -> dict[int, str]:
    """The pull requests whose head enters on an earlier head's green light
    lane, and that head: judged once per head, a reuse said once on the pull
    request. GitHub or git unreadable: no reuse this pass, said."""
    if not settings.merge_queue.reuse:
        return {}
    code = reuse.Code(
        gh.run, settings.repo_dir, settings.remote, settings.base_branch,
        settings.merge_queue.reuse_ignore,
    )  # fmt: skip
    found: dict[int, str] = {}
    for pull in candidates:
        record = _reuse_record(settings, pull.number, pull.head)
        if record is None:
            try:
                earlier = gh.earlier(pull.branch, pull.head) if pull.branch else []
            except CommandError as exc:
                unread = texts.QUEUE_REUSE_UNREAD
                said.append(unread.format(pull=pull.number, why=exc))
                continue
            verdict = reuse.judge(code, pull.number, pull.head, earlier)
            if not verdict.decided:
                if verdict.why.startswith(reuse.GIT_FAILED):
                    unread = texts.QUEUE_REUSE_UNREAD
                    said.append(unread.format(pull=pull.number, why=verdict.why))
                continue
            # said before it is written: a comment that fails is said again
            # on the next pass, never lost
            if verdict.reused_from:
                gh.comment(
                    pull.number,
                    REUSE_COMMENT.format(
                        sha=verdict.reused_from[:8], url=verdict.url,
                        head=pull.head[:8], base=settings.base_branch,
                    ),
                )  # fmt: skip
            _record_reuse(settings, pull.number, pull.head, verdict, now)
            record = (verdict.reused_from,)
        if record[0]:
            found[pull.number] = record[0]
    return found


def _reused_froms(settings: Settings) -> dict[int, tuple[str, int]]:
    """Each pull request's last verdict, when it reused a green: pull →
    (that head, when)."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT pull, reused_from, at FROM merge_queue_reuse r WHERE project = ?"
            " AND id = (SELECT MAX(id) FROM merge_queue_reuse s"
            " WHERE s.project = r.project AND s.pull = r.pull)",
            (settings.github_repo,),
        ).fetchall()
    return {int(p): (str(f), int(at)) for p, f, at in rows if f}


# --- the runs of a pull request waiting, cancelled (#2109) ----------------- #

#: French: written on the pull request, for the operator.
CANCEL_COMMENT = (
    "**Run annulé par la file de fusion (#2109)** : {url} (tentative {attempt})."
    " La PR attend son tour dans la file, qui refera toute sa suite sur le"
    " `{base}` du moment : ce run tenait un runner lourd pour rien. Ce n'est"
    " pas un rouge : la PR garde sa place.\n"
)


def _last_cancels(db: sqlite3.Connection, project: str) -> dict[int, tuple[str, int]]:
    """Each stay still waiting whose run the queue cancelled during it:
    pull → (the last one's link, when)."""
    rows = db.execute(
        "SELECT c.pull, c.url, c.at FROM merge_queue_cancel c"
        " JOIN merge_queue q ON q.project = c.project AND q.pull = c.pull"
        " AND q.state = 'waiting' AND c.at >= q.entered_at"
        " WHERE c.project = ? ORDER BY c.at, c.id",
        (project,),
    ).fetchall()
    return {int(p): (str(url), int(at)) for p, url, at in rows}


def cancelled_runs(db: sqlite3.Connection, project: str, pull: int) -> frozenset[int]:
    """The runs the queue cancelled for `pull` and has not tested since: no
    red — its stay over too (gone, a draft): a run cancelled is not its
    worker's red. A test started after the cancel reruns everything on its
    own: its red is a red."""
    rows = db.execute(
        "SELECT c.run FROM merge_queue_cancel c WHERE c.project = ? AND c.pull = ?"
        " AND NOT EXISTS (SELECT 1 FROM merge_queue q WHERE q.project = c.project"
        " AND q.pull = c.pull AND q.started_at >= c.at)",
        (project, pull),
    ).fetchall()
    return frozenset(int(r) for (r,) in rows)


def queue_cancelled(settings: Settings, pull: int) -> frozenset[int]:
    """`cancelled_runs`; the base unreadable: none — the red stands."""
    try:
        with closing(store.connect(settings.history_db)) as db:
            return cancelled_runs(db, settings.github_repo, pull)
    except BASE_ERRORS:
        return frozenset()


def _cancel_waiting(
    gh: GitHub,
    settings: Settings,
    pulls: dict[int, Pull],
    reused: dict[int, str],
    now: float,
    said: list[str],
) -> None:
    """On the head of each pull request waiting, every run alive that a
    rerun started — its light lane too when it entered on an earlier head's
    green — force-cancelled once per attempt, and said. Never the run in
    test: only the stays still waiting after the starts of this pass are
    read, each on its own head. A call that fails is said, and that pull
    request read again at the next pass."""
    for stay in stays(settings):
        pull = pulls.get(stay.pull)
        if stay.state != "waiting" or pull is None:
            continue
        if not checks_of(list(pull.rollup)).running:
            continue  # nothing alive on its head
        try:
            alive = [
                r
                for r in gh.runs(pull.head)
                if r.status in UNFINISHED and (r.attempt > 1 or pull.number in reused)
            ]
            for run in alive:
                _cancel_one(gh, settings, pull, run, now, said)
        except CommandError as exc:  # this one again at the next pass
            said.append(texts.QUEUE_CANCEL_FAILED.format(pull=pull.number, error=exc))


def _cancel_one(
    gh: GitHub, settings: Settings, pull: Pull, run: Run, now: float, said: list[str]
) -> None:
    """Written, then cancelled: a cancel that fails is unwritten and tried
    at the next pass; one never written is never a cancelled run read red."""
    key = (settings.github_repo, run.id, run.attempt)
    with closing(store.connect(settings.history_db)) as db:
        written = db.execute(
            "INSERT OR IGNORE INTO merge_queue_cancel"
            " (project, pull, head, run, attempt, at, url)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key[0], pull.number, pull.head, run.id, run.attempt, int(now), run.url),
        ).rowcount
        if not written:
            return  # GitHub still shows it alive: cancelled already
        try:
            gh.force_cancel(run.id)
        except CommandError:
            db.execute(
                "DELETE FROM merge_queue_cancel"
                " WHERE project = ? AND run = ? AND attempt = ?",
                key,
            )
            raise
    said.append(
        texts.QUEUE_CANCELLED.format(pull=pull.number, run=run.id, attempt=run.attempt)
    )
    try:
        gh.comment(
            pull.number,
            CANCEL_COMMENT.format(
                url=run.url or run.id, attempt=run.attempt, base=settings.base_branch
            ),
        )
    except CommandError as exc:  # cancelled and recorded: said, not again
        said.append(texts.QUEUE_CANCEL_NOT_SAID.format(pull=pull.number, error=exc))


def queueable(settings: Settings, pull: Pull) -> bool:
    """`pull` is the queue's: ready, of a trusted author, without `off_label`."""
    return not pull.draft and not pull.off and pull.author in settings.trusted_logins


def refused_auto_off_records(settings: Settings) -> list[tuple[int, str, int]]:
    """The pull requests whose last adoption was refused: (pull, why, at)."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT pull, outcome, reason, at FROM merge_queue_adoption a"
            " WHERE project = ? AND id = (SELECT MAX(id) FROM merge_queue_adoption b"
            " WHERE b.project = a.project AND b.pull = a.pull) ORDER BY pull",
            (settings.github_repo,),
        ).fetchall()
    return [(int(p), str(r or ""), int(t)) for p, o, r, t in rows if o == "refused"]


@dataclass(frozen=True)
class View:
    """What `status` and the workshop show, read from the base only."""

    testing: Stay | None
    waiting: list[Stay]
    last_out: Stay | None
    #: auto-merges that would not come off (#1783): (pull, why, at), out of
    #: the queue
    refused: tuple[tuple[int, str, int], ...] = ()
    #: the open stays whose head entered on an earlier head's green (#1844):
    #: pull → that head
    reused: dict[int, str] = field(default_factory=dict)
    #: the waiting stays whose run the queue cancelled (#2109): pull → (that
    #: run's link, when) — the last one
    cancelled: dict[int, tuple[str, int]] = field(default_factory=dict)


def view(settings: Settings) -> View:
    """The queue as recorded; `sqlite3.Error`, `OSError` or `StoreError`
    when the base cannot be read (the caller says it)."""
    open_ = stays(settings)
    with closing(store.connect(settings.history_db)) as db:
        row = db.execute(
            f"SELECT {_COLUMNS} FROM merge_queue WHERE project = ? AND state = 'out'"
            " ORDER BY left_at DESC, id DESC LIMIT 1",
            (settings.github_repo,),
        ).fetchone()
        cancelled = _last_cancels(db, settings.github_repo)
    froms = _reused_froms(settings)
    return View(
        testing=next((s for s in open_ if s.state == "testing"), None),
        waiting=[s for s in open_ if s.state == "waiting"],
        last_out=Stay(*row) if row else None,
        refused=tuple(refused_auto_off_records(settings)),
        # a verdict of this stay only: one before it entered is another's
        reused={
            s.pull: froms[s.pull][0]
            for s in open_
            if s.pull in froms and froms[s.pull][1] >= s.entered_at
        },
        cancelled=cancelled,
    )


BASE_ERRORS = (OSError, sqlite3.Error, store.StoreError)


# --- one pass ---------------------------------------------------------------- #


def advance(gh: GitHub, settings: Settings, now: float) -> list[str]:
    """One pass of the queue: what it did, one line each. A GitHub call that
    fails raises `CommandError` — the stay stays as recorded, the next pass
    reads the world again."""
    listed = gh.pulls()
    said, stuck = auto_merge_off(gh, settings, listed, now)
    pulls = {
        p.number: p for p in listed if queueable(settings, p) and p.number not in stuck
    }
    still_open = {p.number for p in listed}
    with closing(store.connect(settings.history_db)) as db:
        for number, pull in pulls.items():
            if ignore := cancelled_runs(db, settings.github_repo, number):
                # what the queue cancelled while it waits is no red (#2109)
                checks = checks_of(list(pull.rollup), ignore)
                pulls[number] = dataclasses.replace(pull, checks=checks)
    for stay in stays(settings):
        if stay.pull not in pulls:
            if stay.state == "testing" and stay.pull in still_open:
                # back to a draft, `off_label`, an untrusted author: open, it
                # must not keep running everything on each push (#1801)
                gh.label(stay.pull, on=False)
            _leave(settings, stay, "gone", texts.QUEUE_GONE_REASON, now)
            said.append(texts.QUEUE_GONE.format(pull=stay.pull))
    # The waiting ones: a red or a conflict leaves; the green ones enter —
    # while another is in test too, the order is the order of entry.
    readmitted = _readmitted(settings, pulls)
    for stay in stays(settings):
        pull = pulls[stay.pull]
        # back at the head for its red judged unrelated (#2078): that red only
        red = bool(pull.checks.failed) and stay.pull not in readmitted
        if stay.state == "waiting" and (red or pull.conflicting):
            why = texts.QUEUE_LIGHT_RED if pull.checks.failed else texts.QUEUE_CONFLICT
            _leave(settings, stay, "sent_back", why, now)
            said.append(texts.QUEUE_LEFT.format(pull=stay.pull, reason=why))
            del pulls[stay.pull]
    queued = {s.pull: s.state for s in stays(settings)}
    reused = reused_heads(
        gh, settings,
        [p for p in pulls.values()
         if queued.get(p.number) != "testing" and _may_reuse(p)],
        now, said,
    )  # fmt: skip
    for number in sorted(pulls):
        if number in queued:
            continue
        if pulls[number].green:
            _enter(settings, number, now)
            said.append(texts.QUEUE_ENTERED.format(pull=number))
        elif number in reused:
            _enter(settings, number, now)
            entered = texts.QUEUE_ENTERED_REUSED
            said.append(entered.format(pull=number, sha=reused[number][:8]))
    testing = next((s for s in stays(settings) if s.state == "testing"), None)
    busy = testing is not None and _follow(gh, settings, testing, pulls, now, said)
    if testing is not None and not busy:
        del pulls[testing.pull]  # it left this pass: it does not enter again
    for stay in stays(settings):
        pull = pulls[stay.pull]
        enters = pull.green or pull.number in reused or pull.number in readmitted
        if stay.state != "waiting" or not enters:
            continue  # a new push runs its light lane: it keeps its place
        if pull.nothing and pull.green:
            # DOCUMENTATION NEVER WAITS BEHIND A TEST (story 15): the one in
            # test merges after it without a new test (`moved_by_nothing`)
            try:
                refused = gh.merge(
                    pull.number, pull.head, "", texts.QUEUE_STATUS_NOTHING
                )
            except CommandError as exc:
                # never the pass's end: the others still move (#2156)
                _merge_error(gh, settings, stay, str(exc), now, said)
                continue
            if refused is not None:  # GitHub sees the conflict before the list
                _leave(settings, stay, "sent_back", texts.QUEUE_CONFLICT, now)
                left = texts.QUEUE_LEFT
                said.append(left.format(pull=pull.number, reason=texts.QUEUE_CONFLICT))
                continue
            _leave(settings, stay, "merged", texts.QUEUE_NOTHING_REASON, now)
            said.append(texts.QUEUE_MERGED_NOTHING.format(pull=pull.number))
            continue
        if busy:
            continue  # one in test at a time; documentation behind may merge
        if gh.night():
            said.append(texts.QUEUE_NIGHT.format(pull=pull.number))
            busy = True
            continue
        busy = _start(gh, settings, stay, pull, now, said)
    _cancel_waiting(gh, settings, pulls, reused, now, said)
    return said


def _may_reuse(pull: Pull) -> bool:
    """Not green on its own head, yet nothing red and no conflict: an
    earlier head's green may stand for it (#1844)."""
    return not pull.green and not pull.checks.failed and pull.mergeable == "MERGEABLE"


def _flaky(
    gh: GitHub, settings: Settings, pull: int, logs: Sequence[str | None]
) -> frozenset[str]:
    """A short test's red (#2130): its tests, when at most `flaky_max_tests`
    are named and none lives in a file the pull request modifies; else none.
    A file that could not even be collected (`ERROR path`, no test) is never
    flaky. GitHub's list of its files is read only then."""
    tests = frozenset(t for log in logs if log for t in pytest_log.failed_tests(log))
    if not tests or len(tests) > settings.merge_queue.flaky_max_tests:
        return frozenset()
    if any("::" not in test for test in tests):
        return frozenset()
    touched = gh.files(pull)
    if any(test.split("::", 1)[0] in touched for test in tests):
        return frozenset()
    return tests


def _names(test: str) -> set[str]:
    """How a bug's title may name a test: its id, or its function's name."""
    return {test, test.split("::")[-1].split("[", 1)[0]}


def _words(title: str) -> set[str]:
    """A title's words, as they are and without the quotes around them."""
    words = title.split()
    return {*words, *(w.strip("`'\"(),.:;!?") for w in words)}


def _flaky_bugs(
    gh: GitHub, settings: Settings, pull: int, head: str, url: str
) -> list[str]:
    """The flaky tests of a green rerun (#2130): each its `bug`, opened, or
    the open one whose title names it commented. Never raises: the merge is
    done, a refusal is said."""
    with closing(store.connect(settings.history_db)) as db:
        tests = machine_red.flaky_tests(db, settings.github_repo, pull, head)
    if not tests:
        return []
    said: list[str] = []
    try:
        bugs = gh.open_bugs()
    except CommandError as exc:
        failed = texts.QUEUE_FLAKY_BUG_FAILED
        return [failed.format(pull=pull, test=t, error=exc) for t in tests]
    for test in tests:
        found = next((n for n, title in bugs if _names(test) & _words(title)), None)
        try:
            if found is None:
                body = texts.FLAKY_BUG_BODY.format(test=test, pull=pull, url=url)
                title = texts.FLAKY_BUG_TITLE.format(test=test)
                found = gh.open_bug(title, body)
                bugs.append((found, title))
                said.append(
                    texts.QUEUE_FLAKY_BUG.format(pull=pull, test=test, bug=found)
                )
            else:
                gh.comment_issue(
                    found, texts.FLAKY_BUG_AGAIN.format(test=test, pull=pull, url=url)
                )
                commented = texts.QUEUE_FLAKY_BUG_COMMENTED
                said.append(commented.format(pull=pull, test=test, bug=found))
        except CommandError as exc:
            failed = texts.QUEUE_FLAKY_BUG_FAILED
            said.append(failed.format(pull=pull, test=test, error=exc))
    return said


def _record_code(
    settings: Settings, pull: int, head: str, step: str, now: float
) -> None:
    with closing(store.connect(settings.history_db)) as db:
        machine_red.record_code(db, settings.github_repo, pull, head, now, step)


def _flaky_reran(settings: Settings, pull: int, head: str) -> bool:
    """This head's one re-run was the queue's, for a short test's red."""
    with closing(store.connect(settings.history_db)) as db:
        return bool(machine_red.flaky_tests(db, settings.github_repo, pull, head))


def _light_reran(settings: Settings, pull: int, head: str) -> bool:
    """A light lane's machine's red of this head was re-run already (#2080)."""
    with closing(store.connect(settings.history_db)) as db:
        return machine_red.rerun_for(db, settings.github_repo, pull, head)


def _conflict(
    gh: GitHub, settings: Settings, stay: Stay, now: float, said: list[str]
) -> None:
    """The update refused for a conflict: out, nothing pushed, label off."""
    gh.label(stay.pull, on=False)
    _leave(settings, stay, "sent_back", texts.QUEUE_CONFLICT, now)
    said.append(texts.QUEUE_LEFT.format(pull=stay.pull, reason=texts.QUEUE_CONFLICT))


def _start(
    gh: GitHub, settings: Settings, stay: Stay, pull: Pull, now: float, said: list[str]
) -> bool:
    """Puts the pull request in test; False when it did not (a conflict at
    the update: it left; no run on its head to rerun: skipped). The testing
    label goes on only once the start is sure, and comes off if it fails."""
    main = gh.main()
    behind = gh.behind(main, pull.head) > 0
    head_run: Run | None = None
    if not behind:
        runs = gh.runs(pull.head)
        head_run = max(runs, key=lambda r: r.started_at or 0, default=None)
        if head_run is None:
            said.append(texts.QUEUE_SKIPPED_NO_RUN.format(pull=pull.number))
            return False
    # entered on an earlier head's green (#1844): its light lane may still run
    replacing = head_run is not None and head_run.status != "completed"
    gh.label(pull.number, on=True)
    try:
        if behind and not gh.update(pull.number, pull.head):
            _conflict(gh, settings, stay, now, said)
            return False
        if head_run is not None:
            (gh.cancel if replacing else gh.rerun)(head_run.id)
    except CommandError:
        gh.label(pull.number, on=False)
        raise
    if behind:
        _set(
            settings, stay, state="testing", tested_main=main,
            updated_from=pull.head, tested_head=None, started_at=int(now),
        )  # fmt: skip
        said.append(texts.QUEUE_UPDATED.format(pull=pull.number, sha=main[:8]))
        return True
    assert head_run is not None
    _set(
        settings, stay, state="testing", tested_main=main, updated_from=None,
        tested_head=pull.head, started_at=int(now), run_url=head_run.url,
        replaced_run=head_run.id if replacing else None,
    )  # fmt: skip
    started = texts.QUEUE_REPLACING if replacing else texts.QUEUE_RERUN
    said.append(started.format(pull=pull.number, sha=main[:8]))
    return True


def _follow(
    gh: GitHub,
    settings: Settings,
    stay: Stay,
    pulls: dict[int, Pull],
    now: float,
    said: list[str],
) -> bool:
    """The pull request in test: True while it still is (the pass ends)."""
    pull = pulls[stay.pull]
    if stay.updated_from is not None and pull.head == stay.updated_from:
        if now - (stay.started_at or now) > UPDATE_TIMEOUT_S:
            # « update branch » is asynchronous: one never pushed is no test
            gh.label(pull.number, on=False)
            _leave(settings, stay, "sent_back", texts.QUEUE_UPDATE_LOST, now)
            lost = texts.QUEUE_UPDATE_LOST
            said.append(texts.QUEUE_LEFT.format(pull=pull.number, reason=lost))
            return False
        said.append(texts.QUEUE_UPDATING.format(pull=pull.number))
        return True
    if stay.replaced_run is not None:
        if pull.head == stay.tested_head:
            return _replace(gh, settings, stay, pull, now, said)
        # a push since: its own run, under the testing label, is the test
        _set(settings, stay, replaced_run=None)
    if stay.tested_head != pull.head:
        # a new head: its merge errors start again (#2156)
        _set(settings, stay, tested_head=pull.head, merge_errors=0)
        stay = dataclasses.replace(stay, tested_head=pull.head, merge_errors=0)
    started = (stay.started_at or 0) - RUN_SKEW_S
    runs = [r for r in gh.runs(pull.head) if (r.started_at or 0) >= started]
    run = max(runs, key=lambda r: r.started_at or 0, default=None)
    if run is None or run.status != "completed":
        if run is not None and run.url != stay.run_url:
            _set(settings, stay, run_url=run.url)
        url = run.url if run else texts.QUEUE_NO_RUN_YET
        said.append(texts.QUEUE_TESTING.format(pull=pull.number, url=url))
        return True
    if run.conclusion != "success":
        rerun_at = stay.machine_rerun_at
        if rerun_at is not None and (run.started_at or 0) < rerun_at - RUN_SKEW_S:
            # rerun, and GitHub still shows the attempt before it
            said.append(texts.QUEUE_TESTING.format(pull=pull.number, url=run.url))
            return True
        failed, ended_at = gh.failed_jobs(run.id)
        jobs = ", ".join(sorted(set(failed.values()))) or "?"
        code_steps = settings.merge_queue.code_steps
        code = machine_red.code_red(failed, gh.step, code_steps, gh.log)
        if code is not None:
            _record_code(settings, pull.number, pull.head, code, now)
        # one re-run per head in all (#2080, #2130): the light lane's counts
        reran = rerun_at is not None or _light_reran(settings, pull.number, pull.head)
        logs = [] if reran or code else [gh.log(job) for job in failed]
        machine = not reran and not code and machine_red.verdict(logs, ended_at, now)
        if machine is None:
            said.append(
                texts.QUEUE_LOG_WAITS.format(pull=pull.number, jobs=jobs, url=run.url)
            )
            return True
        flaky = (
            _flaky(gh, settings, pull.number, logs)
            if logs and not machine
            else frozenset()
        )
        named = {"pull": pull.number, "url": run.url, "n": len(flaky)}
        short = low_memory.short(settings.meminfo) if machine or flaky else None
        if short is not None:
            # #2096: each re-run under the threshold was killed again on 29/10;
            # in test still, its one re-run not spent
            wait = texts.QUEUE_RERUN_WAITS_MEMORY
            if flaky:
                tests = ", ".join(sorted(flaky))
                wait = texts.QUEUE_FLAKY_RERUN_WAITS_MEMORY
                said.append(wait.format(**named, tests=tests, mb=short))
            else:
                said.append(wait.format(**named, jobs=jobs, mb=short))
            return True
        if machine or flaky:
            gh.rerun_failed(run.id)
            _set(settings, stay, machine_rerun_at=int(now), run_url=run.url)
            if flaky:
                with closing(store.connect(settings.history_db)) as db:
                    machine_red.record_flaky(
                        db, settings.github_repo, pull.number, pull.head, now, flaky
                    )
                rerun = texts.QUEUE_FLAKY_RERUN
                said.append(rerun.format(**named, tests=", ".join(sorted(flaky))))
            else:
                rerun = texts.QUEUE_MACHINE_RERUN
                said.append(rerun.format(**named, jobs=jobs))
            return True
        gh.label(pull.number, on=False)
        red = texts.QUEUE_FULL_RED_AGAIN if rerun_at else texts.QUEUE_FULL_RED
        if rerun_at and _flaky_reran(settings, pull.number, pull.head):
            red = texts.QUEUE_FULL_RED_AGAIN_FLAKY
        red = texts.QUEUE_CODE_RED if code else red
        why = red.format(jobs=jobs, url=run.url)
        _set(settings, stay, run_url=run.url)
        _leave(settings, stay, "sent_back", why, now)
        said.append(texts.QUEUE_LEFT.format(pull=pull.number, reason=why))
        return False
    main = gh.main()
    tested = stay.tested_main or main
    behind = gh.behind(main, pull.head) > 0
    if behind and pull.conflicting:
        # GitHub says it already: no merge tried, the update path (#2156)
        return _update(
            gh, settings, stay, pull, main, texts.QUEUE_UNMERGEABLE, now, said
        )
    if behind and not (
        gh.behind(tested, pull.head) == 0 and gh.moved_by_nothing(tested, main)
    ):
        return _update(gh, settings, stay, pull, main, texts.QUEUE_MOVED, now, said)
    try:
        refused = gh.merge(
            pull.number,
            pull.head,
            run.url,
            texts.QUEUE_STATUS_GREEN.format(sha=tested[:8]),
        )
    except CommandError as exc:
        return _merge_error(gh, settings, stay, str(exc), now, said)
    if refused is not None:
        # THE 30/10 (#2154): the base moved by « nothing » into the same file;
        # the shortcut no longer shields a refusal — the update, or the worker
        if not behind:
            error = texts.QUEUE_UNMERGEABLE_UP_TO_DATE.format(said=refused)
            return _merge_error(gh, settings, stay, error, now, said)
        return _update(
            gh, settings, stay, pull, main, texts.QUEUE_UNMERGEABLE, now, said
        )
    _set(settings, stay, run_url=run.url)
    _leave(settings, stay, "merged", texts.QUEUE_GREEN_REASON, now)
    said.append(texts.QUEUE_MERGED.format(pull=pull.number, url=run.url))
    if stay.machine_rerun_at is not None:  # a short test's red, green rerun
        said.extend(_flaky_bugs(gh, settings, pull.number, pull.head, run.url))
    return False


def _update(
    gh: GitHub,
    settings: Settings,
    stay: Stay,
    pull: Pull,
    main: str,
    line: str,
    now: float,
    said: list[str],
) -> bool:
    """The branch brought up to the base and tested again — True; a conflict
    at the update: out to its worker, False."""
    if not gh.update(pull.number, pull.head):
        _conflict(gh, settings, stay, now, said)
        return False
    _set(
        settings, stay, tested_main=main, updated_from=pull.head,
        tested_head=None, started_at=int(now), run_url=None, merge_errors=0,
    )  # fmt: skip
    said.append(line.format(pull=pull.number, sha=main[:8]))
    return True


def _merge_error(
    gh: GitHub, settings: Settings, stay: Stay, error: str, now: float, said: list[str]
) -> bool:
    """`gh pr merge` failed, not for « not mergeable » (#2156): still in test
    — True — until `merge_error_passes` passes in a row; then out with the
    error, its label off, and the alert (`merge_failures`)."""
    error = " ".join(error.split())
    count, passes = stay.merge_errors + 1, settings.merge_queue.merge_error_passes
    _set(settings, stay, merge_errors=count)
    if count < passes:
        failed = texts.QUEUE_MERGE_ERROR
        said.append(failed.format(pull=stay.pull, n=count, of=passes, error=error))
        return True
    if stay.state == "testing":
        gh.label(stay.pull, on=False)
    why = texts.QUEUE_MERGE_FAILED.format(n=count, error=error)
    _leave(settings, stay, "sent_back", why, now)
    said.append(texts.QUEUE_LEFT.format(pull=stay.pull, reason=why))
    return False


def merge_failures(
    db: sqlite3.Connection, project: str, now: float
) -> list[tuple[int, str]]:
    """The pull requests sent back for their merge errors in the last day:
    (pull, why) — what the alerts say (#2156)."""
    start = texts.QUEUE_MERGE_FAILED.split("{", 1)[0]
    return [
        (int(pull), str(reason))
        for pull, reason in db.execute(
            "SELECT pull, reason FROM merge_queue WHERE project = ?"
            " AND outcome = 'sent_back' AND left_at > ? AND substr(reason, 1, ?) = ?"
            " ORDER BY left_at, id",
            (project, int(now) - MERGE_FAILED_ALERT_S, len(start), start),
        )
    ]


def _replace(
    gh: GitHub, settings: Settings, stay: Stay, pull: Pull, now: float, said: list[str]
) -> bool:
    """The light lane cancelled at the start (#1844): once over, the same run
    is rerun under the testing label — the full suite. Still in test. Its
    run no longer listed: the head's last one stands for it."""
    runs = gh.runs(pull.head)
    run = next((r for r in runs if r.id == stay.replaced_run), None) or max(
        runs, key=lambda r: r.started_at or 0, default=None
    )
    if run is None or run.status != "completed":
        said.append(texts.QUEUE_REPLACING_WAIT.format(pull=pull.number))
        return True
    gh.rerun(run.id)
    _set(settings, stay, replaced_run=None, started_at=int(now))
    said.append(texts.QUEUE_REPLACED.format(pull=pull.number))
    return True


# --- what is shown ----------------------------------------------------------- #


def describe(found: View | None, now: float) -> list[str]:
    """The queue in `tracerworks status`: in test, waiting, the last exit."""
    from tracerworks.report import ago  # report → parking → cleanup: at call time

    if found is None:
        return [texts.QUEUE_VIEW_UNREADABLE]
    refused = [
        texts.QUEUE_VIEW_STUCK.format(pull=pull, why=why, ago=ago(at, now))
        for pull, why, at in found.refused
    ]
    if found.testing is None and not found.waiting and found.last_out is None:
        return [texts.QUEUE_VIEW_EMPTY, *refused]
    reused = [
        texts.QUEUE_ENTERED_REUSED.format(pull=pull, sha=sha[:8])
        for pull, sha in sorted(found.reused.items())
    ]
    cancelled = [
        texts.QUEUE_VIEW_CANCELLED.format(pull=pull, url=url, ago=ago(at, now))
        for pull, (url, at) in sorted(found.cancelled.items())
    ]
    lines = []
    if found.testing is not None:
        t = found.testing
        lines.append(
            texts.QUEUE_VIEW_TESTING.format(
                pull=t.pull,
                ago=ago(t.started_at, now),
                run=t.run_url or texts.QUEUE_NO_RUN_YET,
            )
        )
    waiting = ", ".join(f"#{s.pull}" for s in found.waiting) or texts.QUEUE_VIEW_NONE
    lines.append(texts.QUEUE_VIEW_WAITING.format(pulls=waiting))
    if found.last_out is not None:
        o = found.last_out
        lines.append(
            texts.QUEUE_VIEW_LAST.format(
                pull=o.pull,
                outcome=texts.QUEUE_OUTCOMES.get(o.outcome or "", o.outcome),
                reason=o.reason or "",
                ago=ago(o.left_at, now),
            )
        )
    return lines + reused + cancelled + refused


def as_json(found: View | None) -> dict[str, Any] | None:
    """The queue in `status --json` and the workshop's snapshot; None:
    the base unreadable."""
    if found is None:
        return None

    def stay(s: Stay) -> dict[str, Any]:
        return {
            "pull": s.pull,
            "entered_at": s.entered_at,
            "started_at": s.started_at,
            "run_url": s.run_url,
            "left_at": s.left_at,
            "outcome": s.outcome,
            "reason": s.reason,
            "reused_from": found.reused.get(s.pull) if s.state != "out" else None,
            "cancelled": (
                {"url": c[0], "at": c[1]}
                if s.state == "waiting" and (c := found.cancelled.get(s.pull))
                else None
            ),
        }

    return {
        "testing": stay(found.testing) if found.testing else None,
        "waiting": [stay(s) for s in found.waiting],
        "last_out": stay(found.last_out) if found.last_out else None,
        "refused": [
            {"pull": pull, "reason": why, "at": at} for pull, why, at in found.refused
        ],
    }


def readable_view(settings: Settings) -> View | None:
    """`view`, or None when the base cannot be read."""
    try:
        return view(settings)
    except BASE_ERRORS:
        return None


# --- what the resumed worker is told (#1715) ------------------------------- #

#: French: a worker's instruction (texts.py keeps English for humans).
RESUME_NOTE = (
    "\n\n## La file de fusion\n\n"
    "Ta PR est sortie de la file de fusion : {reason}. Pour la tester sur le"
    " `{base}` du moment, tracerworks a pu mettre ta branche à jour sur GitHub"
    " (un commit de fusion de `{base}`, sur `{remote}`) : fais `git pull` avant de"
    " corriger. Ne pose pas `{testing}` et ne fusionne pas :"
    " une fois ta voie légère verte, ta PR rentre seule dans la file.\n"
)


def resume_note(settings: Settings, pull: int) -> str:
    """Said to the worker its pull request's red resumes, when the queue sent
    it back (its last stay); empty otherwise, or when the base cannot be read."""
    try:
        with closing(store.connect(settings.history_db)) as db:
            row = db.execute(
                "SELECT outcome, reason FROM merge_queue WHERE project = ? AND pull = ?"
                " ORDER BY id DESC LIMIT 1",
                (settings.github_repo, pull),
            ).fetchone()
    except BASE_ERRORS:
        return ""
    if row is None or row[0] != "sent_back":
        return ""
    return RESUME_NOTE.format(
        reason=row[1],
        base=settings.base_branch,
        remote=settings.remote,
        testing=settings.merge_queue.testing_label,
    )


#: French: written on the pull request, for the operator (#2078).
READMIT_COMMENT = (
    "tracerworks : la PR repart en tête de la file de fusion. La suite complète"
    " de la file a rougi sur `{head}` — {url} ({tests}) — et le worker juge ce"
    " rouge sans rapport. Pas de relance hors file : le test de la file, sur le"
    " `{base}` du moment, tient lieu de relance unique. Un second rouge sur ce"
    " commit va au worker.\n"
)
#: The start of a stay's reason when the queue's full suite sent it back.
_FULL_RED = texts.QUEUE_FULL_RED.split(":", 1)[0]


def readmit(
    gh: GitHub, settings: Settings, pull: int, head: str, now: float
) -> str | None:
    """A red judged unrelated on the head the queue's full suite sent back
    (#2078): the pull request enters again, at the head of those waiting,
    said once on it; what was done, or None — not the queue's red (a light
    lane's, a conflict), or already sent back to the head for this head."""
    project = settings.github_repo
    with closing(store.connect(settings.history_db)) as db:
        row = db.execute(
            f"SELECT {_COLUMNS} FROM merge_queue WHERE project = ? AND pull = ?"
            " ORDER BY id DESC LIMIT 1",
            (project, pull),
        ).fetchone()
        done = db.execute(
            "SELECT 1 FROM merge_queue_readmit WHERE project = ? AND pull = ?"
            " AND head = ?",
            (project, pull, head),
        ).fetchone()
    last = Stay(*row) if row else None
    if (
        done is not None
        or last is None
        or last.outcome != "sent_back"
        or last.tested_head != head
        or not last.run_url
        or not (last.reason or "").startswith(_FULL_RED)
    ):
        return None
    tests = ", ".join(sorted(_failed_tests(gh, last.run_url))) or "?"
    # said before it is written: a comment that fails is tried again
    gh.comment(
        pull,
        READMIT_COMMENT.format(
            head=head[:9], url=last.run_url, tests=tests, base=settings.base_branch
        ),
    )
    with closing(store.connect(settings.history_db)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            stay = db.execute(
                "INSERT INTO merge_queue (project, pull, entered_at, state, ahead)"
                " VALUES (?, ?, ?, 'waiting', 1)",
                (project, pull, int(now)),
            ).lastrowid
            db.execute(
                "INSERT INTO merge_queue_readmit (project, pull, head, at, stay,"
                " run_url) VALUES (?, ?, ?, ?, ?, ?)",
                (project, pull, head, int(now), stay, last.run_url),
            )
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return texts.QUEUE_READMITTED.format(
        pull=pull, sha=head[:9], tests=tests, url=last.run_url
    )


def readmitted_for(settings: Settings, pull: int, head: str) -> bool:
    """`pull` was sent back to the head of the queue for `head` already."""
    with closing(store.connect(settings.history_db)) as db:
        row = db.execute(
            "SELECT 1 FROM merge_queue_readmit WHERE project = ? AND pull = ?"
            " AND head = ?",
            (settings.github_repo, pull, head),
        ).fetchone()
    return row is not None


def _failed_tests(gh: GitHub, url: str) -> set[str]:
    """The tests the run's failed jobs name; none when GitHub does not say."""
    run = run_of({"detailsUrl": url})
    if run is None:
        return set()
    try:
        failed, _ = gh.failed_jobs(run)
        logs = [gh.log(job) for job in failed]
    except CommandError:
        return set()
    return {test for log in logs if log for test in pytest_log.failed_tests(log)}


def _readmitted(settings: Settings, pulls: dict[int, Pull]) -> set[int]:
    """The waiting stays sent back to the head for a red judged unrelated,
    their head unchanged since (#2078): that red does not send them back."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT r.pull, r.head FROM merge_queue_readmit r JOIN merge_queue q"
            " ON q.id = r.stay WHERE r.project = ? AND q.state = 'waiting'",
            (settings.github_repo,),
        ).fetchall()
    return {int(p) for p, h in rows if p in pulls and pulls[p].head == h}


def holds(settings: Settings, pull: int) -> bool:
    """`pull`'s red is the queue's to read first: in test (#1715), or
    waiting at the head for a red judged unrelated (#2078). The base
    unreadable: no, the resume goes on."""
    try:
        if in_test(settings, pull):
            return True
        with closing(store.connect(settings.history_db)) as db:
            row = db.execute(
                "SELECT 1 FROM merge_queue_readmit r JOIN merge_queue q"
                " ON q.id = r.stay WHERE r.project = ? AND r.pull = ?"
                " AND q.state = 'waiting'",
                (settings.github_repo, pull),
            ).fetchone()
    except BASE_ERRORS:
        return False
    return row is not None


def in_test(settings: Settings, pull: int) -> bool:
    """`pull` is the queue's pull request in test: its red is the queue's to
    read first (#1715). The base unreadable: not in test, the resume goes on."""
    try:
        return any(s.pull == pull and s.state == "testing" for s in stays(settings))
    except BASE_ERRORS:
        return False
