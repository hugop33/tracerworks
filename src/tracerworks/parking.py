"""A worker waiting on its CI is parked, then resumed if the CI goes red (#1025).

The cap counts the workers in a window; a worker that only waits for its CI
held one of the two slots for an hour (08/10: #1016 « waiting for CI » 1 h 22,
#1021 queued behind it). So `clean` PARKS it: its Claude session is closed,
the session's id is written here, and the worktree, the branch and the pull
request stay. A parked worker has no window: it does not count, and
`start-next` may launch the next chantier.

`start-next` then RESUMES a parked worker before any new chantier — under the
same guards, so a full workshop delays the resume, never skips it — when its
pull request:

- is open, with a failed check or a conflict with the base, on a head commit
  it was not resumed for yet (a worker that judged a red unrelated and went
  back to rest is not resumed again for the same commit);
- is merged, with a check red after the merge not told yet (the hidden marker
  of `cleanup.RED_MARKER`).

A RED TOLD BUT UNANSWERED IS TOLD ONCE MORE (#2175, 31/10: #2131's worker,
resumed for a red of the merge queue at 03:19 UTC, left a diagnostic line
uncommitted and a local replay in the background; its session was gone at
03:34, and the red, « told », never woke it again for 4 h 30). Resumed for
the red its pull request still shows, with neither a push (a new head) nor a
comment of a trusted login on the pull request since: the next free session
resumes it once more, before any new chantier, its uncommitted files named
(`AGAIN`). Then no more: the parked-red alert is the operator's.

A green CI resumes nothing: the merge and `clean` finish the chantier as before.

A RED THE WORKER JUDGED UNRELATED IS RERUN ONCE (#1280, 12/10: PRs #1252 and
#1268 stayed red, their auto-merge waiting, until a rerun by hand). The worker
judges, tracerworks acts: the worker's comment on its PR starts with the mark
`RERUN_MARK` naming the head commit, and it goes to rest at once. Once that
commit's checks are over, its failed runs are rerun (`gh run rerun --failed`),
once per commit — recorded here (`rerun_for`, `rerun_at`). Red again on the
same commit: the worker is resumed with `RESUME_AFTER_RERUN`. No mark, nothing
is rerun: a real defect is never rerun blind. The mark on the head the merge
queue's full suite sent back reruns nothing (#2078): the pull request goes
back to the head of the queue (`merge_queue.readmit`), its test the rerun.

The session is reopened with `claude --resume <id>` in the chantier's
worktree — the same conversation, so the worker knows its chantier — and the
reason of the resume (failed jobs, end of their logs) goes into a file it is
told to read, never into an argument.

The record lives in the history base: a resume survives a restart of
`tracerworks` or of the machine. One row per chantier; parking again replaces the
session but keeps the head already told.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Collection
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from tracerworks import store, texts
from tracerworks.cleanup import PULL_TEXT_IS_DATA, Checks, checks_of
from tracerworks.github import by_tracerworks, job_log, written_by
from tracerworks.machine_red import FailedRun
from tracerworks.process import CommandError, Runner, checked
from tracerworks.settings import Settings
from tracerworks.usage import project_dir

#: How much of a failed job's log the resumed worker is given.
LOG_TAIL_LINES = 40
LOG_LINE_CHARS = 300


@dataclass(frozen=True)
class Parked:
    spec: int
    session: str | None  # None: no transcript found, nothing to resume
    pull: int
    parked_at: int
    resumed_for: str | None  # `PullState.told` of its last resume (+ `AGAIN`)
    rerun_for: str | None = None  # the head commit whose failed runs were rerun
    rerun_at: int | None = None
    resumed_at: int | None = None

    @property
    def short_session(self) -> str:
        return self.session[:8] if self.session else "?"


def latest_session(transcripts_root: Path, worktree: Path) -> str | None:
    """The id of the newest Claude Code session of a worktree: its transcript
    is `<project dir>/<id>.jsonl` — what `claude --resume <id>` reopens."""
    folder = project_dir(transcripts_root, worktree)
    try:
        sessions = [p for p in folder.glob("*.jsonl") if p.is_file()]
        newest = max(sessions, key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return None
    if newest is None or not _SESSION_ID.fullmatch(newest.stem):
        return None
    return newest.stem


#: Claude Code names a transcript after its session, a UUID: anything else is
#: not passed to `--resume`.
_SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def park(
    settings: Settings,
    spec: int,
    session: str | None,
    pull: int,
    now: float,
    seen: str | None = None,
) -> None:
    """`seen`: the `PullState.told` the worker already saw at rest — a red it
    went back to rest on is not a reason to wake it (no park → resume churn).
    A `seen` other than what it was resumed for forgets the resume's time:
    a red seen live, or seen again after its second telling (`AGAIN`), is
    not « resumed for and unanswered » (#2175)."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT INTO parked"
            " (project, spec, session_id, pull, parked_at, resumed_for)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (spec, project) DO UPDATE SET"
            " session_id = excluded.session_id, pull = excluded.pull,"
            " parked_at = excluded.parked_at,"
            " resumed_at = CASE WHEN excluded.resumed_for IS NOT NULL"
            " AND excluded.resumed_for IS NOT parked.resumed_for"
            " THEN NULL ELSE parked.resumed_at END,"
            " resumed_for = coalesce(excluded.resumed_for, parked.resumed_for)",
            (settings.github_repo, spec, session, pull, int(now), seen),
        )


def parked(settings: Settings) -> dict[int, Parked]:
    """The parked chantiers of this project; a base that cannot be read is
    raised (`StoreError`, `sqlite3.Error`, `OSError`): the caller says it."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT spec, session_id, pull, parked_at, resumed_for, rerun_for,"
            " rerun_at, resumed_at FROM parked"
            " WHERE project = ?",
            (settings.github_repo,),
        ).fetchall()
    return {row[0]: Parked(*row) for row in rows}


def readable_parked(settings: Settings) -> tuple[dict[int, Parked], Exception | None]:
    """`parked`, or none and why: the base unreadable never stops a command."""
    try:
        return parked(settings), None
    except (OSError, sqlite3.Error, store.StoreError) as exc:
        return {}, exc


def mark_resumed(settings: Settings, spec: int, told: str, now: float) -> None:
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "UPDATE parked SET resumed_for = ?, resumed_at = ?"
            " WHERE project = ? AND spec = ?",
            (told, int(now), settings.github_repo, spec),
        )


def mark_refused(settings: Settings, spec: int, sha: str) -> None:
    """Its failed runs are the merge queue's (#2110): nothing rerun for
    `sha`, ever — and its red not told yet, so the next pass resumes its
    worker with the red as it is, never « rerun once »."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "UPDATE parked SET rerun_for = ?, rerun_at = NULL, resumed_for = NULL"
            " WHERE project = ? AND spec = ?",
            (sha, settings.github_repo, spec),
        )


def mark_rerun(settings: Settings, spec: int, sha: str, now: float) -> None:
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "UPDATE parked SET rerun_for = ?, rerun_at = ?"
            " WHERE project = ? AND spec = ?",
            (sha, int(now), settings.github_repo, spec),
        )


def forget(settings: Settings, spec: int) -> None:
    """A new session (`start`) or a removed worktree: nothing left to resume."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "DELETE FROM parked WHERE project = ? AND spec = ?",
            (settings.github_repo, spec),
        )


@dataclass(frozen=True)
class PullState:
    number: int
    state: str  # OPEN, MERGED, CLOSED
    head_sha: str
    conflicting: bool
    checks: Checks
    #: a comment of the PR carries the worker's « red unrelated » mark for the
    #: head commit (#1280)
    marked: bool = False
    #: GitHub still computing `mergeable` — after a merge on the base (#2137)
    mergeable_unknown: bool = False
    #: when a trusted login last commented on the pull request (#2175), not
    #: tracerworks (`TRACERWORKS_SIGNATURE`): the worker's answer to a red; None:
    #: never; `math.inf` when GitHub gives a comment no date — an answer,
    #: never a second telling on a guess
    commented_at: float | None = None

    @property
    def ci_underway(self) -> bool:
        """Something still runs (or waits for a runner) and nothing failed."""
        return bool(self.checks.running) and not self.checks.failed

    @property
    def needs_worker(self) -> bool:
        return bool(self.checks.failed) or self.conflicting

    @property
    def told(self) -> str:
        """What a resume tells: the head, and red and/or conflict — a conflict
        after a red on the same head is news, the same red again is not."""
        head = self.head_sha
        why = "red" if self.checks.failed else ""
        return (conflict_told(head) if self.conflicting else f"{head}:") + why


def conflict_told(head: str) -> str:
    """How `PullState.told` starts for a conflict on `head`."""
    return f"{head}:conflict,"


def pull_state(
    runner: Runner,
    repo: str,
    pull: int,
    trusted: Collection[str],
    ignore: Collection[int] = (),
) -> PullState:
    """The pull request as a resume reads it; its « red unrelated » mark
    counts only from a trusted login (#1529) — anyone else's comment cannot
    make tracerworks rerun CI. The runs in `ignore` (the merge queue cancelled
    them while it waits, #2109) are no red."""
    command = f"gh pr view {pull} (state)"
    payload = checked(
        runner(
            [
                "gh",
                "pr",
                "view",
                str(pull),
                "--repo",
                repo,
                "--json",
                "state,headRefOid,mergeable,statusCheckRollup,comments",
            ]
        ),
        command,
    )
    try:
        data = json.loads(payload)
        head = str(data["headRefOid"])
        trusted_comments = [
            c for c in data.get("comments") or [] if written_by(c, trusted)
        ]
        return PullState(
            number=pull,
            state=str(data["state"]),
            head_sha=head,
            conflicting=data.get("mergeable") == "CONFLICTING",
            mergeable_unknown=str(data.get("mergeable") or "UNKNOWN") == "UNKNOWN",
            checks=checks_of(data.get("statusCheckRollup") or [], ignore),
            marked=any(marks(str(c.get("body") or ""), head) for c in trusted_comments),
            commented_at=max(
                (
                    _started(c.get("createdAt")) or math.inf
                    for c in trusted_comments
                    if not by_tracerworks(c)  # tracerworks's own: no answer
                ),
                default=None,
            ),
        )
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc


#: The first line of the worker's comment on its PR when it judges a red
#: unrelated (#1280): the primer and `RESUME_RED` give it, `marks` reads it.
RERUN_MARK = "Rouge sans rapport — {sha}"
#: an em dash, an en dash or a hyphen: the worker may not type the em dash
_MARK = re.compile(
    r"rouge sans rapport\s*[\u2014\u2013-]\s*`?([0-9a-f]{7,40})\b", re.IGNORECASE
)
#: How long a red read after the rerun is still the old one: `clean` reruns,
#: `start-next` follows in the same second, before GitHub shows the rerun.
RERUN_SETTLE_S = 120

#: After a resume, a worker read « at rest » on the red it was resumed for is
#: not parked again before this (19/10, #1539): one look at its pane, between
#: two of its commands, read it at rest a minute after its resume, and the
#: red, recorded as seen, never woke it again.
RESUME_GRACE_S = 900


def in_resume_grace(pull: PullState, record: Parked | None, now: float) -> bool:
    """Resumed for what `pull` tells now, less than `RESUME_GRACE_S` ago.

    A resume for a conflict holds while GitHub says `UNKNOWN` on the same
    head (30/10, #2137): a merge on the base makes it recompute, and the
    worker, in the middle of its own merge, was parked 15 s after its
    resume — until a push (a new head) or the pull request mergeable."""
    return (
        record is not None
        and record.resumed_at is not None
        and now - record.resumed_at < RESUME_GRACE_S
        and resumed_for_told(
            told_of(_unknown_as_conflict(pull, record), record, now), record
        )
    )


def _unknown_as_conflict(pull: PullState, record: Parked) -> PullState:
    """`pull` still in conflict when resumed for its head's conflict and
    GitHub does not know yet."""
    resumed_for = record.resumed_for or ""
    if pull.mergeable_unknown and resumed_for.startswith(conflict_told(pull.head_sha)):
        return replace(pull, conflicting=True)
    return pull


#: Added to `PullState.told` for a resume after the rerun: the same red, news.
AFTER_RERUN = ":rerun"
#: Added to what a resume told for its second telling (#2175): never a third.
AGAIN = ":again"


def resumed_for_told(told: str, record: Parked) -> bool:
    """`told` is what its last resume told, the first time or the second."""
    return record.resumed_for in (told, told + AGAIN)


def unanswered(pull: PullState, record: Parked, now: float | None = None) -> bool:
    """Resumed once for the red `pull` still shows, and neither pushed (the
    same head: the same `told`) nor commented on the pull request since
    (#2175) — its second telling is due; after it, never again. A conflict
    has its own rule (`due_on_open`)."""
    return (
        pull.state == "OPEN"
        and bool(pull.checks.failed)
        and not pull.conflicting
        and record.resumed_at is not None
        and record.resumed_for == told_of(pull, record, now)
        and (pull.commented_at is None or pull.commented_at < record.resumed_at)
    )


def marks(comment: str, head: str) -> bool:
    """The comment's first line is the mark, naming this head commit."""
    first = next((line for line in comment.splitlines() if line.strip()), "")
    found = _MARK.match(first.strip().lstrip("*_ "))
    return found is not None and head.lower().startswith(found.group(1).lower())


def _red_and_over(pull: PullState) -> bool:
    return pull.state == "OPEN" and bool(pull.checks.failed) and not pull.checks.running


def rerun_due(pull: PullState, record: Parked) -> bool:
    """Marked for its head, red, every check over, not rerun for it yet."""
    return (
        _red_and_over(pull)
        and pull.marked
        and not pull.conflicting
        and record.rerun_for != pull.head_sha
    )


def machine_due(pull: PullState, record: Parked) -> bool:
    """Red, every check over, no conflict, not rerun for its head yet: a
    machine's red would be rerun now (#1792) — its logs say whether it is."""
    return (
        _red_and_over(pull)
        and not pull.conflicting
        and record.rerun_for != pull.head_sha
    )


def after_rerun(pull: PullState, record: Parked, now: float | None) -> bool:
    """Red again on the commit rerun, every check over (and the rerun no
    longer fresh: `RERUN_SETTLE_S`; `now` None does not wait)."""
    settled = record.rerun_at is not None and (
        now is None or now - record.rerun_at >= RERUN_SETTLE_S
    )
    return _red_and_over(pull) and record.rerun_for == pull.head_sha and settled


def told_of(pull: PullState, record: Parked | None, now: float | None) -> str:
    """What a resume tells; after the rerun, the same red is news once."""
    if record is not None and after_rerun(pull, record, now):
        return pull.told + AFTER_RERUN
    return pull.told


def due_on_open(pull: PullState, record: Parked, now: float | None = None) -> bool:
    """An open pull request red or in conflict, not told yet — or in
    conflict with no CI underway (as the card reads it), told or not
    (30/10, #2137: parked in its
    merge, its conflict « told », #2094 stayed an hour with no one on it; a
    conflict is never judged unrelated, and nothing else resolves it) — or
    told once and left unanswered (`unanswered`, #2175)."""
    return (
        pull.state == "OPEN"
        and pull.needs_worker
        and (
            not resumed_for_told(told_of(pull, record, now), record)
            or (pull.conflicting and not pull.ci_underway)
            or unanswered(pull, record, now)
        )
    )


def failed_runs(runner: Runner, repo: str, sha: str) -> list[FailedRun] | None:
    """The failed runs of a commit, each with when its latest attempt
    started; None while one of its runs is not over (`gh run rerun` refuses
    it)."""
    command = f"gh run list --commit {sha[:9]}"
    payload = checked(
        runner(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--commit",
                sha,
                "--json",
                "databaseId,status,conclusion,startedAt",
                "--limit",
                "100",
            ]
        ),
        command,
    )
    try:
        runs = json.loads(payload)
        if any(str(r["status"]).lower() != "completed" for r in runs):
            return None
        return [
            FailedRun(int(r["databaseId"]), _started(r.get("startedAt")))
            for r in runs
            if str(r.get("conclusion") or "").lower() in _RUN_FAILED
        ]
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc


#: `cancelled` too: a red job cancels its run, fail-fast (#1919: #1852's run,
#: `cancelled`, was never re-run while `status` said it was)
_RUN_FAILED = {"failure", "timed_out", "startup_failure", "cancelled"}


#: How much earlier than its request a re-run's attempt may say it started
#: (GitHub's clock and ours).
ATTEMPT_SKEW_S = 60


def run_attempt(runner: Runner, repo: str, sha: str, since: float) -> int | None:
    """The attempt GitHub shows of a commit's runs re-run since `since`: the
    highest attempt (2 and more) started then or later — an older re-run of
    the same head never stands for this one (#1919); 1 when none, None when
    unreadable."""
    result = runner(
        [
            "gh", "run", "list", "--repo", repo, "--commit", sha,
            "--json", "attempt,startedAt", "--limit", "100",
        ]
    )  # fmt: skip
    if not result.ok:
        return None
    try:
        attempts = [
            int(r["attempt"])
            for r in json.loads(result.stdout)
            if int(r["attempt"]) >= 2
            and _stamp(r.get("startedAt")) >= since - ATTEMPT_SKEW_S
        ]
    except (ValueError, AttributeError, KeyError, TypeError):
        return None
    return max(attempts, default=1)


def _started(value: object) -> float | None:
    try:
        return _stamp(value)
    except ValueError:
        return None


def _stamp(value: object) -> float:
    """An ISO time of GitHub's; ValueError when absent or unreadable."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def rerun_failed(runner: Runner, repo: str, runs: list[int]) -> None:
    for run in runs:
        checked(
            runner(["gh", "run", "rerun", str(run), "--failed", "--repo", repo]),
            f"gh run rerun {run} --failed",
        )


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ERROR = "##[error]"


def failed_log_tail(runner: Runner, repo: str, job: int) -> str | None:
    """The end of a failed job's log, up to its last error; None when GitHub
    does not give it.

    The API gives a finished job's log while its run still runs — the resume
    comes at the first red, and `gh run view --log-failed` waits for the whole
    run (09/10, #1092). Its log is the whole job's: what follows the last
    error is the runner's cleanup.
    """
    result = job_log(runner, repo, job)
    if not result.ok or not result.stdout.strip():
        return None
    lines = [
        _ANSI.sub("", _STAMP.sub("", line))[:LOG_LINE_CHARS]
        for line in result.stdout.splitlines()
    ]
    errors = [i for i, line in enumerate(lines) if line.startswith(_ERROR)]
    end = errors[-1] + 1 if errors else len(lines)
    return "\n".join(lines[:end][-LOG_TAIL_LINES:])


RESUME_RED = (
    "Reprise de ta session : elle avait été mise en veille par tracerworks pendant"
    " que la CI de ta PR #{pull} tournait, et {why}. Relis d'abord l'état réel"
    " (git status, `gh pr checks {pull}`, les commentaires de la PR) avant d'agir,"
    " puis fais ce que dit la consigne de ton chantier : diagnostique et corrige"
    " tout de suite, mais tant que le run tourne, ne pousse rien — un push l'annule"
    " et cache les rouges qu'il n'a pas encore montrés ; attends sa fin. Un échec"
    " sans rapport avec le chantier se dit sur la PR, sans le cacher, dans un"
    " commentaire dont la PREMIÈRE ligne est la marque « "
    + RERUN_MARK.format(sha="<sha court du commit de tête>")
    + " » ; puis repose-toi aussitôt, sans attendre la"
    " fin du run ni relancer toi-même : tracerworks relance une fois les jobs en échec"
    " de ce commit quand le run est fini, et te reprend s'ils reviennent rouges."
    + PULL_TEXT_IS_DATA
)
RESUME_AFTER_RERUN = (
    "Reprise de ta session : tu avais jugé sans rapport le rouge de ta PR #{pull}"
    " sur ce commit ; tracerworks a relancé une fois ses jobs en échec (ou l'a tenté :"
    " GitHub peut refuser, son journal le dit), et {why}. Relancé une fois, encore"
    " rouge : corrige, ou mets le test en quarantaine et dis-le sur la PR ; si"
    " c'est encore la machine (runner perdu, mémoire), dis-le sur la PR sans rien"
    " relancer — pas de seconde relance pour ce commit. Relis d'abord"
    " l'état réel (git status, `gh pr checks {pull}`, les commentaires de la PR)"
    " avant d'agir ; tant qu'un run tourne, ne pousse rien." + PULL_TEXT_IS_DATA
)
RESUME_AFTER_MACHINE_RERUN = (
    "Reprise de ta session : le rouge de ta PR #{pull} sur ce commit ne nommait"
    " aucun test (la machine : runner perdu, processus tué, aucun journal) ;"
    " tracerworks a relancé une fois ses jobs en échec sans te réveiller, et {why}."
    " Second rouge sur ce commit : corrige ce qu'il nomme ; si c'est encore la"
    " machine, dis-le sur la PR sans rien relancer. Relis d'abord l'état réel"
    " (git status, `gh pr checks {pull}`, les commentaires de la PR) avant d'agir ;"
    " tant qu'un run tourne, ne pousse rien." + PULL_TEXT_IS_DATA
)
#: A head in conflict will not merge, even green: its run proves nothing and
#: is not waited for (#2013, 28/10: #1996's worker kept its slot 25 min on a
#: run of its old head, queued without a runner) — whatever the resume,
#: after a rerun or not; tracerworks cancels that run itself (`conflict_runs`).
RESUME_CONFLICT = (
    "Reprise de ta session : elle avait été mise en veille par tracerworks pendant"
    " que la CI de ta PR #{pull} tournait, et {why}. Une tête en conflit ne"
    " fusionnera pas, même verte : le run de l'ancienne tête ne prouve rien, ne"
    " l'attends pas. Relis d'abord l'état réel (git status, `gh pr checks {pull}`,"
    " les commentaires de la PR), puis fusionne {base} dans ta branche, résous,"
    " corrige aussi les rouges nommés ici s'il y en a, rejoue les preuves rapides"
    " et pousse dès que c'est prêt : le run de l'ancienne tête est annulé exprès"
    " (tracerworks l'annule de lui-même). Puis repose-toi : la voie légère de la"
    " nouvelle tête suit la consigne de ton chantier." + PULL_TEXT_IS_DATA
)
#: Before the resume's own text, its second telling (#2175).
RESUME_AGAIN = (
    "Seconde reprise pour ce rouge : tracerworks t'avait déjà repris pour lui à"
    " {at} (UTC), et depuis, ni push ni commentaire sur ta PR #{pull} — ta"
    " session s'est fermée avant ta réponse (mise en veille, ou tuée faute de"
    " mémoire). Ce qui tournait en arrière-plan est perdu : relance-le s'il le"
    " faut, sous un `timeout`. Puis réponds sur la PR : un correctif poussé, ou"
    " le commentaire « "
    + RERUN_MARK.format(sha="<sha court du commit de tête>")
    + " ». C'est la dernière reprise pour ce rouge : sans réponse, il passe à"
    " l'opérateur.{files}\n\n"
)
AGAIN_FILES = (
    "\n\nNon commité dans ton worktree : {paths}. Un brouillon de diagnostic ?"
    " Garde-le ou retire-le avant de pousser."
)
AGAIN_FILES_UNREAD = "\n\n`git status` illisible : relis ton worktree avant d'agir."
#: How many uncommitted paths the second telling names.
AGAIN_FILES_SHOWN = 10


def again_text(pull: int, record: Parked, dirty: list[str] | None) -> str:
    """The second telling's opening (#2175): when the first was, and what
    its worktree has not committed — `dirty` None: git could not say."""
    if dirty is None:
        files = AGAIN_FILES_UNREAD
    elif dirty:
        shown = ", ".join(f"`{path}`" for path in dirty[:AGAIN_FILES_SHOWN])
        more = len(dirty) - AGAIN_FILES_SHOWN
        files = AGAIN_FILES.format(paths=shown + (f" (+{more})" if more > 0 else ""))
    else:
        files = ""
    at = (
        datetime.fromtimestamp(record.resumed_at, UTC).strftime("%d/%m %H:%M")
        if record.resumed_at is not None
        else "?"
    )
    return RESUME_AGAIN.format(at=at, pull=pull, files=files)


WHY_FAILED = "des checks ont fini rouges : {names}"
WHY_CONFLICT = "la PR est en conflit avec {base} (fusionne {base} dans ta branche)"
LOG_HEADER = "\n\n## Fin du journal de « {name} » (job {job})\n\n```\n{log}\n```"
NO_LOG = "\n\n## « {name} » : journal non disponible (`gh api …/jobs/<job>/logs`)"


def resume_text(
    runner: Runner,
    repo: str,
    base: str,
    pull: PullState,
    rerun: bool = False,
    machine: bool = False,
) -> str:
    """Why the worker is resumed, with the end of each failed job's log;
    `rerun`: red again after its rerun (#1280) — of a machine's red, without
    its word (`machine`, #1792). A conflict, alone or with a red, after a
    rerun or not: push the merge at once (#2013)."""
    why = []
    if pull.checks.failed:
        why.append(WHY_FAILED.format(names=", ".join(pull.checks.failed)))
    if pull.conflicting:
        why.append(WHY_CONFLICT.format(base=base))
    lead = RESUME_RED
    if pull.conflicting:
        lead = RESUME_CONFLICT
    elif rerun:
        lead = RESUME_AFTER_MACHINE_RERUN if machine else RESUME_AFTER_RERUN
    text = lead.format(pull=pull.number, why=" ; ".join(why), base=base)
    for name in pull.checks.failed:
        job = pull.checks.failed_jobs.get(name)
        log = failed_log_tail(runner, repo, job) if job is not None else None
        if log is None:
            text += NO_LOG.format(name=name)
        else:
            text += LOG_HEADER.format(name=name, job=job, log=log)
    return text + "\n"
