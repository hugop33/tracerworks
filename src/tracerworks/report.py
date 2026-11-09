"""`tracerworks status` as text for the operator, or JSON for the cockpit."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tracerworks import texts
from tracerworks.disk import describe_size
from tracerworks.github import RunnerStatus
from tracerworks.level import describe_view
from tracerworks.observe import Worker
from tracerworks.order import Factory, chantier_json, ordinal, view_json
from tracerworks.state_file import State

if TYPE_CHECKING:  # parking → cleanup → report: a cycle at import time
    from tracerworks.chantier_state import State as ChantierState
    from tracerworks.merge_queue import View
    from tracerworks.parking import Parked


def ago(then: int | None, now: float) -> str:
    if then is None:
        return "?"
    minutes = max(0, int(now - then)) // 60
    if minutes < 1:
        return texts.JUST_NOW
    if minutes < 60:
        return texts.MINUTES_AGO.format(m=minutes)
    hours = minutes // 60
    if hours < 48:
        return texts.HOURS_AGO.format(h=hours, m=minutes % 60)
    return texts.DAYS_AGO.format(d=hours // 24)


def describe_saved(worker: Worker) -> str:
    saved = worker.saved
    if worker.worktree is None:
        return texts.NO_WORKTREE_SAVED
    if saved is None:
        return texts.GIT_UNREADABLE
    if saved.everything_pushed:
        return texts.ALL_PUSHED
    parts = []
    if saved.unpushed:
        suffix = texts.NEVER_PUSHED if saved.upstream is None else ""
        parts.append(texts.UNPUSHED.format(n=saved.unpushed, suffix=suffix))
    if saved.dirty:
        parts.append(texts.UNCOMMITTED.format(n=saved.dirty))
    return ", ".join(parts)


def describe_quota(state: State) -> str:
    used = "?" if state.claude_weekly_used is None else str(state.claude_weekly_used)
    return texts.QUOTA.format(used=used, cap=state.claude_weekly_cap)


def describe_memory(memory_mb: int | None) -> str:
    if memory_mb is None:
        return texts.MEMORY_UNREADABLE
    return texts.MEMORY.format(gb=memory_mb / 1024)


def parked_of(worker: Worker, parked: Mapping[int, Parked]) -> Parked | None:
    """Its parked session (#1025) — only while it has no window."""
    return parked.get(worker.spec) if worker.window is None else None


def describe_state(
    state: ChantierState, entered: Mapping[int, int], spec: int, now: float
) -> str:
    """Its state in the chantier's words (#1729), and since when it is in it
    — when the history base knows."""
    said = state.describe()
    if spec in entered:
        said += " " + texts.PHASE_SINCE.format(ago=ago(entered[spec], now))
    return said


def describe_rank(spec: int, shop: Factory | None) -> list[str]:
    """Its rank in the factory's order (#1823) — its state already says its
    role; nothing for a chantier not in flight, nor when the order is
    unreadable."""
    rank = shop.rank(spec) if shop is not None else None
    return [] if rank is None else [texts.RANK.format(rank=ordinal(rank))]


def _level_words(shown: dict[str, Any] | None) -> list[str]:
    """Its model and effort (#2252) — nothing when nothing is known."""
    said = describe_view(shown) if shown is not None else None
    return [] if said is None else [said]


def render_text(
    workers: list[Worker],
    runner: RunnerStatus,
    memory_mb: int | None,
    state: State,
    now: float,
    sizes: Mapping[int, int | None],
    parked: Mapping[int, Parked],
    shop: Factory | None,
    queue: View | None,
    states: Mapping[int, ChantierState],
    entered: Mapping[int, int],
    nudged: Mapping[int, str] | None = None,
    levels: Mapping[int, dict[str, Any]] | None = None,
) -> str:
    from tracerworks import merge_queue

    nudged = nudged or {}
    levels = levels or {}
    # first, so that a factory starting nothing is not taken for broken (#2192)
    lines = [texts.QUEUE_PAUSED_LINE] if state.queue_paused else []
    for worker in workers:
        where = []
        if worker.worktree is not None:
            where.append(worker.worktree.branch or texts.DETACHED)
            where.append(str(worker.worktree.path))
            where.append(texts.DISK.format(size=describe_size(sizes.get(worker.spec))))
        lines.append(
            " — ".join(
                [
                    f"#{worker.spec} {worker.slug or ''}".rstrip(),
                    worker.provider or texts.PROVIDER_UNKNOWN,
                    *_level_words(levels.get(worker.spec)),
                    describe_state(states[worker.spec], entered, worker.spec, now),
                    *where,
                    texts.ACTIVITY.format(ago=ago(worker.last_activity, now)),
                    describe_saved(worker),
                    *describe_rank(worker.spec, shop),
                    *([nudged[worker.spec]] if worker.spec in nudged else []),
                ]
            )
        )
    if not workers:
        lines.append(texts.NO_WORKER)
    if shop is not None:
        lines += shop.describe({n: st.describe() for n, st in states.items()})
        lines += shop.describe_waits()
    lines += merge_queue.describe(queue, now)
    lines.append(
        " · ".join(
            [
                runner.describe(),
                describe_memory(memory_mb),
                texts.PROVIDER.format(name=state.provider),
                describe_quota(state),
            ]
        )
    )
    return "\n".join(lines)


def render_json(
    workers: list[Worker],
    runner: RunnerStatus,
    memory_mb: int | None,
    state: State,
    sizes: Mapping[int, int | None],
    parked: Mapping[int, Parked],
    shop: Factory | None,
    queue: View | None,
    states: Mapping[int, ChantierState],
    entered: Mapping[int, int],
    nudged: Mapping[int, str] | None = None,
    levels: Mapping[int, dict[str, Any]] | None = None,
) -> str:
    from tracerworks import merge_queue

    nudged = nudged or {}
    levels = levels or {}

    def worker_json(worker: Worker) -> dict[str, Any]:
        record = parked_of(worker, parked)
        return {
            "spec": worker.spec,
            "state": "parked" if record else worker.state.name.lower(),
            # the chantier's state (#1729): one set of words everywhere
            "phase": states[worker.spec].phase.key,
            "phase_text": states[worker.spec].describe(),
            "phase_reason": states[worker.spec].reason,
            "phase_since": entered.get(worker.spec),
            "parked": None
            if record is None
            else {
                "session": record.session,
                "pull": record.pull,
                "parked_at": record.parked_at,
            },
            "asking": worker.asking,
            # at rest, a text left in its field, never submitted (#2003)
            "pending_field": worker.pending,
            # its last nudge for a permission it need not ask (#1789)
            "nudge": nudged.get(worker.spec),
            "provider": worker.provider,
            # its model and effort, asked and seen (#2252); None: not read
            "model": levels[worker.spec]["model"] if worker.spec in levels else None,
            "effort": levels[worker.spec]["effort"] if worker.spec in levels else None,
            "branch": worker.worktree.branch if worker.worktree else None,
            "worktree": str(worker.worktree.path) if worker.worktree else None,
            "last_activity": worker.last_activity,
            "unpushed_commits": worker.saved.unpushed if worker.saved else None,
            "uncommitted_paths": worker.saved.dirty if worker.saved else None,
            "disk_kb": sizes.get(worker.spec),
            **chantier_json(shop, worker.spec),
        }

    return json.dumps(
        {
            "workers": [worker_json(w) for w in workers],
            "runner": {
                "name": runner.name,
                "status": runner.status,
                "busy": runner.busy,
            },
            "available_memory_mb": memory_mb,
            "provider": state.provider,
            "claude_weekly_used": state.claude_weekly_used,
            "claude_weekly_cap": state.claude_weekly_cap,
            "queue_paused": state.queue_paused,
            # None: the order unreadable (said on stderr)
            **view_json(shop, {n: st.describe() for n, st in states.items()}),
            # the merge queue (#1714); None: the history base unreadable
            "merge_queue": merge_queue.as_json(queue),
        },
        ensure_ascii=False,
        indent=2,
    )
