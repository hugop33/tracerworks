"""One state per chantier, the same words everywhere, and its history (#1729).

`status`, the workshop and GitHub read a chantier in one set of words:

    queued · in development · light check · in the merge queue ·
    waits for a human · blocked · parked · done

each with its reason, the time it was entered and its lane. The state is
DEDUCED, as the workers' (`observe`): `derive` reads the facts the world
gives — the worker and its hooks' last word, its parked or set-aside record,
its pull request, its CI, the merge queue, the `needs-human` label — in one
order, the first that holds wins, and says which rule decided (its cause).

THE TRANSITIONS ARE A TABLE (`ALLOWED`). `serve`'s cycle records each change
of a chantier in the history base (`chantier_state`) with its cause; a change
the table refuses is recorded as refused, and the chantier stays in its last
recorded state until the world agrees with the table again — or the refused
state lasts (`CONFIRM_SECONDS`), then real and accepted, said so: two
commands racing never leave a chantier in a state no one can explain, and a
`serve` down while it moved on never freezes its history.
`tracerworks history <n>` unrolls it.
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from tracerworks import texts
from tracerworks.observe import Worker, WorkerState

if TYPE_CHECKING:  # parking → cleanup → report → here: a cycle at import time
    from tracerworks.merge_queue import View
    from tracerworks.order import Factory
    from tracerworks.parking import Parked


class Phase(enum.Enum):
    """Its value is its name in JSON and in the history base; its words are
    texts.py's — two states translated alike never become one."""

    QUEUED = "queued"
    DEVELOPMENT = "development"
    LIGHT_CHECK = "light_check"
    MERGE_QUEUE = "merge_queue"
    HUMAN = "human"
    BLOCKED = "blocked"
    PARKED = "parked"
    DONE = "done"

    @property
    def key(self) -> str:
        return str(self.value)

    @property
    def words(self) -> str:
        return texts.PHASE_WORDS[self.value]


P = Phase
#: Where a chantier may go from each state. A state is always allowed to
#: stay itself (its reason may change). What is absent is refused: a
#: chantier enters the merge queue or its light check only with a pull
#: request, so never straight from the queue; it leaves the merge queue
#: merged, sent back to its worker, or waiting for someone — never queued;
#: and a done chantier comes back only through the queue or its worker.
ALLOWED: Mapping[Phase, frozenset[Phase]] = {
    P.QUEUED: frozenset({P.DEVELOPMENT, P.HUMAN, P.BLOCKED, P.PARKED}),
    P.DEVELOPMENT: frozenset(set(P) - {P.DEVELOPMENT}),
    P.LIGHT_CHECK: frozenset(set(P) - {P.LIGHT_CHECK}),
    P.MERGE_QUEUE: frozenset(set(P) - {P.MERGE_QUEUE, P.QUEUED}),
    P.HUMAN: frozenset(set(P) - {P.HUMAN}),
    P.BLOCKED: frozenset(set(P) - {P.BLOCKED}),
    P.PARKED: frozenset(set(P) - {P.PARKED}),
    P.DONE: frozenset({P.QUEUED, P.DEVELOPMENT}),
}


#: How long a refused state must last before it is taken as real.
CONFIRM_SECONDS = 10 * 60


def allowed(before: Phase, after: Phase) -> bool:
    return before is after or after in ALLOWED[before]


#: What the worker waits for, by the kind its hooks or screen said (#1728):
#: someone's answer, or a gesture no answer replaces.
_FOR_A_HUMAN = ("permission", "question")


@dataclass(frozen=True)
class Facts:
    """What the world says of one chantier. None: not known, or not so."""

    worker: WorkerState | None = None  # None: no worker (no window, no worktree)
    asking: str | None = None  # what it waits for, while ASKING
    asking_kind: str | None = None  # permission, question, login, limit
    parked_pull: int | None = None  # its session closed while its CI runs
    set_aside_for: int | None = None  # 0: by the operator
    pull: int | None = None
    ci_underway: bool = False
    queue_state: str | None = None  # its pull request's stay: waiting, testing
    needs_human: int | None = None  # the issue that carries `needs-human`
    merged: int | None = None  # its pull request, merged
    waits_for_memory: bool = False  # the queue held, or its worker said so
    ci_red: bool = False  # its pull request's CI failed (#1730)
    conflicting: bool = False  # its pull request in conflict with the base (#2137)
    pending: str | None = None  # at rest, a text left in its field (#2003)


@dataclass(frozen=True)
class State:
    phase: Phase
    reason: str | None
    cause: str  # the rule that decided, for the history

    def describe(self) -> str:
        if self.reason is None:
            return self.phase.words
        return texts.PHASE_WITH_REASON.format(phase=self.phase.words, why=self.reason)


#: How much of a text left in a field the state shows: its start.
EXCERPT_CHARS = 60


def excerpt(text: str) -> str:
    """The start of a text, on one line."""
    line = " ".join(text.split())
    return line if len(line) <= EXCERPT_CHARS else f"{line[:EXCERPT_CHARS]}…"


#: the rule of a chantier parked on a red (#1730): `status` reads it back
#: (#2175) and the parked-red alert waits on it
CAUSE_PARKED_RED = "parked: red"


def derive(facts: Facts) -> State:
    """The chantier's state: the first rule that holds."""
    f = facts
    if f.merged is not None:
        return State(P.DONE, texts.WHY_MERGED.format(pull=f.merged), "pull merged")
    if f.queue_state is not None:
        why = (
            texts.WHY_QUEUE_TESTING
            if f.queue_state == "testing"
            else texts.WHY_QUEUE_WAITING
        )
        return State(P.MERGE_QUEUE, why, f"merge queue: {f.queue_state}")
    if f.worker is WorkerState.DEAD:
        return State(P.BLOCKED, texts.WHY_DEAD, "window without its provider")
    if f.worker is WorkerState.ASKING:
        phase = P.HUMAN if f.asking_kind in _FOR_A_HUMAN else P.BLOCKED
        return State(phase, f.asking, f"worker: {f.asking_kind or 'asking'}")
    halted = f.worker in (WorkerState.WAITING, WorkerState.STOPPED, None)
    if f.needs_human is not None and halted:
        why = texts.WHY_NEEDS_HUMAN.format(issue=f.needs_human)
        return State(P.HUMAN, why, "human label")
    if f.set_aside_for is not None:
        why = (
            texts.WHY_SET_ASIDE_BY_HAND
            if f.set_aside_for == 0
            else texts.WHY_SET_ASIDE_FOR.format(spec=f.set_aside_for)
        )
        return State(P.PARKED, why, "set aside")
    if f.parked_pull is not None and f.conflicting and not f.ci_underway:
        # no CI runs on a conflict: never « light check » (30/10, #2137)
        why = texts.WHY_PARKED_CONFLICT.format(pull=f.parked_pull)
        return State(P.PARKED, why, "parked: conflict")
    if f.parked_pull is not None and f.ci_red:
        # its CI red, no resume yet: a deadline runs (#1730)
        why = texts.WHY_PARKED_RED.format(pull=f.parked_pull)
        return State(P.PARKED, why, CAUSE_PARKED_RED)
    if f.parked_pull is not None:
        why = texts.WHY_PARKED_CI.format(pull=f.parked_pull)
        return State(P.LIGHT_CHECK, why, "parked during its CI")
    if f.worker is WorkerState.WORKING:
        return State(P.DEVELOPMENT, None, "worker: working")
    # 26/10: « at rest » for hours, a text typed and never submitted (#2003)
    left = (
        texts.WHY_FIELD_TAKEN.format(text=excerpt(f.pending))
        if f.worker is WorkerState.WAITING and f.pending
        else None
    )
    if f.worker is WorkerState.WAITING and f.ci_underway and f.pull is not None:
        why = texts.WHY_CI.format(pull=f.pull)
        why = why if left is None else f"{why}; {left}"
        return State(P.LIGHT_CHECK, why, "worker at rest, its CI runs")
    if left is not None:
        return State(P.DEVELOPMENT, left, "worker: at rest, its field taken")
    if f.worker is WorkerState.WAITING and f.waits_for_memory:
        return State(P.DEVELOPMENT, texts.WHY_MEMORY, "worker: waits for memory")
    if f.worker is WorkerState.WAITING:
        return State(P.DEVELOPMENT, texts.WHY_AT_REST, "worker: at rest")
    if f.worker is WorkerState.STOPPED:
        return State(P.PARKED, texts.WHY_STOPPED, "worktree without a window")
    if f.waits_for_memory:
        return State(P.QUEUED, texts.WHY_MEMORY, "no worker, the memory low")
    return State(P.QUEUED, None, "no worker")


def queue_states(queue: View | None) -> dict[int, str]:
    """The merge queue's stays by pull request: testing, waiting."""
    if queue is None:
        return {}
    stays = {stay.pull: "waiting" for stay in queue.waiting}
    if queue.testing is not None:
        stays[queue.testing.pull] = "testing"
    return stays


def worker_facts(
    worker: Worker,
    parked: Mapping[int, Parked],
    shop: Factory | None,
    queue: Mapping[int, str],
    pull: int | None,
    ci_underway: bool = False,
    needs_human: int | None = None,
    merged: int | None = None,
    ci_red: bool = False,
    conflicting: bool = False,
) -> Facts:
    """The facts `status` and the snapshot share; the CI, its conflict, the
    human label and a merge, only the snapshot reads them."""
    record = parked.get(worker.spec) if worker.window is None else None
    aside = shop.set_aside.get(worker.spec) if shop is not None else None
    pull = pull if pull is not None else (record.pull if record else None)
    has_worker = worker.window is not None or worker.worktree is not None
    return Facts(
        worker=worker.state if has_worker else None,
        asking=worker.asking,
        asking_kind=worker.asking_kind,
        parked_pull=record.pull if record else None,
        set_aside_for=(aside.for_spec or 0) if aside is not None else None,
        pull=pull,
        ci_underway=ci_underway,
        queue_state=queue.get(pull) if pull is not None else None,
        needs_human=needs_human,
        merged=merged,
        waits_for_memory=worker.waits_for_memory,
        ci_red=ci_red,
        conflicting=conflicting,
        pending=worker.pending,
    )


# --- the history ---------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One row of `chantier_state`: a change, recorded or refused."""

    at: int
    phase: Phase
    reason: str | None
    lane: int | None
    cause: str
    refused: bool


def _step(row: tuple[object, ...]) -> Step:
    at, phase, reason, lane, cause, refused = row
    return Step(
        int(str(at)),
        Phase[str(phase).upper()],
        None if reason is None else str(reason),
        None if lane is None else int(str(lane)),
        str(cause),
        bool(refused),
    )


_COLUMNS = "at, phase, reason, lane, cause, refused"


def steps(db: sqlite3.Connection, project: str, spec: int) -> list[Step]:
    """A chantier's history, oldest first."""
    rows = db.execute(
        f"SELECT {_COLUMNS} FROM chantier_state"
        " WHERE project = ? AND spec = ? ORDER BY at, id",
        (project, spec),
    )
    return [_step(row) for row in rows]


def current(db: sqlite3.Connection, project: str, spec: int) -> Step | None:
    """Its last ACCEPTED state."""
    row = db.execute(
        f"SELECT {_COLUMNS} FROM chantier_state WHERE project = ? AND spec = ?"
        " AND refused = 0 ORDER BY at DESC, id DESC LIMIT 1",
        (project, spec),
    ).fetchone()
    return None if row is None else _step(row)


def parked_red(db: sqlite3.Connection, project: str, specs: Iterable[int]) -> set[int]:
    """The chantiers `serve` last read parked on a red (#2175): `status`
    calls no GitHub, and its parked chantiers said « light check » with
    their pull request red (31/10, #2131, for 4 h 30)."""
    return {
        spec
        for spec in specs
        if (step := current(db, project, spec)) is not None
        and step.cause == CAUSE_PARKED_RED
    }


def _last(db: sqlite3.Connection, project: str, spec: int) -> Step | None:
    row = db.execute(
        f"SELECT {_COLUMNS} FROM chantier_state WHERE project = ? AND spec = ?"
        " ORDER BY at DESC, id DESC LIMIT 1",
        (project, spec),
    ).fetchone()
    return None if row is None else _step(row)


def record(
    db: sqlite3.Connection,
    project: str,
    spec: int,
    state: State,
    lane: int | None,
    now: int,
) -> Step | None:
    """Write the chantier's state if it changed; the row written, else None.

    The same phase with another reason or lane is a change too. A change the
    table refuses is written once as refused (not again while the world says
    the same), and the chantier keeps its last accepted state — a race of
    two commands lasts a snapshot. A refused state the world still shows
    `CONFIRM_SECONDS` later is real (a `serve` down while the chantier moved
    on): accepted then, its cause saying so — the history never freezes."""
    before = current(db, project, spec)
    if (
        before is not None
        and before.phase is state.phase
        and (before.reason, before.lane) == (state.reason, lane)
    ):
        return None
    refused = before is not None and not allowed(before.phase, state.phase)
    cause = state.cause
    if refused:
        last = _last(db, project, spec)
        if last is not None and last.refused and last.phase is state.phase:
            if now - last.at < CONFIRM_SECONDS:
                return None
            refused, cause = False, texts.CAUSE_CONFIRMED.format(cause=state.cause)
    db.execute(
        "INSERT INTO chantier_state (project, spec, at, phase, reason, lane, cause,"
        " refused) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project, spec, now, state.phase.key, state.reason, lane, cause, refused),
    )
    return Step(now, state.phase, state.reason, lane, cause, refused)


def _shown(item: Mapping[str, object]) -> State | None:
    phase = item.get("phase")
    if not isinstance(phase, str) or phase.upper() not in Phase.__members__:
        return None
    reason, cause = item.get("phase_reason"), item.get("phase_cause")
    return State(
        Phase[phase.upper()],
        reason if isinstance(reason, str) else None,
        cause if isinstance(cause, str) else "?",
    )


def record_snapshot(
    db: sqlite3.Connection, project: str, snapshot: Mapping[str, object], now: int
) -> list[tuple[int, Step]]:
    """Record the state of every chantier the snapshot shows — its workers,
    its queue; the rows written, by spec."""
    workers = snapshot.get("workers")
    queue = snapshot.get("queue")
    shown = [
        *(workers if isinstance(workers, list) else []),
        *(queue if isinstance(queue, list) else []),
    ]
    written = []
    for item in shown:
        if not isinstance(item, Mapping):
            continue
        spec = item.get("spec", item.get("number"))
        state = _shown(item)
        if not isinstance(spec, int) or state is None:
            continue
        lane = item.get("lane")
        step = record(
            db, project, spec, state, lane if isinstance(lane, int) else None, now
        )
        if step is not None:
            written.append((spec, step))
    return written


def since(db: sqlite3.Connection, project: str, specs: Iterable[int]) -> dict[int, int]:
    """When each chantier entered its current phase — the earliest accepted
    row of its last run of rows in that phase."""
    entered: dict[int, int] = {}
    for spec in specs:
        history = [s for s in steps(db, project, spec) if not s.refused]
        if not history:
            continue
        at = history[-1].at
        for step in reversed(history):
            if step.phase is not history[-1].phase:
                break
            at = step.at
        entered[spec] = at
    return entered


def describe_history(history: list[Step], spec: int) -> list[str]:
    if not history:
        return [texts.HISTORY_NONE.format(spec=spec)]
    lines = []
    for step in history:
        when = datetime.fromtimestamp(step.at).strftime("%d/%m %H:%M")
        state = State(step.phase, step.reason, step.cause).describe()
        lane = texts.HISTORY_LANE.format(lane=step.lane) if step.lane else ""
        template = texts.HISTORY_REFUSED if step.refused else texts.HISTORY_STEP
        lines.append(
            template.format(when=when, state=state, lane=lane, cause=step.cause)
        )
    return lines
