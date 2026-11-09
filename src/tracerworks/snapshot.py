"""The factory snapshot: everything the dashboard shows, as one JSON structure (#806).

Built from the same readings as `status` and `start-next` — tmux, git, `gh` —
at each call, nothing stored. All the logic of the dashboard lives here; the
page only draws what this returns.

A value GitHub could not give is `null`, never an empty list: « no ticket »
and « could not read the tickets » must not look alike. `github.issues` says
whether the open-issue list was read at all.

The ticket a worker is on is DECLARED when one open ticket of its chantier
carries the project's progress label; otherwise it is DEDUCED — the head of
the frontier, by the rule `start-next` follows (`frontier.head_of`).
"""

from __future__ import annotations

import functools
import re
import time
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracerworks import (
    chantier_state,
    level,
    low_memory,
    machine_red,
    merge_queue,
    order,
    store,
    texts,
)
from tracerworks.cleanup import merged_pull_request
from tracerworks.control import Memo, build_control
from tracerworks.disk import filesystem
from tracerworks.frontier import BodyRules, Issue, Queue, head_of, parse_issues
from tracerworks.git import pushed_tickets
from tracerworks.github import (
    CLOSED_LIMIT,
    OPEN_FIELDS,
    closed_issues_payload,
    is_blocked,
    last_comment,
)
from tracerworks.github import open_issues_payload as read_open_issues
from tracerworks.guards import Admission, admission, next_chantier, no_candidate
from tracerworks.history import build_history, events_of_issues, recent
from tracerworks.measures import red_runs
from tracerworks.natures import Nature
from tracerworks.observe import (
    HALTED,
    Worker,
    WorkerState,
    available_memory_mb,
    observe_workers,
)
from tracerworks.parking import Parked, readable_parked, run_attempt
from tracerworks.process import CommandError, Runner
from tracerworks.quotas import (
    Fetch,
    actions_quota,
    claude_quota,
    fetch_billing,
    included_minutes,
)
from tracerworks.reaper_log import last_pass
from tracerworks.relauncher import next_pass
from tracerworks.report import describe_saved, parked_of
from tracerworks.runtime.tmux import Tmux
from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime
from tracerworks.settings import Settings, Waits
from tracerworks.state_file import State
from tracerworks.trends import Slope, month_bounds, projection, slope_said, slopes
from tracerworks.usage import chantier_usage

#: How much of a spec or ticket the side panel shows, at most.
EXCERPT_CHARS = 1500  # a Problem statement and the start of the Solution


def excerpt(body: str) -> str:
    """The start of an issue, AS WRITTEN — no model, no rendering: free,
    instant, faithful. A long body is cut at its last paragraph break within
    `EXCERPT_CHARS` if one falls in its last two thirds, else its last line
    break, else its last space — without a heading left dangling at the end."""
    text = body.strip()
    if len(text) <= EXCERPT_CHARS:
        return text
    head = text[:EXCERPT_CHARS]
    for mark in ("\n\n", "\n", " "):
        at = head.rfind(mark)
        if at > EXCERPT_CHARS // 3:
            head = head[:at]
            break
    lines = head.rstrip().split("\n")
    while len(lines) > 1 and (
        not lines[-1].strip() or lines[-1].lstrip().startswith("#")
    ):
        lines.pop()
    return "\n".join(lines).rstrip()


#: What the dashboard calls each state (derived, never stored). A worker at
#: rest (WAITING) is told apart by `_at_rest` (#830): « blocked » only when it
#: asked the operator (needs-human), else « ci » (its CI underway), « done » (its pull
#: request merged) or « idle ».
SHOWN_STATE = {
    WorkerState.WORKING: "active",
    WorkerState.WAITING: "idle",
    WorkerState.ASKING: "asking",  # on a permission prompt (#984)
    WorkerState.STOPPED: "stopped",
    WorkerState.DEAD: "dead",
}
CI_UNDERWAY = ("running", "queued")


def build_snapshot(
    runner: Runner,
    settings: Settings,
    tmux: Tmux,
    state: State,
    now: float,
    transcripts: Path,
    memo: Memo | None = None,
    billing: Fetch | None = None,
    cpu: tuple[float, float] | None = None,
) -> dict[str, Any]:
    repo = settings.github_repo
    rules = BodyRules(settings.parent_keywords, settings.blocker_keywords)
    # The GitHub reads are slow (the closed list alone takes seconds): issues,
    # CI and history side by side, while tmux and git are asked here.
    with ThreadPoolExecutor(max_workers=4) as pool:
        reading_issues = pool.submit(_issues, runner, repo, rules)
        reading_control = pool.submit(
            build_control,
            runner,
            repo,
            settings.conventions,
            settings.runner_name,
            memo,
            now,
        )
        reading_history = pool.submit(
            build_history,
            runner,
            repo,
            settings.deploy_workflow,
            settings.relauncher_log,
            settings.conventions.spec_of_window,
            now,
        )
        sessions_readable = True
        try:
            workers = observe_workers(runner, settings, tmux)
        except CommandError:
            sessions_readable = False
            workers = observe_workers(runner, settings, tmux, known_windows=[])
        parked, _ = readable_parked(settings)
        if sessions_readable:
            recorded, room = _order(settings, state, workers, parked, now)
        else:
            recorded, room = None, texts.SESSIONS_UNREADABLE
        admitting = pool.submit(_admission, runner, settings, tmux, state, room, now)
        history = reading_history.result()
        open_issues, closed = reading_issues.result()
        control = reading_control.result()
    posts = _posts(settings, open_issues, closed, {w.spec for w in workers})
    pulls = {p["branch"]: p for p in control["pull_requests"] or []}
    conflicting = {p["number"] for p in pulls.values() if p.get("conflicting")}
    queue = merge_queue.readable_view(settings)
    moving = chantier_state.queue_states(queue)
    issue_events, issues_unknown = events_of_issues(
        open_issues,
        None if closed is None else [i for c in closed.values() for i in c],
        settings.ready_label,
        lambda n: f"https://github.com/{repo}/issues/{n}",
    )
    shown_workers = [
        {
            **_with_phase(
                _worker(
                    runner, settings, open_issues, closed, pulls, w, transcripts, parked
                ),
                w, parked, recorded, moving, conflicting,
            ),
            **order.chantier_json(recorded, w.spec),
        }
        for w in workers
    ]  # fmt: skip
    if not sessions_readable:
        for worker in shown_workers:
            worker.update(
                state="unknown",
                phase=None,
                phase_text=texts.SESSIONS_UNREADABLE,
                session_occupied=None,
                session_hold_reason=None,
            )
    said = {w["spec"]: w["phase_text"] for w in shown_workers}
    if control["pull_requests"]:
        reruns, reds = _red_history(settings)
        control = {
            **control,
            "pull_requests": [
                {
                    **p,
                    **_red_hold(p, workers, parked, reruns, now, settings.waits),
                    # its CI runs ended red, every commit, every attempt (#2058)
                    "reds": reds.get(p["branch"], 0),
                }
                for p in control["pull_requests"]
            ],
        }
    return {
        "generated_at": now,
        "repo": repo,
        "max_workers": state.max_workers,
        "sessions_readable": sessions_readable,
        "github": {
            "issues": "unknown" if open_issues is None else "ok",
            "closed": "unknown" if closed is None else "ok",
        },
        "vps": {
            **_vps(runner, settings, state, now, billing or fetch_billing),
            # measured by serve's history cycle (#836); unknown elsewhere
            "cpu_percent": cpu[1] if cpu else None,
            "cpu_at": cpu[0] if cpu else None,
        },
        "workers": shown_workers,
        **order.view_json(recorded, said),
        # the merge queue (#1714), from the history base; None: unreadable
        "merge_queue": merge_queue.as_json(queue),
        # the machine's reds re-run of the last hours, requested or re-run as
        # GitHub shows them (#1792, #1919); None: the history base unreadable
        "machine_reruns": _machine_reruns(runner, settings, now),
        **_ranked(_queued_phase(posts, admitting.result()), recorded),
        "relauncher": _relauncher(
            runner, settings, admitting.result(), open_issues, workers, state
        ),
        # the queue paused from the Settings tab (#2192): said at the top
        "queue_paused": state.queue_paused,
        "control": control,
        # how long a red nobody holds waits before the banner says it (#2057)
        "red_unowned_minutes": settings.waits.red_unowned_minutes,
        # the memory low for long (#2096): since when, the largest processes
        # now; None while not low, or unknown — the banner says it past this
        "low_memory": _low_memory(settings, now),
        "low_memory_minutes": settings.waits.low_memory_minutes,
        "delivered": history["delivered"],
        "events": recent([*history["events"], *issue_events], now),
        "events_unknown": sorted([*history["events_unknown"], *issues_unknown]),
    }


def _low_memory(settings: Settings, now: float) -> dict[str, Any] | None:
    since = low_memory.streak(settings.history_db, now)
    if since is None:
        return None
    top = low_memory.largest(settings.proc, settings.conventions)
    return {"since": since, "top": top}


def _machine_reruns(
    runner: Runner, settings: Settings, now: float
) -> list[dict[str, Any]] | None:
    repo = settings.github_repo
    try:
        with closing(store.connect(settings.history_db)) as db:
            shown = machine_red.recent(
                db, repo, now, functools.partial(run_attempt, runner, repo)
            )
    except merge_queue.BASE_ERRORS:
        return None
    return [{"pull": s.pull, "text": s.text} for s in shown]


def _red_history(settings: Settings) -> tuple[dict[int, int], dict[str, int]]:
    """The machine's re-runs by pull request, the red runs by branch."""
    try:
        with closing(store.connect(settings.history_db)) as db:
            return (
                machine_red.rerun_times(db, settings.github_repo),
                red_runs(db, settings.github_repo, settings.ci_workflow),
            )
    except merge_queue.BASE_ERRORS:
        return {}, {}  # unreadable: no re-run holds a red, no red counted


@dataclass(frozen=True)
class Red:
    """What a pull request's red CI is held by (#2057); None: not so."""

    at: float | None  # its CI ended red then
    rerun_at: float | None = None  # a machine's re-run asked then (#1792)
    worker: WorkerState | None = None  # None: no worker
    parked_at: float | None = None  # parked then, with a session to resume
    rested_at: float | None = None  # its worker's last turn ended then


def red_holder(
    ci: str, red: Red, now: float, waits: Waits
) -> tuple[str | None, float | None]:
    """Who holds a pull request's red CI (#2057), and since when nobody does.
    A machine's re-run asked after the red (#1792), until GitHub had time to
    run it (`red_unowned_minutes`); its parked worker, until its resume
    deadline (#1730, `parked_red_minutes`); its worker at work. Else nobody:
    since the red, or since what came after (its worker's rest, its parking,
    the re-run asked). Not red: nothing to hold."""
    if ci != "failure":
        return None, None
    after = [red.at] if red.at is not None else []
    if red.rerun_at is not None and red.at is not None and red.at <= red.rerun_at:
        if now - red.rerun_at < waits.red_unowned_minutes * 60:
            return "machine", None
        after.append(red.rerun_at)  # asked, never run: nobody's since
    if red.parked_at is not None:
        parked = max([red.parked_at, *after])
        if now - parked < waits.parked_red_minutes * 60:
            return "parked", None
        after.append(parked)
    if red.worker is WorkerState.WORKING:
        return "worker", None
    if red.rested_at is not None:
        after.append(red.rested_at)
    return None, max(after, default=None)


def _red_hold(
    pull: dict[str, Any],
    workers: Sequence[Worker],
    parked: Mapping[int, Parked],
    reruns: Mapping[int, int],
    now: float,
    waits: Waits,
) -> dict[str, Any]:
    """The facts of a pull request's red: its worker is the one on its
    branch, else the one parked on it; a worker's rest is its last turn's
    end, never its window's activity (any output refreshes it)."""
    number = pull["number"]
    worker, record = None, None
    for w in workers:
        record = parked_of(w, parked)
        on_branch = w.worktree is not None and w.worktree.branch == pull["branch"]
        if on_branch or (record is not None and record.pull == number):
            worker = w
            break
    resumable = worker is not None and record is not None and record.session
    owner, since = red_holder(
        pull["ci"],
        Red(
            at=pull.get("ci_red_at"),
            rerun_at=reruns.get(number),
            worker=worker.state if worker else None,
            parked_at=record.parked_at if resumable and record else None,
            rested_at=worker.rest.at if worker and worker.rest else None,
        ),
        now,
        waits,
    )
    return {"red_owner": owner, "red_unowned_since": since}


def _open_issues(runner: Runner, repo: str, rules: BodyRules) -> list[Issue] | None:
    try:
        return parse_issues(read_open_issues(runner, repo, OPEN_FIELDS), rules)
    except (CommandError, ValueError, KeyError, TypeError):
        return None


Closed = dict[int, list[Issue]]


def _order(
    settings: Settings,
    state: State,
    workers: list[Worker],
    parked: Mapping[int, Parked],
    now: float,
) -> tuple[order.Factory | None, str | None]:
    """The factory's order as recorded (#1823) — what the page shows, as
    `status` reads it — and `start-next`'s rule, its entries made in memory
    only: the page reads, the pass writes. An unreadable base: the order
    unknown (None), never room."""
    try:
        base = order.recorded(settings)
    except order.BASE_ERRORS as exc:
        return None, texts.ORDER_UNREADABLE.format(error=exc)
    for spec in order.stale(base.aside, workers):
        del base.aside[spec]  # `start-next` forgets it first
    found, _ = order.arrange(
        workers, parked, base.aside, base.entered, settings.in_flight,
        state.max_workers, now, base.place_waits,
    )  # fmt: skip
    return found, found.why_full()


def _ranked(posts: dict[str, Any], found: order.Factory | None) -> dict[str, Any]:
    """The queue's specs ranked after the chantiers in flight, in the order
    `start-next` takes them (#1823); no rank while the order is unknown. A
    spec waiting for a place (#2118) heads it, taken from wherever it was."""
    if posts.get("queue") is not None:
        posts = _waits_first(posts, found)
    for i, spec in enumerate(posts.get("queue") or []):
        spec["rank"] = None if found is None else found.in_flight + i + 1
    return posts


def _waits_first(posts: dict[str, Any], found: order.Factory | None) -> dict[str, Any]:
    """The specs waiting for a place, the next to start first, before the
    queue — out of Waiting on GitHub. One with a worktree has its worker
    card (`chantier_json`); one not open any more is not shown."""
    if found is None:
        return posts
    groups = posts.get("waiting") or {}
    by_number = {
        i["number"]: i
        for i in [*posts["queue"], *(i for g in groups.values() for i in g)]
    }
    head = [
        {**by_number[s], "place_wait": found.place_wait(s)}
        for s in found.waiting
        if s in by_number
    ]
    shown = {i["number"] for i in head}
    rest = [
        {**i, "place_wait": None} for i in posts["queue"] if i["number"] not in shown
    ]
    if posts.get("waiting") is not None:
        posts = {
            **posts,
            "waiting": {
                group: [i for i in issues if i["number"] not in shown]
                for group, issues in groups.items()
            },
        }
    return {**posts, "queue": [*head, *rest]}


def _admission(
    runner: Runner,
    settings: Settings,
    tmux: Tmux,
    state: State,
    room: str | None,
    now: float,
) -> Admission:
    launcher = TmuxWorktreeRuntime(runner, settings, tmux, time.sleep, lambda: now)
    return admission(
        runner,
        settings,
        state,
        now,
        launcher.launch_blocker,
        over_cap=False,
        room=room,
    )


def _relauncher(
    runner: Runner,
    settings: Settings,
    admitted: Admission,
    open_issues: list[Issue] | None,
    workers: list[Worker],
    state: State,
) -> dict[str, Any]:
    """The queue's next pass (#869): when the timer runs `start-next`, and
    why it would launch nothing — `start-next`'s own decision, in its order
    (room first, then a chantier), never a copy. Issues unread: the Queue
    column says « unknown » already."""
    refusal = None if admitted.provider else admitted.reason
    if state.queue_paused:  # start-next's first refusal (#2192)
        refusal = texts.QUEUE_PAUSED
    if refusal is None and open_issues is not None:
        candidate, _ = next_chantier(runner, settings, open_issues, workers)
        refusal = None if candidate else no_candidate(settings)
    return {
        "next_at": next_pass(runner, settings.relauncher_timer),
        "refusal": refusal,
    }


def _issues(
    runner: Runner, repo: str, rules: BodyRules
) -> tuple[list[Issue] | None, Closed | None]:
    """The open issues, then the closed tickets created since the oldest open
    spec (unbounded when a spec's date is unknown)."""
    opened = _open_issues(runner, repo, rules)
    specs = [i for i in opened or [] if i.parent is None]
    dates = [i.created_at for i in specs]
    since = None
    if specs and all(d is not None for d in dates):
        oldest = min(d for d in dates if d is not None)
        since = datetime.fromtimestamp(oldest, UTC).strftime("%Y-%m-%d")
    return opened, _closed_by_parent(runner, repo, rules, since)


def _closed_by_parent(
    runner: Runner, repo: str, rules: BodyRules, since: str | None
) -> Closed | None:
    """Closed tickets by the spec they name; None when unreadable, or when the
    list reached its limit (some may be missing: unknown, not wrong)."""
    payload = closed_issues_payload(runner, repo, since)
    if payload is None:
        return None
    try:
        issues = parse_issues(payload, rules)
    except (ValueError, KeyError, TypeError):
        return None
    if len(issues) >= CLOSED_LIMIT:
        return None
    by_parent: Closed = {}
    for issue in issues:
        if issue.parent is not None:
            by_parent.setdefault(issue.parent, []).append(issue)
    return by_parent


def _posts(
    settings: Settings,
    open_issues: list[Issue] | None,
    closed: Closed | None,
    in_workshop: set[int],
) -> dict[str, Any]:
    """File, then En attente GitHub. The queue: the ready specs without a worker
    carrying the launch label, in `start-next`'s order (`frontier.Queue`).
    Waiting: every other open issue outside the workshop, grouped by nature
    (#857) — an issue naming an open parent counted under it, never listed
    apart: the worker cards and the queue's fractions count it alike. The
    operator's starred issues first in their group (#1875) — never in the
    queue: the star is his pick, not an order."""
    if open_issues is None:
        return {"waiting": None, "queue": None}
    queue = Queue(
        frozenset({settings.ready_label, settings.launch_label}),
        settings.priority_label,
    )
    queued = sorted(
        (
            i
            for i in open_issues
            if settings.ready_label in i.labels
            and i.parent is None
            and i.number not in in_workshop
            and queue.admits(i)
        ),
        key=queue.rank,
    )
    open_numbers = {i.number for i in open_issues}

    def open_tickets(issue: Issue) -> list[Issue]:
        return [i for i in open_issues if i.parent == issue.number]

    def starred(issue: Issue) -> bool:
        return settings.star_label in issue.labels

    def labels(issue: Issue) -> list[str]:
        """Its labels but the star, which `is_starred` says (#1875)."""
        return sorted(issue.labels - {settings.star_label})

    def entry(spec: Issue) -> dict[str, Any]:
        return {
            "number": spec.number,
            "title": spec.title,
            "excerpt": excerpt(spec.body),
            "labels": labels(spec),
            "prioritized": settings.priority_label in spec.labels,
            "is_starred": starred(spec),
            "created_at": spec.created_at,
            **_tickets(open_tickets(spec), closed, spec.number, None),
        }

    def row(issue: Issue) -> dict[str, Any]:
        return {
            "number": issue.number,
            "title": issue.title,
            "labels": labels(issue),
            "is_starred": starred(issue),
            "created_at": issue.created_at,
            "open_tickets": len(open_tickets(issue)),
        }

    gone = in_workshop | {i.number for i in queued}
    groups: dict[str, list[Issue]] = {group: [] for group in _GROUPS.values()}
    for issue in sorted(open_issues, key=lambda i: (not starred(i), -i.number)):
        if issue.number in gone or issue.parent in open_numbers:
            continue
        groups[_GROUPS[settings.natures.of(issue)]].append(issue)

    def spec_row(spec: Issue) -> dict[str, Any]:
        """A spec ready, without the launch label, with tickets done and
        tickets open: a tranche merged, the next one waits for the operator's
        word (#1926 — `clean` took the label off when it kept it open)."""
        shown = {**entry(spec), "open_tickets": len(open_tickets(spec))}
        shown["waits_launch"] = (
            settings.ready_label in spec.labels
            and settings.launch_label not in spec.labels
            and shown["open_tickets"] > 0
            and (shown.get("done") or 0) > 0
        )
        return shown

    waiting = {
        group: [spec_row(i) if group == "specs" else row(i) for i in issues]
        for group, issues in groups.items()
    }
    return {"waiting": waiting, "queue": [entry(s) for s in queued]}


#: Waiting on GitHub's groups, in their order. A ticket whose parent is not
#: open has nothing to sit under: to sort, never lost.
_GROUPS: dict[Nature | None, str] = {
    "spec": "specs",
    "bug": "bugs",
    "task": "tasks",
    "idea": "ideas",
    "ticket": "to_sort",
    None: "to_sort",
}


def _tickets(
    opened: Sequence[Issue],
    closed: Closed | None,
    spec: int,
    current: Issue | None,
    pushed: Collection[int] = (),
) -> dict[str, Any]:
    """The ticket line and the fraction done; null when closed tickets are
    unknown — never a fraction that forgets some. A ticket is done once
    closed, or once its commit `(#n)` is pushed (#1729): the « 0/5 » of 09/10
    was five tickets coded, open until their pull request merges."""
    if closed is None:
        return {"tickets": None, "done": None, "total": None}
    done = [*closed.get(spec, []), *(i for i in opened if i.number in pushed)]
    todo = [i for i in opened if i.number not in pushed]
    return {
        "tickets": _ticket_line(todo, done, current),
        "done": len(done),
        "total": len(todo) + len(done),
    }


def _worker(
    runner: Runner,
    settings: Settings,
    open_issues: list[Issue] | None,
    closed: Closed | None,
    pulls: dict[str, dict[str, Any]],
    worker: Worker,
    transcripts: Path,
    parked: Mapping[int, Parked],
) -> dict[str, Any]:
    repo = settings.github_repo
    branch = worker.worktree.branch if worker.worktree else None
    pull = pulls.get(branch) if branch else None
    shown: dict[str, Any] = {
        "spec": worker.spec,
        "session_occupied": worker.running,
        "session_hold_reason": (
            describe_saved(worker)
            if worker.running
            and (worker.saved is None or not worker.saved.everything_pushed)
            else None
        ),
        "title": None,
        "excerpt": None,
        "state": SHOWN_STATE[worker.state],
        "asking": worker.asking,
        "pending_field": worker.pending,  # at rest, never submitted (#2003)
        "provider": worker.provider,
        "branch": branch,
        "pull_request": {"number": pull["number"], "ci": pull["ci"]} if pull else None,
        "current_ticket": None,
        "blocked_by": None,
        "tickets": None,
        "done": None,
        "total": None,
        "last_activity": worker.last_activity,
        **_usage(settings, worker, transcripts),
    }
    if worker.state is WorkerState.WAITING:
        shown["state"] = "ci" if pull and pull["ci"] in CI_UNDERWAY else "idle"
    record = parked_of(worker, parked)
    if record is not None:
        # its session closed while its CI runs (#1025): holds no slot
        shown["state"] = "parked"
        shown["parked"] = {"pull": record.pull, "session": record.session}
    if open_issues is None:
        return _at_rest(runner, repo, worker, branch, pull, shown)
    spec = next((i for i in open_issues if i.number == worker.spec), None)
    shown["title"] = spec.title if spec else None
    shown["excerpt"] = excerpt(spec.body) if spec else None
    opened = [i for i in open_issues if i.parent == worker.spec]
    current, declared = _current(runner, repo, settings, open_issues, opened)
    if current is not None:
        shown["current_ticket"] = {
            "number": current.number,
            "title": current.title,
            "declared": declared,
        }
    if worker.state in HALTED:  # a question stays shown under a prompt (#984)
        shown["blocked_by"] = _blocked_by(runner, repo, settings, spec, opened)
    pushed = (
        pushed_tickets(runner, worker.worktree.path, settings.base_ref)
        if worker.worktree is not None
        else set()
    )
    shown.update(_tickets(opened, closed, worker.spec, current, pushed))
    return _at_rest(runner, repo, worker, branch, pull, shown)


_LOW_MEMORY = texts.LOW_MEMORY.split("{")[0]


def _queued_phase(posts: dict[str, Any], admitted: Admission) -> dict[str, Any]:
    """The queued specs in the chantier's words (#1729): « queued », and why
    when the machine holds them — the memory of 18/10."""
    if posts["queue"] is None:
        return posts
    memory = any(b.startswith(_LOW_MEMORY) for b in admitted.blocked)
    state = chantier_state.derive(chantier_state.Facts(waits_for_memory=memory))
    phase = {
        "phase": state.phase.key,
        "phase_text": state.describe(),
        "phase_reason": state.reason,
        "phase_cause": state.cause,
    }
    return {**posts, "queue": [{**entry, **phase} for entry in posts["queue"]]}


def _with_phase(
    shown: dict[str, Any],
    worker: Worker,
    parked: Mapping[int, Parked],
    shop: order.Factory | None,
    moving: Mapping[int, str],
    conflicting: Collection[int] = (),
) -> dict[str, Any]:
    """The chantier's state (#1729), from the facts `status` reads and those
    only the snapshot has: its CI, `needs-human`, its merge, its conflict."""
    pull = shown["pull_request"]
    facts = chantier_state.worker_facts(
        worker,
        parked,
        shop,
        moving,
        pull["number"] if pull else None,
        ci_underway=bool(pull) and pull["ci"] in CI_UNDERWAY,
        ci_red=bool(pull) and pull["ci"] == "failure",
        needs_human=(shown["blocked_by"] or {}).get("number"),
        merged=pull["number"] if pull and pull["ci"] == "merged" else None,
        conflicting=bool(pull) and pull["number"] in conflicting,
    )
    state = chantier_state.derive(facts)
    return {
        **shown,
        "phase": state.phase.key,
        "phase_text": state.describe(),
        "phase_reason": state.reason,
        "phase_cause": state.cause,
    }


def _at_rest(
    runner: Runner,
    repo: str,
    worker: Worker,
    branch: str | None,
    pull: dict[str, Any] | None,
    shown: dict[str, Any],
) -> dict[str, Any]:
    """A worker at rest (#830): « blocked » only when it asked the operator; then
    « ci » while its CI runs; « done » once its pull request is merged; else
    « idle ». A merge that cannot be read is « idle », never « done »."""
    if worker.state is not WorkerState.WAITING:
        return shown
    if shown["blocked_by"] is not None:
        shown["state"] = "blocked"
    elif shown["state"] != "ci" and pull is None and branch:
        try:
            merged = merged_pull_request(runner, repo, branch)
        except CommandError:
            merged = None
        if merged is not None:
            shown["state"] = "done"
            shown["pull_request"] = {"number": merged, "ci": "merged"}
    return shown


def _current(
    runner: Runner,
    repo: str,
    settings: Settings,
    open_issues: Sequence[Issue],
    opened: Sequence[Issue],
) -> tuple[Issue | None, bool]:
    ordered = sorted(opened, key=lambda i: i.number)
    declared = next((i for i in ordered if settings.progress_label in i.labels), None)
    if declared is not None:
        return declared, True
    open_numbers = {i.number for i in open_issues}

    def blocked(ticket: int, unlisted: list[int]) -> bool:
        return is_blocked(runner, repo, ticket, unlisted)

    return head_of(ordered, open_numbers, blocked), False


def _blocked_by(
    runner: Runner,
    repo: str,
    settings: Settings,
    spec: Issue | None,
    opened: Sequence[Issue],
) -> dict[str, Any] | None:
    """The first issue of the chantier — spec or open ticket — whose worker
    asked the operator, and the first line of its last comment."""
    asking = sorted(
        (
            i
            for i in (*([spec] if spec else []), *opened)
            if settings.human_label in i.labels
        ),
        key=lambda i: i.number,
    )
    if not asking:
        return None
    return {
        "number": asking[0].number,
        "reason": last_comment(runner, repo, asking[0].number, settings.trusted_logins),
    }


def _ticket_line(
    opened: Sequence[Issue], closed: Sequence[Issue], current: Issue | None
) -> list[dict[str, Any]]:
    line = [(i, "done") for i in closed]
    line += [
        (i, "current" if current is not None and i.number == current.number else "todo")
        for i in opened
    ]
    return [
        {
            "number": i.number,
            "title": i.title,
            "status": status,
            "excerpt": excerpt(i.body),
            "closed_at": i.closed_at,
        }
        for i, status in sorted(line, key=lambda pair: pair[0].number)
    ]


def _usage(settings: Settings, worker: Worker, transcripts: Path) -> dict[str, Any]:
    """Since when the chantier runs and what it cost — from the provider's own
    session logs of its worktree (`usage.py`); null when there are none."""
    worktree = settings.conventions.worktree_path(worker.spec)
    usage = chantier_usage(transcripts, worktree)
    span = usage.span() if usage else None
    # its model and effort, asked at its last start and seen in its log (#2252)
    asked = level.recorded(settings.cache_dir, worker.spec)
    return {
        "started_at": int(span[0].timestamp()) if span else None,
        "tokens": usage.total().total if usage else None,
        **level.view(asked, usage.seen() if usage else (None, None)),
    }


_MEM_TOTAL = re.compile(r"^MemTotal:\s+(\d+) kB", re.MULTILINE)


def _vps(
    runner: Runner, settings: Settings, state: State, now: float, billing: Fetch
) -> dict[str, Any]:
    available_kb, total_kb = _disk(runner, settings.worktree_root)
    actions_slope, claude_slope = slopes(
        settings.history_db,
        now,
        settings.actions_slope_days,
        settings.claude_week_slope_days,
        settings.claude_week_half_life_hours,
        settings.actions_half_life_hours,
    )
    return {
        "memory_available_mb": available_memory_mb(settings.meminfo),
        "memory_total_mb": memory_total_mb(settings.meminfo),
        "disk_available_kb": available_kb,
        "disk_total_kb": total_kb,
        "cleanup": last_pass(settings.reaper_log),
        "quota": {
            "provider": state.provider,
            "used_percent": state.claude_weekly_used,
            "cap_percent": state.claude_weekly_cap,
        },
        "claude_quota": _claude_quota(settings, now, claude_slope),
        "actions_quota": _actions_quota(settings, state, now, billing, actions_slope),
    }


def _actions_quota(
    settings: Settings, state: State, now: float, billing: Fetch, slope: Slope | None
) -> dict[str, Any]:
    """The month's minutes, and where they are heading (#848, #864): the
    Tendances rule, from the reading's own time."""
    read = actions_quota(
        settings.billing_token_file,
        settings.github_repo.split("/", 1)[0],
        included_minutes(settings, state),  # the Settings tab's, read each build
        settings.cache_dir / "actions-quota.json",
        now,
        billing,
    )
    used, at = read.get("used_minutes"), read.get("read_at")
    end = month_bounds(at if at is not None else now)[1]
    projected = None if used is None or at is None else projection(used, at, end, slope)
    return {
        **read,
        "projected_minutes": projected,
        "slope": slope_said(slope),
    }


def _claude_quota(
    settings: Settings, now: float, slope: Slope | None
) -> dict[str, Any]:
    """What the workers' status line last saw (#822); when nothing was, say
    whether the status line is even given to them."""
    read = claude_quota(settings.claude_quota_file, now)
    if week := read["seven_day"]:  # where the week is heading (#848, #864)
        week["projected_percent"] = projection(
            week["used_percent"], week["seen_at"], week["resets_at"], slope
        )
        week["slope"] = slope_said(slope)
    if read["error"] and not settings.claude_statusline:
        read["error"] += texts.STATUSLINE_OFF
    return read


def memory_total_mb(meminfo: Path) -> int | None:
    try:
        match = _MEM_TOTAL.search(meminfo.read_text())
    except OSError:
        return None
    return int(match.group(1)) // 1024 if match else None


def _disk(runner: Runner, path: Path) -> tuple[int | None, int | None]:
    """The filesystem the worktrees live on; unknown on any failure."""
    found = filesystem(runner, path)
    return (found.available_kb, found.total_kb) if found else (None, None)
