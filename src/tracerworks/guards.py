"""When no new worker may start (architecture §10). A running one is never killed.

- room in the factory's order (`order`, #1823): fewer than `in_flight`
  chantiers in flight and a free session of `max_workers` — a dead window
  still holds its session until it is stopped (someone has to look at it), a
  parked one holds its place in flight;
- the self-hosted runner unreadable: « unknown » is never « free »;
- available memory under `min_available_memory_mb` — or unreadable; while the
  runner runs a CI the bar is `min_available_memory_mb_runner_busy` instead.
  A busy runner alone no longer blocks (26/09, the operator): the machine lost a 1 Go
  service, and an hour of CI kept every chantier waiting with room to spare.

`admission` and `next_chantier` are THE decision: `start`, `start-next` and
the dashboard's next pass (#869) ask them, so that the page never says « will
launch » where the relauncher would refuse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tracerworks import texts
from tracerworks.frontier import Candidate, Issue, Queue, choose
from tracerworks.github import RunnerStatus, is_blocked, last_labeled_by, runner_status
from tracerworks.observe import Worker, available_memory_mb
from tracerworks.process import Runner
from tracerworks.providers import PROVIDERS, Provider
from tracerworks.quotas import measured_weekly
from tracerworks.routing import route
from tracerworks.settings import Settings
from tracerworks.state_file import State


def refusals(runner: RunnerStatus, memory_mb: int | None, state: State) -> list[str]:
    """The machine's guards: the runner readable, enough memory."""
    reasons = []
    if runner.status == "unknown":
        reasons.append(texts.RUNNER_UNREADABLE.format(name=runner.name))
    busy = runner.status == "online" and bool(runner.busy)
    floor = (
        state.min_available_memory_mb_runner_busy
        if busy
        else state.min_available_memory_mb
    )
    if memory_mb is None:
        reasons.append(texts.FREE_MEMORY_UNREADABLE)
    elif memory_mb < floor:
        why = texts.RUNNER_BUSY.format(name=runner.name) if busy else ""
        reasons.append(texts.LOW_MEMORY.format(mb=memory_mb, floor=floor, why=why))
    return reasons


@dataclass(frozen=True)
class Admission:
    provider: Provider | None  # None: no new worker may start
    reason: str  # why this provider — or why none
    blocked: tuple[str, ...] = ()  # the order's and the machine's refusals


def admission(
    run: Runner,
    settings: Settings,
    state: State,
    now: float,
    launch_blocker: Callable[[Provider], str | None],
    *,
    over_cap: bool,
    room: str | None = None,
    machine: bool = True,
) -> Admission:
    """The order (`room`: why no new chantier may start, None: one may),
    the machine's guards, then the quota cap's route, then the runtime.

    `machine=False`: a session that REPLACES another one — a red resumed on
    the lane it sets aside (#1263) — adds nothing to the machine: only the
    quota and the runtime still say."""
    blocked = [room] if room else []
    if machine:
        runner = runner_status(run, settings.runner_name, settings.github_repo)
        memory_mb = available_memory_mb(settings.meminfo)
        blocked += refusals(runner, memory_mb, state)
    if blocked:
        reasons = "; ".join(blocked)
        return Admission(None, texts.NO_NEW_WORKER.format(reasons=reasons), (*blocked,))
    measured = measured_weekly(settings.claude_quota_file, now)
    chosen = route(state, over_cap=over_cap, measured=measured)
    if chosen.provider is None:
        return Admission(None, chosen.reason)
    provider = PROVIDERS[chosen.provider]
    blocker = launch_blocker(provider)
    if blocker is not None:
        return Admission(None, blocker)
    return Admission(provider, chosen.reason)


def next_chantier(
    run: Runner, settings: Settings, issues: list[Issue], workers: list[Worker]
) -> tuple[Candidate | None, list[str]]:
    """The queue's first chantier with a free head ticket, and why others were
    skipped. Never one that has a worker, nor a stopped one: those are explicit."""
    by_spec = {w.spec: w for w in workers}

    def has_worker(spec: int) -> str | None:
        worker = by_spec.get(spec)
        if worker is None:
            return None
        if worker.window is None:
            return texts.STOPPED_WITH_WORKTREE
        return texts.HAS_WORKER.format(state=worker.state.value)

    def launched(spec: Issue) -> str | None:
        """The launch label counts when the operator put it last (#1529)."""
        label = settings.launch_label
        actor = last_labeled_by(run, settings.github_repo, spec.number, label)
        if actor is None:
            return texts.LAUNCH_ACTOR_UNKNOWN.format(label=label)
        if actor != settings.operator_login:
            return texts.LAUNCHED_BY_OTHER.format(label=label, login=actor)
        return None

    queue = Queue(
        frozenset({settings.ready_label, settings.launch_label}),
        settings.priority_label,
    )
    return choose(
        issues,
        queue,
        has_worker,
        lambda ticket, unlisted: is_blocked(
            run, settings.github_repo, ticket, unlisted
        ),
        trusted=lambda issue: issue.author in settings.trusted_logins,
        launched=launched,
    )


def no_candidate(settings: Settings) -> str:
    return texts.NO_CANDIDATE.format(
        ready=settings.ready_label, launch=settings.launch_label
    )
