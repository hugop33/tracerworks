"""Command-line entry point: one `tracerworks` command = one reading of the world.

Split like the other tools: `run` takes an `Env` (the process runner, the
clock, the streams), so every command is reachable in a unit test with recorded
outputs and no tmux; `main` hands over the real ones.

Exit codes: 0 done; 1 refused (a guard said no, a message was not received —
the reason is on stderr); 2 argparse's usage error; 3 the world could not be
read or written (a command that had to succeed failed).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from tracerworks import (
    background,
    chantier_state,
    ci_revival,
    conflict_runs,
    conflicts,
    flaky_bugs,
    level,
    low_memory,
    machine_red,
    merge_queue,
    nudges,
    order,
    overdue,
    store,
    texts,
    waits,
    worktree_watch,
)
from tracerworks.alerts import AlertError, telegram
from tracerworks.backup import KEEP_DAYS, BackupError, take
from tracerworks.carry import carried_of, carry, issue_labels
from tracerworks.cleanup import (
    RED_MARKER,
    RED_MESSAGE,
    BaseBranch,
    Checks,
    OpenTickets,
    close_spec,
    commented,
    keep_spec_open,
    may_park,
    merged_pull_request,
    open_pull_request,
    post_usage,
    pull_checks,
    red_comment,
    remove_worktree,
    shells_running,
    spec_pulls,
    ticket_refs,
    usage_comment,
    usage_posted,
    why_kept,
)
from tracerworks.control import Memo
from tracerworks.conventions import slugify
from tracerworks.delivery import build_delivery
from tracerworks.disk import describe_size, size_kb
from tracerworks.errors import Refused
from tracerworks.frontier import BodyRules, parse_issues
from tracerworks.git import dirty_paths
from tracerworks.github import (
    SpecIssue,
    last_reopened_at,
    open_issues_payload,
    runner_status,
    view_issue,
)
from tracerworks.guards import admission, next_chantier, no_candidate
from tracerworks.machine_red import FailedRun
from tracerworks.mcp import Door, Outcome, Server
from tracerworks.measures import build_measures, span_of
from tracerworks.memory import release_memory
from tracerworks.messaging import WAIT_IDLE_SECONDS, NotIdleError
from tracerworks.observe import (
    Worker,
    WorkerState,
    asking_specs,
    available_memory_mb,
    observe_workers,
    pending_field,
    read_worktrees,
    resting_specs,
)
from tracerworks.parking import (
    AGAIN,
    Parked,
    PullState,
    after_rerun,
    again_text,
    due_on_open,
    failed_runs,
    forget,
    in_resume_grace,
    latest_session,
    machine_due,
    mark_refused,
    mark_rerun,
    mark_resumed,
    park,
    pull_state,
    readable_parked,
    rerun_due,
    rerun_failed,
    resume_text,
    run_attempt,
    told_of,
    unanswered,
)
from tracerworks.primer import primer
from tracerworks.process import (
    CommandError,
    Runner,
    checked,
    one_at_a_time,
    read_once,
    run_process,
)
from tracerworks.providers import CLAUDE, PROVIDERS, Provider
from tracerworks.quality import build_quality
from tracerworks.query import (
    QueryError,
    as_json,
    as_table,
    describe_schema,
    query,
    schema,
)
from tracerworks.quotas import record_statusline, state_defaults
from tracerworks.recorder import CpuReading, Recorder, record_chantier
from tracerworks.relauncher import RELAUNCHER_LOG_LINES, cap_log
from tracerworks.report import describe_quota, describe_saved, render_json, render_text
from tracerworks.routing import CODEX_CLOSED
from tracerworks.runtime.tmux import Tmux
from tracerworks.runtime.tmux_worktree import NoWorkerError, TmuxWorktreeRuntime
from tracerworks.self_update import self_update
from tracerworks.serve import (
    PAGE_FILE,
    Dashboard,
    SecretError,
    SnapshotCache,
    Star,
    make_server,
    read_secret,
    watch_base,
)
from tracerworks.service import install_service
from tracerworks.settings import ConfigError, Settings
from tracerworks.settings import load as load_settings
from tracerworks.snapshot import build_snapshot
from tracerworks.state_file import State, StateFileError, assign, load, update
from tracerworks.store import SCHEMA_VERSION, StoreError
from tracerworks.strays import strays as find_strays
from tracerworks.trends import Pace, build_trends
from tracerworks.usage import ChantierUsage, Tokens, chantier_usage
from tracerworks.worktrees import prepare

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BROKEN = 3
#: `serve` before a history base newer than itself (#2156): its unit restarts
#: it, on the version installed
EXIT_NEWER_BASE = 2
#: How often `serve` reads the history base's schema version.
BASE_WATCH_S = 60.0


@dataclass
class Env:
    settings: Settings
    runner: Runner = run_process
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    stdin: IO[bytes] = field(default_factory=lambda: sys.stdin.buffer)
    out: IO[str] = field(default_factory=lambda: sys.stdout)
    err: IO[str] = field(default_factory=lambda: sys.stderr)

    @property
    def tmux(self) -> Tmux:
        return Tmux(self.runner, self.settings.tmux_session, self.settings.tmux_socket)

    def window(self, spec: int) -> str:
        return self.settings.conventions.window_name(spec)

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def refuse(self, text: str) -> int:
        """Print a refusal; commands raise `Refused` and `run` calls this."""
        print(texts.REFUSED.format(text=text), file=self.err, flush=True)
        return EXIT_REFUSED


def cmd_status(env: Env, args: argparse.Namespace) -> int:
    settings = env.settings
    state = load(settings.state_file)
    workers = observe_workers(env.runner, settings, env.tmux)
    runner = runner_status(env.runner, settings.runner_name, settings.github_repo)
    memory = available_memory_mb(settings.meminfo)
    sizes = {
        w.spec: size_kb(env.runner, w.worktree.path) for w in workers if w.worktree
    }
    parked = parked_records(env)
    shop = order_view(env, state, workers, parked)
    queue = merge_queue.readable_view(settings)
    moving = chantier_state.queue_states(queue)
    in_conflict = conflicted_pulls(env)
    red = parked_red(env, [w.spec for w in workers if w.spec in parked])
    states = {
        w.spec: chantier_state.derive(
            chantier_state.worker_facts(
                w, parked, shop, moving, None,
                ci_red=w.spec in red,
                conflicting=w.spec in parked and parked[w.spec].pull in in_conflict,
            )
        )
        for w in workers
    }  # fmt: skip
    entered = entered_states(env, [w.spec for w in workers])
    nudged = last_nudges(env, [w.spec for w in workers])
    levels = levels_of(env, workers)
    if args.json:
        env.say(
            render_json(
                workers, runner, memory, state, sizes, parked, shop, queue, states,
                entered, nudged, levels,
            )
        )  # fmt: skip
    else:
        env.say(
            render_text(
                workers, runner, memory, state, env.now(), sizes, parked, shop, queue,
                states, entered, nudged, levels,
            )
        )  # fmt: skip
        for line in conflict_lines(env):
            env.say(line)
    return EXIT_OK


def conflicted_pulls(env: Env) -> set[int]:
    """The pull requests in conflict as `serve`'s last pass read them (its
    open episodes, #1791) — `status` calls no GitHub (30/10, #2137); none,
    said, when the base cannot be read."""
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            return set(conflicts.open_episodes(db, env.settings.github_repo))
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return set()


def parked_red(env: Env, specs: list[int]) -> set[int]:
    """The parked chantiers `serve`'s last pass read red (#2175): « red »,
    never « light check »; none, said, when the base cannot be read."""
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            return chantier_state.parked_red(db, env.settings.github_repo, specs)
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return set()


def conflict_lines(env: Env) -> list[str]:
    """The conflicts no worker carries (#1791), as the alert says them, and
    the machine's reds re-run of the last hours (#1792), and their re-run
    held while the memory is low (#2096); nothing from the history, said, when
    its base cannot be read."""
    short = low_memory.short(env.settings.meminfo)
    held = [] if short is None else [texts.STATUS_RERUN_WAITS_MEMORY.format(mb=short)]
    now = env.now()
    project = env.settings.github_repo
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            found = conflicts.silent(db, project)
            reruns = machine_red.recent(
                db, project, now, functools.partial(run_attempt, env.runner, project)
            )  # #1792, #1919
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return held
    return [
        *held,
        *(
            texts.CONFLICT_STATUS.format(pull=pull, minutes=int(now - since) // 60)
            for pull, since in found
        ),
        *(shown.text for shown in reruns),
    ]


def nudge_words(
    db: sqlite3.Connection, project: str, specs: list[int]
) -> dict[int, str]:
    """Each worker's last nudge (#1789), in the words `status` and the
    workshop share."""
    said = {}
    for spec in specs:
        before = nudges.history(db, project, spec)
        words = nudges.describe(nudges.last(db, project, spec), len(before))
        if words is not None:
            said[spec] = words
    return said


def last_nudges(env: Env, specs: list[int]) -> dict[int, str]:
    """Nothing, said, when the history base cannot be read."""
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            return nudge_words(db, env.settings.github_repo, specs)
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return {}


def entered_states(env: Env, specs: list[int]) -> dict[int, int]:
    """When each chantier entered its state, as `serve` recorded it (#1729);
    nothing, said, when the history base cannot be read."""
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            return chantier_state.since(db, env.settings.github_repo, specs)
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return {}


def cmd_history(env: Env, args: argparse.Namespace) -> int:
    """A chantier's history (#1729): each change of its state, its cause."""
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            history = chantier_state.steps(db, env.settings.github_repo, args.spec)
    except order.BASE_ERRORS as exc:
        raise Refused(texts.HISTORY_UNREADABLE.format(error=exc)) from exc
    for line in chantier_state.describe_history(history, args.spec):
        env.say(line)
    return EXIT_OK


def order_view(
    env: Env, state: State, workers: list[Worker], parked: Mapping[int, Parked]
) -> order.Factory | None:
    """The factory's order as recorded (#1823) — nothing written; None,
    said, when the base cannot be read."""
    try:
        found = order.recorded(env.settings)
    except order.BASE_ERRORS as exc:
        print(texts.ORDER_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return None
    for spec in order.stale(found.aside, workers):
        del found.aside[spec]  # `start-next` forgets it first
    factory, _ = order.arrange(
        workers, parked, found.aside, found.entered, env.settings.in_flight,
        state.max_workers, env.now(), found.place_waits,
    )  # fmt: skip
    return factory


def leave(env: Env, spec: int) -> None:
    """A stopped or cleaned chantier leaves the order (#1823), said."""
    try:
        if spec not in order.entries(env.settings):
            return
        order.leave(env.settings, spec)
    except order.BASE_ERRORS as exc:
        print(texts.ENTRY_NOT_FORGOTTEN.format(n=spec, error=exc), file=env.err)
        return
    env.say(texts.LEFT.format(n=spec))


RESUME_TEXT = (
    "Reprise après une pause demandée par {operator} : reprends le chantier là où tu"
    " l'as laissé. Relis d'abord l'état réel (git status, git log, le ticket de"
    " tête et ses commentaires sur GitHub) avant d'agir."
)

WIP_REQUEST = (
    "{operator} demande l'arrêt du chantier #{n}. Avant tout : fais un commit"
    " `wip: <ce qui est en cours, ce qui reste>` de tout le travail non commité"
    " (fichiers non suivis compris), pousse la branche (git push -u {remote} HEAD),"
    " puis attends sans rien commencer d'autre."
)


def runtime(env: Env) -> TmuxWorktreeRuntime:
    return TmuxWorktreeRuntime(
        env.runner, env.settings, env.tmux, env.sleep, env.now, err=env.err
    )


def live_worker(env: Env, spec: int) -> Worker:
    """The worker whose provider is alive in its window — or a refusal."""
    worker = runtime(env).status(spec)
    if worker is None or worker.window is None:
        raise Refused(texts.NO_WINDOW.format(window=env.window(spec)))
    if worker.state is WorkerState.DEAD:
        raise Refused(texts.DEAD_WINDOW.format(window=env.window(spec)))
    return worker


def stamp(env: Env) -> str:
    return datetime.fromtimestamp(env.now()).strftime("%d/%m %Hh%M")


def deliver(env: Env, spec: int, text: str, wait_idle: bool = False) -> None:
    """Send and verify, or refuse saying what was seen."""
    live_worker(env, spec)
    try:
        delivery = runtime(env).send(spec, text, stamp(env), wait_idle)
    except (NotIdleError, NoWorkerError) as exc:
        raise Refused(str(exc)) from exc
    said = {
        "window": env.window(spec),
        "marker": delivery.marker,
        "trace": "; ".join(delivery.attempts),
    }
    if not delivery.received:
        raise Refused(texts.NOT_RECEIVED.format(**said))
    env.say(texts.RECEIVED.format(**said))


def read_text(env: Env, file: Path | None) -> str:
    data = file.read_bytes() if file is not None else env.stdin.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if not text.strip():
        source = str(file) if file is not None else "stdin"
        raise Refused(texts.EMPTY_MESSAGE.format(source=source))
    return text


def cmd_message(env: Env, args: argparse.Namespace) -> int:
    deliver(env, args.spec, read_text(env, args.file), args.wait_idle)
    return EXIT_OK


def cmd_pause(env: Env, args: argparse.Namespace) -> int:
    live_worker(env, args.spec)
    env.tmux.send_keys(env.window(args.spec), "Escape")
    env.say(texts.PAUSED.format(window=env.window(args.spec)))
    return EXIT_OK


def cmd_resume(env: Env, args: argparse.Namespace) -> int:
    text = RESUME_TEXT.format(operator=env.settings.operator)
    if args.file is not None:
        text = f"{text}\n\n{read_text(env, args.file).strip()}"
    deliver(env, args.spec, text)
    return EXIT_OK


def admit(
    env: Env, state: State, *, over_cap: bool, room: str | None = None
) -> Provider:
    """The provider a new worker gets, or a refusal saying why none may start.

    The guards hold for `start` as for `start-next`: architecture §10 says
    « no new worker », whoever asks.
    """
    chosen = admission(
        env.runner,
        env.settings,
        state,
        env.now(),
        runtime(env).launch_blocker,
        over_cap=over_cap,
        room=room,
    )
    if chosen.provider is None:
        raise Refused(chosen.reason)
    env.say(chosen.reason)
    return chosen.provider


def factory(
    env: Env, state: State, observed: list[Worker], parked: Mapping[int, Parked]
) -> order.Factory:
    """The factory's order (#1823), the chantiers in flight without an entry
    entered and said. A base that cannot be read refuses: an unknown order
    is never room."""
    try:
        base = order.recorded(env.settings)
    except order.BASE_ERRORS as exc:
        raise Refused(texts.ORDER_UNREADABLE.format(error=exc)) from exc
    for spec in order.stale(base.aside, observed):
        forget_aside(env, spec, texts.ASIDE_STALE.format(n=spec))
        del base.aside[spec]
    found, added = order.arrange(
        observed, parked, base.aside, base.entered, env.settings.in_flight,
        state.max_workers, env.now(), base.place_waits,
    )  # fmt: skip
    for spec in added:
        enter(env, spec, texts.ENTERED_AUTO)
    for spec in base.place_waits.keys() - found.place_waits.keys():
        forget_wait(env, spec)  # in flight again: it has its place
    return found


def enter(env: Env, spec: int, how: str = "") -> None:
    """Written and said; a base that fails is said, never a reason to stop:
    the next pass enters a chantier found without an entry."""
    try:
        order.enter(env.settings, spec, env.now())
    except order.BASE_ERRORS as exc:
        print(
            texts.ENTRY_NOT_WRITTEN.format(n=spec, error=exc), file=env.err, flush=True
        )
        return
    env.say(texts.ENTERED.format(n=spec, how=how))


def open_spec(env: Env, spec: int) -> SpecIssue:
    found = view_issue(env.runner, spec, env.settings.github_repo)
    if found is None:
        raise Refused(texts.NO_SUCH_ISSUE.format(n=spec))
    if not found.open:
        raise Refused(texts.ISSUE_CLOSED.format(n=spec))
    return found


def cmd_start(env: Env, args: argparse.Namespace) -> int:
    spec: int = args.spec
    workers = observe_workers(env.runner, env.settings, env.tmux)
    worker = next((w for w in workers if w.spec == spec), None)
    if worker is not None and worker.window is not None:
        raise Refused(
            texts.WINDOW_EXISTS.format(
                window=env.window(spec), state=worker.state.value
            )
        )
    found = open_spec(env, spec)
    state = load(env.settings.state_file)
    found_order = factory(env, state, workers, parked_records(env))
    room = found_order.why_full(spec, over_limit=args.over_limit)
    try:
        provider = admit(env, state, over_cap=args.over_cap, room=room)
    except Refused as exc:
        if room is None or spec in found_order.roles:
            raise
        raise Refused(wait_for_place(env, spec, str(exc))) from exc
    return launch(env, found, worker, provider, head=None)


def wait_for_place(env: Env, spec: int, refusal: str) -> str:
    """The order refused `spec`: it waits for a place (#2118), and the
    refusal says so; a base that fails says it will not come back."""
    try:
        order.wait_for_place(env.settings, spec, env.now())
    except order.BASE_ERRORS as exc:
        print(texts.PLACE_WAIT_NOT_WRITTEN.format(n=spec, error=exc), file=env.err)
        return refusal
    reason = refusal.replace(texts.WAITS_FOR_A_PLACE, "", 1)
    return texts.PLACE_WAITS.format(reason=reason, n=spec)


def forget_wait(env: Env, spec: int, said: str | None = None) -> None:
    """No longer waiting for a place: relaunched, closed, stopped or
    removed. A base that fails is said."""
    try:
        order.forget_place_wait(env.settings, spec)
    except order.BASE_ERRORS as exc:
        print(texts.PLACE_WAIT_NOT_FORGOTTEN.format(n=spec, error=exc), file=env.err)
        return
    if said is not None:
        env.say(said)


def launch(
    env: Env,
    spec: SpecIssue,
    worker: Worker | None,
    provider: Provider,
    head: str | None,
) -> int:
    """Worktree, branch, primer, then the runtime's spawn; it enters the order."""
    prepared = prepare(
        env.runner,
        env.settings,
        spec.number,
        slugify(spec.title),
        worker.worktree if worker else None,
    )
    blocker = runtime(env).guard_blocker(spec.number)
    if blocker is not None:
        raise Refused(blocker)
    labels = issue_labels(env.runner, env.settings.github_repo, spec.number)
    carried = carried_of(labels, env.settings.carried_labels)
    chosen = level.choose(labels, env.settings.level)
    text = primer(
        env.settings.github_repo,
        spec.number,
        spec.title,
        prepared.branch,
        provider.name,
        env.settings.primer_rules,
        resuming=prepared.resuming,
        head=head,
        carried=carried,
        scratch=env.settings.conventions.scratch_path(spec.number),
        trusted=env.settings.trusted_logins,
    )
    env.settings.cache_dir.mkdir(parents=True, exist_ok=True)
    primer_file = env.settings.cache_dir / f"primer-{spec.number}.md"
    primer_file.write_text(text, encoding="utf-8")
    base = env.settings.base_ref
    how = texts.RESUMED if prepared.resuming else texts.CREATED_FROM.format(base=base)
    env.say(
        texts.PREPARED.format(
            n=spec.number,
            path=prepared.path,
            branch=prepared.branch,
            how=how,
            primer=primer_file,
        )
    )
    runtime(env).spawn(spec.number, provider, text, chosen)
    _record_level(env, spec.number, chosen)
    env.say(runtime(env).github_auth(spec.number))
    try:
        forget(env.settings, spec.number)  # a new session: nothing to resume
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.FORGET_FAILED.format(n=spec.number, error=exc), file=env.err)
    forget_aside(env, spec.number)
    forget_wait(env, spec.number)
    env.say(
        texts.LAUNCHED.format(
            window=env.window(spec.number),
            provider=provider.name,
            level=chosen.describe(),
        )
    )
    enter(env, spec.number)
    return EXIT_OK


def cmd_start_next(env: Env, args: argparse.Namespace) -> int:
    """One action per pass, the most urgent first (#1823): a parked red
    resumed (a free session, or one it preempts), else a chantier set aside
    resumed, else a spec waiting for a place (#2118), else a new chantier
    under the limit. Never over the quota cap, never a stopped chantier
    unless `start` asked for it: those are explicit."""
    cap_log(env.settings.relauncher_log, keep=RELAUNCHER_LOG_LINES)  # #2288
    state = load(env.settings.state_file)
    observed = observe_workers(env.runner, env.settings, env.tmux)
    if _memory_back(env, observed) is not None:
        return EXIT_OK
    _nudge_askers(env, observed)  # takes no session, holds back nothing
    if state.queue_paused:  # the Settings tab's (#2192): `start <n>` still starts
        raise Refused(texts.QUEUE_PAUSED)
    parked = parked_records(env)
    found_order = factory(env, state, observed, parked)
    if _resume_red(env, state, observed, found_order, parked) is not None:
        return EXIT_OK
    if _resume_set_aside(env, state, observed, found_order) is not None:
        return EXIT_OK
    if _start_waiting(env, state, observed, found_order) is not None:
        return EXIT_OK
    provider = admit(env, state, over_cap=False, room=found_order.why_full())
    workers = {w.spec: w for w in observed}
    settings = env.settings
    rules = BodyRules(settings.parent_keywords, settings.blocker_keywords)
    issues = parse_issues(open_issues_payload(env.runner, settings.github_repo), rules)
    candidate, skipped = next_chantier(env.runner, settings, issues, observed)
    for reason in skipped:
        env.say(texts.SKIPPED.format(reason=reason))
    if candidate is None:
        raise Refused(no_candidate(settings))
    found = open_spec(env, candidate.spec.number)
    head = f"#{candidate.head.number} — {candidate.head.title}"
    env.say(texts.HEAD_TICKET.format(n=found.number, title=found.title, head=head))
    return launch(env, found, workers.get(found.number), provider, head=head)


def _start_waiting(
    env: Env, state: State, observed: list[Worker], found_order: order.Factory
) -> int | None:
    """Start the spec that waits for a place the longest (#2118), once the
    order has room for it; None: none. A closed spec stops waiting; a
    refusal of the guards keeps its place."""
    by_spec = {w.spec: w for w in observed}
    for i, spec in enumerate(found_order.waiting):
        why = found_order.why_full(spec)
        if why is not None:
            rank = order.ordinal(i + 1)
            env.say(texts.PLACE_WAIT_HELD.format(n=spec, rank=rank, reason=why))
            return None
        try:
            found = open_spec(env, spec)
        except Refused as exc:
            forget_wait(env, spec, texts.PLACE_WAIT_CLOSED.format(n=spec, why=exc))
            continue
        provider = admit(env, state, over_cap=False, room=None)
        since = order.describe_when(found_order.place_waits[spec])
        env.say(texts.PLACE_WAIT_DUE.format(n=spec, since=since))
        return launch(env, found, by_spec.get(spec), provider, head=None)
    return None


def _memory_back(env: Env, observed: Sequence[Worker]) -> int | None:
    """A worker that ended its turn waiting for memory (#1730) is told to go
    on once `[waits] memory_resume_mb` is available — the first, one per
    pass; nothing while the memory is short or unreadable."""
    waiting = [w for w in observed if w.waits_for_memory]
    memory = available_memory_mb(env.settings.meminfo) if waiting else None
    if not waiting or memory is None or memory < env.settings.waits.memory_resume_mb:
        return None
    spec = waiting[0].spec
    deliver(env, spec, waits.MEMORY_RESUME.format(mb=memory))
    env.say(texts.MEMORY_BACK.format(n=spec, mb=memory))
    return spec


def worktree_progress(env: Env, worker: Worker) -> str:
    """Its commit and its upstream's: a commit or a push changes it."""
    if worker.worktree is None:
        return "-"
    path = str(worker.worktree.path)
    shas = []
    for rev in ("HEAD", "@{upstream}"):
        result = env.runner(["git", "-C", path, "rev-parse", "--verify", "-q", rev])
        shas.append(result.stdout.strip() if result.ok else "-")
    return ":".join(shas)


def _nudge_askers(env: Env, observed: Sequence[Worker]) -> list[int]:
    """A worker at rest in development whose turn ended on a permission it
    need not ask is nudged (#1789): once an hour at most; the third without
    progress puts the human label on its spec instead. A base or GitHub that
    cannot be reached, a nudge not received: said, tried at the next pass."""
    settings, project = env.settings, env.settings.github_repo
    resting = [w for w in observed if w.rest is not None and w.window is not None]
    if not resting:
        return []
    acted = []
    try:
        with closing(store.connect(settings.history_db)) as db:
            for worker in resting:
                assert worker.rest is not None
                step = chantier_state.current(db, project, worker.spec)
                seen = nudges.Seen(
                    spec=worker.spec,
                    cause=step.cause if step is not None else None,
                    rested_at=worker.rest.at,
                    last_words=worker.rest.text,
                    progress=worktree_progress(env, worker),
                )
                if seen.cause != nudges.AT_REST or not nudges.asks_permission(
                    seen.last_words,
                    settings.waits.permission_ask,
                    settings.waits.decision,
                ):
                    seen = replace(seen, unsaved=_unfinished_at_rest(env, worker, step))
                before = nudges.history(db, project, worker.spec)
                gesture = nudges.decide(
                    seen,
                    before,
                    int(env.now()),
                    after_minutes=settings.waits.permission_ask_minutes,
                    asks=settings.waits.permission_ask,
                    decisions=settings.waits.decision,
                )
                if gesture is nudges.Gesture.NONE:
                    continue
                if _nudge(env, db, seen, before, gesture):
                    acted.append(worker.spec)
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
    return acted


def _unfinished_at_rest(
    env: Env, worker: Worker, step: chantier_state.Step | None
) -> bool:
    """Known unsaved work behind an open PR, with no human hold (#2148).

    Never infer a free field or a safe Git state from an unreadable one.
    The worker finishes its own work; this pass never modifies its files.
    """
    settings = env.settings
    if (
        step is None
        or step.cause not in nudges.UNSAVED_CAUSES
        or worker.state is not WorkerState.WAITING
        or worker.window is None
        or worker.pending
        or worker.provider != CLAUDE.name
        or worker.waits_for_memory
        or worker.rest is None
        or env.now() - worker.rest.at < settings.waits.permission_ask_minutes * 60
        or worker.saved is None
        or worker.saved.everything_pushed
        or worker.worktree is None
        or not worker.worktree.branch
    ):
        return False
    pane = env.tmux.try_capture(env.window(worker.spec), escapes=True)
    if pane is None or pending_field(pane, CLAUDE) != "":
        return False
    try:
        if (
            open_pull_request(env.runner, settings.github_repo, worker.worktree.branch)
            is None
        ):
            return False
        issues = parse_issues(
            open_issues_payload(env.runner, settings.github_repo),
            BodyRules(settings.parent_keywords, settings.blocker_keywords),
        )
    except (CommandError, ValueError, TypeError, KeyError) as exc:
        env.say(texts.NUDGE_FAILED.format(n=worker.spec, error=exc))
        return False
    return not any(
        settings.human_label in issue.labels
        and (issue.number == worker.spec or issue.parent == worker.spec)
        for issue in issues
    )


def _nudge(
    env: Env,
    db: sqlite3.Connection,
    seen: nudges.Seen,
    before: Sequence[nudges.Sent],
    gesture: nudges.Gesture,
) -> bool:
    settings, project, n = env.settings, env.settings.github_repo, seen.spec
    now = int(env.now())
    vain = [s for s in before if s.progress == seen.progress]  # no progress since
    try:
        if gesture is nudges.Gesture.LABEL:
            argv = ["issue", "edit", str(n), "-R", project]
            label = [*argv, "--add-label", settings.human_label]
            checked(env.runner(["gh", *label]), "gh issue edit")
        else:
            message = nudges.UNSAVED_NUDGE if seen.unsaved else nudges.NUDGE
            deliver(env, n, message.format(label=settings.human_label))
    except (Refused, CommandError) as exc:
        env.say(texts.NUDGE_FAILED.format(n=n, error=exc))
        return False
    # the label is on: recorded at once, so that its removal by the operator
    # is an answer — a comment that then fails is said, never a second label
    outcome = "needs_human" if gesture is nudges.Gesture.LABEL else "sent"
    nudges.record(db, project, n, now, seen.progress, outcome)
    if gesture is nudges.Gesture.LABEL:
        body = nudges.give_up_comment(
            n,
            vain,
            seen.last_words or "",
            label=settings.human_label,
            window=env.window(n),
            session=settings.tmux_session,
            unsaved=seen.unsaved,
        )
        comment = ["gh", "issue", "comment", str(n), "-R", project, "--body-file", "-"]
        try:
            checked(env.runner(comment, stdin=body.encode("utf-8")), "gh issue comment")
        except CommandError as exc:
            env.say(texts.NUDGE_FAILED.format(n=n, error=exc))
        env.say(
            texts.NUDGE_LABELED.format(n=n, count=len(vain), label=settings.human_label)
        )
    else:
        count = len(vain) + 1
        message = texts.NUDGED_UNSAVED if seen.unsaved else texts.NUDGED
        env.say(message.format(n=n, count=count, most=nudges.GIVE_UP_AT - 1))
    return True


def parked_records(env: Env) -> dict[int, Parked]:
    """The parked chantiers (#1025); a base that cannot be read is said, and
    reads as « none parked »: a resume waits, nothing is invented."""
    records, error = readable_parked(env.settings)
    if error is not None:
        print(texts.PARKED_UNREADABLE.format(error=error), file=env.err, flush=True)
    return records


def _admit_resume(
    env: Env, state: State, refused: str, spec: int, *, machine: bool = True
) -> Provider:
    """A resume's guards — never the limit in flight, it is in flight — and its
    worktree's session guard (#1529), or a refusal that keeps its place:
    nothing new passes before it."""
    chosen = admission(
        env.runner,
        env.settings,
        state,
        env.now(),
        runtime(env).launch_blocker,
        over_cap=False,
        machine=machine,
    )
    if chosen.provider is None:
        reason = "; ".join(chosen.blocked) or chosen.reason
        raise Refused(texts.RESUME_REFUSED.format(what=refused, reason=reason))
    env.say(chosen.reason)
    if chosen.provider is not CLAUDE:
        raise Refused(texts.RESUME_WAITS_CLAUDE.format(what=refused))
    guard = runtime(env).guard_blocker(spec)
    if guard is not None:
        raise Refused(texts.RESUME_REFUSED.format(what=refused, reason=guard))
    return chosen.provider


def _resume_red(
    env: Env,
    state: State,
    observed: list[Worker],
    found_order: order.Factory,
    records: Mapping[int, Parked],
) -> int | None:
    """Reopen the best ranked parked session whose pull request needs its
    worker (#1025) — before anything else; None: none (#1263, #1823).

    A free session takes it: it adds one, so the memory guard says too, and
    a refusal keeps its place. With none free, it PREEMPTS the most recent
    chantier in dev ranked after it, at a safe point (`_preemptible`): that
    one is SET ASIDE first, the red replaces its session, so the memory
    guard does not say — only the quota. A chantier set aside is resumed as
    such, with its newer session (`_resume_set_aside`)."""
    by_spec = {w.spec: w for w in observed}
    settings, repo = env.settings, env.settings.github_repo
    held: Refused | None = None  # a red the machine refused: it keeps its place
    since = entered_states(env, [*records, *found_order.in_dev])
    ranked = sorted(records.items(), key=lambda r: (found_order.rank(r[0]) or 0, r[0]))
    for spec, record in ranked:
        worker = by_spec.get(spec)
        if worker is None or worker.worktree is None or worker.window is not None:
            continue  # never two sessions on one chantier
        if spec in found_order.set_aside:
            continue
        try:
            pull = pull_state(
                env.runner, repo, record.pull, env.settings.trusted_logins,
                merge_queue.queue_cancelled(settings, record.pull),
            )  # fmt: skip
            if rerun_due(pull, record):
                # the worker judged its red unrelated (#1280): rerun, the pass's action
                if _rerun(env, spec, pull):
                    return spec
                continue
            machine = (
                machine_due(pull, record)
                and not merge_queue.holds(settings, pull.number)
                and _machine_red(env, pull)
            )
            if machine is None:
                # a failed job's log not given yet (#1919): neither rerun nor resume
                env.say(
                    texts.MACHINE_LOG_WAITS.format(
                        n=spec,
                        pull=pull.number,
                        minutes=machine_red.LOG_WAIT_S // 60,
                    )
                )
                continue
            if machine:
                # no failed job names a test (#1792): rerun, its worker asleep
                if _rerun(env, spec, pull, machine=True):
                    return spec
                continue
            if record.session is None:
                continue
            merged_red = (
                pull.state == "MERGED"
                and bool(pull.checks.failed)
                and not commented(
                    env.runner,
                    repo,
                    spec,
                    RED_MARKER.format(pull=pull.number),
                    env.settings.trusted_logins,
                )
            )
        except CommandError as exc:
            print(
                texts.RESUME_FAILED.format(n=spec, error=exc), file=env.err, flush=True
            )
            continue
        if not (merged_red or due_on_open(pull, record, env.now())):
            continue
        if not merged_red and merge_queue.holds(settings, pull.number):
            # the queue decides first (#1715): a machine's red is rerun
            # without the worker, a test's red leaves the queue, then resumes
            env.say(texts.RED_WAITS_QUEUE.format(n=spec, pull=pull.number))
            continue
        red_after_rerun = after_rerun(pull, record, env.now())
        # resumed once for this red, no push nor comment since (#2175)
        again = not merged_red and unanswered(pull, record, env.now())
        busy: list[int] = []
        if not found_order.sessions_free:
            safe = functools.partial(
                _safe_now, by_spec, since, since.get(spec), env.now()
            )
            victim = found_order.victim(spec, safe)
            if victim is None:
                said = found_order.describe_dev()
                env.say(
                    texts.RED_WAITS_SESSION.format(
                        n=spec, pull=pull.number, sessions=said
                    )
                )
                continue
            busy = [victim]
        red = texts.RED_DUE.format(n=spec, pull=pull.number)
        try:
            _admit_resume(env, state, red, spec, machine=not busy)
        except Refused as exc:
            if busy:
                raise  # the quota: it says no to every red
            # a red that adds a session waits for the machine; one that
            # replaces a session does not wait with it
            env.say(str(exc))
            held = held or exc
            continue
        if busy:
            why_not = _set_aside(env, by_spec[busy[0]], spec)
            if why_not is not None:
                env.say(texts.RED_WAITS_ASIDE.format(why=why_not, red=spec))
                continue
        try:
            if merged_red:
                text = RED_MESSAGE.format(
                    n=spec,
                    pull=pull.number,
                    names=", ".join(pull.checks.failed),
                    base=settings.base_ref,
                )
            else:
                text = resume_text(
                    env.runner, repo, settings.base_ref, pull, red_after_rerun,
                    machine=_machine_reran(env, pull.number, pull.head_sha),
                ) + merge_queue.resume_note(settings, pull.number)  # fmt: skip
                if again:
                    assert worker.worktree is not None
                    dirty = dirty_paths(env.runner, worker.worktree.path)
                    text = again_text(pull.number, record, dirty) + text
            chosen = spec_level(env, spec)
            runtime(env).resume(spec, record.session, text, chosen)
            _record_level(env, spec, chosen)
            env.say(runtime(env).github_auth(spec))
            env.say(texts.RESUMED_AT_LEVEL.format(n=spec, level=chosen.describe()))
        except (CommandError, OSError) as exc:
            failed = texts.RESUME_FAILED.format(n=spec, error=exc)
            if busy:  # a session was closed for it: the pass ends, broken
                raise CommandError(failed) from exc
            print(failed, file=env.err, flush=True)
            continue
        # The session is open: whatever fails now is said, and the pass ends —
        # one resume per pass.
        try:
            told = told_of(pull, record, env.now()) + (AGAIN if again else "")
            mark_resumed(settings, spec, told, env.now())
        except (OSError, sqlite3.Error, StoreError) as exc:
            print(texts.RESUME_NOT_MARKED.format(n=spec, error=exc), file=env.err)
        if merged_red:
            try:
                post_usage(
                    env.runner,
                    repo,
                    spec,
                    red_comment(pull.number, pull.checks.failed, True),
                )
            except CommandError as exc:
                print(texts.RESUME_NOT_MARKED.format(n=spec, error=exc), file=env.err)
        said = texts.RESUMED_AFTER_RERUN if red_after_rerun else texts.RESUMED_PARKED
        env.say(
            (texts.RESUMED_AGAIN if again else said).format(
                n=spec,
                pull=pull.number,
                window=env.window(spec),
                session=record.short_session,
            )
        )
        return spec
    if held is not None:
        raise held  # nothing new passes before it
    return None


def _preemptible(
    worker: Worker | None, in_dev_since: int | None, red_since: int | None, now: float
) -> bool:
    """A chantier in dev whose session a better ranked red may close now
    (#1823): alive, in dev for `order.HOLD` at least (no back and forth),
    and at a safe point — its worker at rest, or the red waiting for
    `order.PREEMPT_WAIT` already (the end of its step, at most)."""
    if worker is None or worker.window is None or worker.state is WorkerState.DEAD:
        return False
    if in_dev_since is not None and now - in_dev_since < order.HOLD:
        return False
    if worker.state is not WorkerState.WORKING:
        return True
    return red_since is not None and now - red_since >= order.PREEMPT_WAIT


def _safe_now(
    by_spec: Mapping[int, Worker],
    since: Mapping[int, int],
    red_since: int | None,
    now: float,
    spec: int,
) -> bool:
    return _preemptible(by_spec.get(spec), since.get(spec), red_since, now)


def _machine_red(env: Env, pull: PullState) -> machine_red.Verdict:
    """No failed job's log names a failed test (#1792); a log GitHub does
    not give yet is waited for, then reads as none (#1919): None meanwhile.
    A test's red is recorded for its head: its logs are read once."""
    settings = env.settings
    gh = merge_queue.GitHub(env.runner, settings)
    try:
        with closing(store.connect(settings.history_db)) as db:
            project, number, head = settings.github_repo, pull.number, pull.head_sha
            if machine_red.decided(db, project, number, head) is not None:
                return False
            code = machine_red.code_red_of(
                pull.checks, gh.step, settings.merge_queue.code_steps, gh.log
            )
            if code is not None:  # a code red, never re-run (#2080)
                machine_red.record_code(db, project, number, head, env.now(), code)
                env.say(texts.CODE_RED.format(pull=number, step=code))
                return False
            if machine_red.queue_reran(db, project, number, head):
                machine: machine_red.Verdict = False  # the queue's was the one
            else:
                machine = machine_red.is_machine_red(pull.checks, gh.log, env.now())
            if machine is None or machine:
                return machine
            machine_red.record(
                db, project, number, head, env.now(), machine_red.TEST_RED
            )
            return False
    except (CommandError, *order.BASE_ERRORS):
        return False


def _machine_reran(env: Env, pull: int, head: str) -> bool:
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            return machine_red.rerun_for(db, env.settings.github_repo, pull, head)
    except order.BASE_ERRORS:
        return False


def _refuse_queue_runs(
    env: Env, spec: int, pull: PullState, runs: list[FailedRun], machine: bool
) -> None:
    """Every failed run of the head is the merge queue's (#2110): none is
    rerun outside it. Decided for the head — a machine's red read as a
    test's, a mark as refused — and its worker resumed at the next pass,
    told the red as it is."""
    repo, sha = env.settings.github_repo, pull.head_sha
    try:
        if machine:
            with closing(store.connect(env.settings.history_db)) as db:
                machine_red.record(
                    db, repo, pull.number, sha, env.now(), machine_red.TEST_RED
                )
        else:
            mark_refused(env.settings, spec, sha)
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.QUEUE_RUN_NOT_RECORDED.format(n=spec, error=exc), file=env.err)
    ids = ", ".join(str(r.id) for r in runs)
    env.say(texts.RERUN_QUEUE_RUN.format(n=spec, pull=pull.number, runs=ids))


def _rerun(env: Env, spec: int, pull: PullState, machine: bool = False) -> bool:
    """Rerun once the failed runs of a head its worker marked « red
    unrelated » (#1280) — or whose red names no test (`machine`, #1792) —
    recorded and said; False: not now (a run not over, or unreadable). A
    rerun GitHub refuses is recorded all the same: tried once, the worker is
    resumed if still red — never stuck on a refusal.

    The queue's full suite red, judged unrelated (#2078): no rerun, the pull
    request goes back to the head of the queue — marked as rerun for its
    head all the same, so a red again there resumes the worker."""
    repo, sha = env.settings.github_repo, pull.head_sha
    if not machine:
        try:
            gh = merge_queue.GitHub(env.runner, env.settings)
            back = merge_queue.readmit(gh, env.settings, pull.number, sha, env.now())
            if back is not None:
                env.say(back)
            # sent back to the head for this head: never a rerun outside the
            # queue, even when its mark was not written then
            if back is not None or merge_queue.readmitted_for(
                env.settings, pull.number, sha
            ):
                mark_rerun(env.settings, spec, sha, env.now())
                return True
        except CommandError as exc:
            print(
                texts.RERUN_FAILED.format(n=spec, pull=pull.number, error=exc),
                file=env.err,
                flush=True,
            )
            return False
        except (OSError, sqlite3.Error, StoreError) as exc:
            print(texts.RERUN_NOT_RECORDED.format(n=spec, error=exc), file=env.err)
            return False
    short = low_memory.short(env.settings.meminfo)
    if short is not None:
        # #2096: each re-run under the threshold was killed again on 29/10
        env.say(texts.RERUN_WAITS_MEMORY.format(n=spec, pull=pull.number, mb=short))
        return False
    try:
        runs = failed_runs(env.runner, repo, sha)
        if runs is None:
            env.say(texts.RERUN_WAITS.format(n=spec, pull=pull.number))
            return False
        with closing(store.connect(env.settings.history_db)) as db:
            queue = [
                r for r in runs if machine_red.in_queue_test(db, repo, pull.number, r)
            ]
    except CommandError as exc:
        print(
            texts.RERUN_FAILED.format(n=spec, pull=pull.number, error=exc),
            file=env.err,
            flush=True,
        )
        return False
    except (OSError, sqlite3.Error, StoreError) as exc:
        # the queue's runs unread: never a rerun blind
        error = texts.RERUN_FAILED.format(n=spec, pull=pull.number, error=exc)
        print(error, file=env.err, flush=True)
        return False
    if runs and len(queue) == len(runs):
        _refuse_queue_runs(env, spec, pull, runs, machine)
        return False
    ids = [r.id for r in runs if r not in queue]
    if machine and not ids:
        # nothing to rerun (#1919): never said re-run — a test's red, resumed
        try:
            with closing(store.connect(env.settings.history_db)) as db:
                machine_red.record(
                    db, repo, pull.number, sha, env.now(), machine_red.TEST_RED
                )
        except (OSError, sqlite3.Error, StoreError) as exc:
            print(texts.RERUN_NOT_RECORDED.format(n=spec, error=exc), file=env.err)
        env.say(
            texts.MACHINE_NOTHING_TO_RERUN.format(n=spec, pull=pull.number, sha=sha[:9])
        )
        return False
    refused: CommandError | None = None
    try:
        rerun_failed(env.runner, repo, ids)
    except CommandError as exc:
        refused = exc
    try:
        mark_rerun(env.settings, spec, sha, env.now())
        if machine:
            with closing(store.connect(env.settings.history_db)) as db:
                machine_red.record(
                    db, repo, pull.number, sha, env.now(), machine_red.PARKED
                )
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.RERUN_NOT_RECORDED.format(n=spec, error=exc), file=env.err)
    if refused is not None:
        print(
            texts.RERUN_REFUSED.format(n=spec, pull=pull.number, error=refused),
            file=env.err,
            flush=True,
        )
        return True
    env.say(
        (texts.MACHINE_RERUN_DONE if machine else texts.RERUN_DONE).format(
            n=spec,
            pull=pull.number,
            sha=sha[:9],
            runs=", ".join(map(str, ids)) or texts.RERUN_NONE,
        )
    )
    return True


def _set_aside(env: Env, worker: Worker, for_spec: int | None) -> str | None:
    """Close a chantier's session, keep its id (#1263): resumed by
    start-next once a session is free. None: done; else why not — a session
    that could not be resumed is never closed. A record that cannot be
    written is broken (exit 3), its session said for a resume by hand."""
    n = worker.spec
    if worker.window is None:
        if for_spec is not None or worker.worktree is None:
            return texts.NO_WINDOW.format(window=env.window(n))
        return _set_aside_stopped(env, worker)
    if worker.state is WorkerState.DEAD:
        return texts.NOT_SET_ASIDE.format(n=n, why=texts.ASIDE_DEAD)
    if worker.provider != CLAUDE.name or worker.worktree is None:
        why = texts.ASIDE_NOT_CLAUDE.format(provider=worker.provider or "?")
        return texts.NOT_SET_ASIDE.format(n=n, why=why)
    session = latest_session(transcripts_root(), worker.worktree.path)
    if session is None:
        return texts.NOT_SET_ASIDE.format(n=n, why=texts.ASIDE_NO_SESSION)
    runtime(env).stop(n)
    try:
        order.set_aside(env.settings, n, session, for_spec, env.now())
    except order.BASE_ERRORS as exc:
        raise CommandError(
            texts.ASIDE_NOT_WRITTEN.format(n=n, session=session, error=exc)
        ) from exc
    if for_spec is None:
        env.say(
            texts.SET_ASIDE_BY.format(
                n=n, session=session[:8], who=env.settings.operator
            )
        )
    else:
        env.say(texts.SET_ASIDE_FOR.format(n=n, session=session[:8], red=for_spec))
    return None


def _set_aside_stopped(env: Env, worker: Worker) -> str | None:
    """A chantier stopped by hand, set aside after all (#2118): no window to
    close, its last Claude session kept; it enters the order again, and
    start-next resumes it before any new spec."""
    n = worker.spec
    assert worker.worktree is not None
    session = latest_session(transcripts_root(), worker.worktree.path)
    if session is None:
        return texts.NOT_SET_ASIDE.format(n=n, why=texts.ASIDE_NO_SESSION)
    try:
        order.set_aside(env.settings, n, session, None, env.now())
    except order.BASE_ERRORS as exc:
        raise CommandError(
            texts.ASIDE_NOT_WRITTEN.format(n=n, session=session, error=exc)
        ) from exc
    env.say(
        texts.SET_ASIDE_BY.format(n=n, session=session[:8], who=env.settings.operator)
    )
    enter(env, n)
    return None


def _resume_set_aside(
    env: Env, state: State, observed: list[Worker], found_order: order.Factory
) -> int | None:
    """Reopen the best ranked chantier set aside once a session is free
    (#1263, #1823) — its own session, told why it was set aside; None: none.
    The machine's guards and the quota say, and a refusal keeps its place."""
    by_spec = {w.spec: w for w in observed}
    for spec in sorted(found_order.set_aside, key=lambda s: found_order.rank(s) or 0):
        record = found_order.set_aside[spec]
        worker = by_spec.get(spec)
        if worker is None or worker.worktree is None or worker.window is not None:
            continue  # never two sessions on one chantier
        if not found_order.sessions_free:
            said = found_order.why_no_session()
            env.say(texts.ASIDE_WAITS.format(n=spec, sessions=said))
            return None
        _admit_resume(env, state, texts.ASIDE_DUE.format(n=spec), spec)
        text = order.resume_text(record, env.settings.operator)
        try:
            chosen = spec_level(env, spec)
            runtime(env).resume(spec, record.session, text, chosen)
        except (CommandError, OSError) as exc:
            print(
                texts.RESUME_FAILED.format(n=spec, error=exc), file=env.err, flush=True
            )
            continue
        _record_level(env, spec, chosen)
        env.say(runtime(env).github_auth(spec))
        env.say(texts.RESUMED_AT_LEVEL.format(n=spec, level=chosen.describe()))
        forget_aside(env, spec)
        env.say(
            texts.ASIDE_RESUMED.format(
                n=spec, window=env.window(spec), session=record.short_session
            )
        )
        return spec
    return None


def spec_level(env: Env, spec: int) -> level.Level:
    """The level the spec's labels ask for NOW (#2251): read at each
    resume, so that a label changed during the pause is obeyed."""
    try:
        labels = issue_labels(env.runner, env.settings.github_repo, spec)
    except CommandError as exc:  # a resume never waits on GitHub for a label
        return level.unreadable(env.settings.level, str(exc))
    return level.choose(labels, env.settings.level)


def _record_level(env: Env, spec: int, chosen: level.Level) -> None:
    """What the worker was started with, for `status` and the dashboard
    (#2252); a file that cannot be written is said, the worker runs."""
    try:
        level.record(env.settings.cache_dir, spec, chosen)
    except OSError as exc:
        print(texts.LEVEL_NOT_RECORDED.format(n=spec, error=exc), file=env.err)


def levels_of(env: Env, workers: Sequence[Worker]) -> dict[int, dict[str, Any]]:
    """Each worker's model and effort, asked and seen (#2252)."""
    root = transcripts_root()
    shown = {}
    for worker in workers:
        worktree = env.settings.conventions.worktree_path(worker.spec)
        usage = chantier_usage(root, worktree)
        seen = usage.seen() if usage else (None, None)
        asked = level.recorded(env.settings.cache_dir, worker.spec)
        shown[worker.spec] = level.view(asked, seen)
    return shown


def forget_aside(env: Env, spec: int, said: str | None = None) -> None:
    """Nothing left set aside for `spec`: resumed, relaunched, parked,
    stopped or removed. A base that fails is said."""
    try:
        order.forget_set_aside(env.settings, spec)
    except order.BASE_ERRORS as exc:
        print(texts.ASIDE_NOT_FORGOTTEN.format(n=spec, error=exc), file=env.err)
        return
    if said is not None:
        env.say(said)


def _k(n: int) -> str:
    return f"{n / 1000:,.0f} k"


def _line(label: str, t: Tokens) -> str:
    return texts.USAGE_LINE.format(
        label=label,
        total=_k(t.total),
        input=_k(t.input),
        cache_write=_k(t.cache_write),
        cache_read=_k(t.cache_read),
        output=_k(t.output),
        messages=t.messages,
    )


def transcripts_root() -> Path:
    """Where Claude Code keeps its session logs — read, never written."""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return root / "projects"


def cmd_usage(env: Env, args: argparse.Namespace) -> int:
    """What a chantier cost, from Claude Code's session logs of its worktree."""
    worktree = env.settings.conventions.worktree_path(args.spec)
    usage = chantier_usage(transcripts_root(), worktree)
    if usage is None:
        raise Refused(texts.NO_CLAUDE_SESSION.format(worktree=worktree))
    env.say(f"#{args.spec} — {worktree}")
    env.say(_line(texts.USAGE_OWNER, usage.owner_tokens()))
    for part in usage.subagents:
        env.say(_line(texts.USAGE_SUBAGENT.format(name=part.name), part.tokens))
    env.say(_line(texts.USAGE_SUBAGENTS, usage.subagent_tokens()))
    env.say(_line(texts.USAGE_TOTAL, usage.total()))
    span = usage.span()
    if span is not None:
        minutes = int((span[1] - span[0]).total_seconds() // 60)
        hours, rest = divmod(minutes, 60)
        env.say(texts.USAGE_DURATION.format(hours=hours, minutes=rest))
    return EXIT_OK


def _give_slot_back(
    env: Env, worker: Worker, dry_run: bool, record: Parked | None
) -> str | None:
    """Park a worker waiting on its open PR (#1025); None: keep it.

    At rest with no job of its own in the background, it goes whatever its
    CI says (27/09; #2175: a job younger than `[workers]
    background_max_minutes` keeps it, `_waits_in_background`). Otherwise
    — a `gh run watch` in a shell, or in the foreground — only while its
    CI runs and nothing has failed: then it only waits. At rest with its
    « red unrelated » mark on the head (#1280), it goes even with a shell
    or a run going: it only waits for the rerun, which is tracerworks's — done
    here if the run is over, else by `start-next`.
    """
    assert worker.worktree is not None and worker.worktree.branch
    n = worker.spec
    try:
        pull = open_pull_request(
            env.runner, env.settings.github_repo, worker.worktree.branch
        )
        if pull is None:
            return None
        state = pull_state(
            env.runner, env.settings.github_repo, pull, env.settings.trusted_logins,
            merge_queue.queue_cancelled(env.settings, pull),
        )  # fmt: skip
    except CommandError as exc:
        print(texts.FAILED.format(text=f"#{n}: {exc}"), file=env.err, flush=True)
        return None
    # A busy worker is parked only if it can be resumed: Claude's (#1025, the
    # Codex route is out of scope).
    underway = state.ci_underway and worker.provider == CLAUDE.name
    at_rest = worker.state is WorkerState.WAITING
    if not underway and not at_rest:
        return None  # working: `why_kept` says so
    marked = at_rest and state.marked and worker.provider == CLAUDE.name
    now = env.now()
    if not underway and not marked and in_resume_grace(state, record, now):
        assert record is not None and record.resumed_at is not None
        ago = int(now - record.resumed_at) // 60
        return texts.KEPT_RESUMED.format(n=n, pull=pull, minutes=ago)
    waits = None if underway or marked else _waits_in_background(env, n, pull)
    if waits is not None:
        return waits
    seen = told_of(state, record, now) if not underway and state.needs_worker else None
    said = _park(env, worker, pull, dry_run, texts.PARKED_OPEN, seen)
    record = record or Parked(n, None, pull, int(now), seen)
    if marked and not dry_run and rerun_due(state, record):
        _rerun(env, n, state)
    return said


def _waits_in_background(env: Env, n: int, pull: int) -> str | None:
    """Why a worker at rest is kept for a job of its own in the background
    (#2175), its scope read; None: park it — no job, or one past
    `[workers] background_max_minutes`, said. Its scope unknown: the
    footer's shells, as before."""
    ages = background.jobs(env.settings.proc, env.settings.conventions, n)
    if ages is None:
        if shells_running(env.tmux.try_capture(env.window(n))):
            return texts.KEPT_SHELL.format(n=n, pull=pull)
        return None
    if not ages:
        return None
    minutes, cap = int(ages[-1] // 60), env.settings.worker_background_max_minutes
    if minutes >= cap:
        env.say(texts.PARKED_PAST_BACKGROUND.format(n=n, minutes=minutes, cap=cap))
        return None
    return texts.KEPT_BACKGROUND.format(n=n, pull=pull, minutes=minutes, cap=cap)


def _park(
    env: Env,
    worker: Worker,
    pull: int,
    dry_run: bool,
    said: str,
    seen: str | None = None,
) -> str:
    """Close the session, keep its id: `start-next` resumes it if needed."""
    assert worker.worktree is not None
    n = worker.spec
    if dry_run:
        return texts.WOULD_PARK.format(n=n, pull=pull, window=env.window(n))
    session = (
        latest_session(transcripts_root(), worker.worktree.path)
        if worker.provider == CLAUDE.name
        else None
    )
    try:
        runtime(env).stop(n)
    except CommandError as exc:
        # the park is written anyway (23/10, #1682): without it, a red on
        # this PR never resumes its worker
        print(
            texts.PARK_NOT_CLOSED.format(n=n, window=env.window(n), error=exc),
            file=env.err,
            flush=True,
        )
    try:
        park(env.settings, n, session, pull, env.now(), seen)
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.PARK_NOT_WRITTEN.format(n=n, error=exc), file=env.err, flush=True)
    forget_aside(env, n)  # parked now: the newer state
    return said.format(
        n=n,
        pull=pull,
        window=env.window(n),
        session=session[:8] if session else texts.NO_SESSION,
    )


def _watched_after_merge(env: Env, worker: Worker) -> int | None:
    """The merged PR a working Claude worker watches in the foreground while
    its checks still run (08/10, #1021) — parkable; None: not that case."""
    if not (
        may_park(worker)
        and worker.state is WorkerState.WORKING
        and worker.provider == CLAUDE.name
    ):
        return None
    assert worker.worktree is not None and worker.worktree.branch
    repo = env.settings.github_repo
    try:
        pull = merged_pull_request(env.runner, repo, worker.worktree.branch)
        if pull is not None and pull_checks(env.runner, repo, pull).running:
            return pull
    except CommandError as exc:
        print(texts.FAILED.format(text=f"#{worker.spec}: {exc}"), file=env.err)
    return None


def _carried(env: Env, worker: Worker, dry_run: bool) -> bool:
    """The spec's carried labels put back on its open PR (#1297); False when
    GitHub could not be read — said, and the rest of `clean` goes on."""
    assert worker.worktree is not None and worker.worktree.branch
    settings = env.settings
    if dry_run or not settings.carried_labels:
        return True
    try:
        said = carry(
            env.runner,
            settings.github_repo,
            worker.spec,
            worker.worktree.branch,
            settings.carried_labels,
        )
    except CommandError as exc:
        print(texts.FAILED.format(text=f"#{worker.spec}: {exc}"), file=env.err)
        return False
    if said is not None:
        env.say(said)
    return True


def cmd_clean(env: Env, args: argparse.Namespace) -> int:
    """Remove the worktrees of merged chantiers, after posting their usage.

    Keeping a worktree is normal (exit 0); only a real failure — a fetch, a
    comment, a removal — makes the exit code 3, after every worktree was seen.
    """
    settings = env.settings
    repo = settings.github_repo
    base = BaseBranch(env.runner, settings)
    rules = BodyRules(settings.parent_keywords, settings.blocker_keywords)
    tickets = OpenTickets(env.runner, repo, rules, env.settings.trusted_logins)
    broken = False
    freed: list[int | None] = []
    removed: list[int] = []
    listed = read_worktrees(env.runner, settings)
    observed = observe_workers(env.runner, settings, env.tmux, listed or {})
    workers = [w for w in observed if w.worktree]
    records = parked_records(env)
    if not workers:
        env.say(texts.NO_WORKTREE)
    for worker in workers:
        assert worker.worktree is not None
        n, path = worker.spec, worker.worktree.path
        if worker.worktree.branch and not _carried(env, worker, args.dry_run):
            broken = True
        kept = why_kept(worker, env.window(n))
        merged: bool | None = None
        if kept is None or may_park(worker):
            merged = base.contains(worker)
            if kept is None and merged is None:
                kept = texts.MERGE_UNREADABLE.format(base=settings.base_ref)
            elif kept is None and not merged:
                kept = texts.NOT_MERGED.format(base=settings.base_ref)
        pull: int | None = None
        if kept is None and worker.worktree.branch:
            try:
                pull = merged_pull_request(env.runner, repo, worker.worktree.branch)
            except CommandError as exc:
                print(
                    texts.FAILED_KEPT.format(n=n, error=exc), file=env.err, flush=True
                )
                broken = True
                continue
        if kept is None and worker.window is not None and pull is None:
            kept = texts.WINDOW_OPEN_NO_PR.format(window=env.window(n))
        red: list[str] = []
        if kept is None and pull is not None:
            try:
                checks = pull_checks(env.runner, repo, pull)
                if checks.running and worker.window is not None:
                    # merged, its checks still run: the slot is given back
                    # now (08/10), the session kept for a red after the merge
                    env.say(_park(env, worker, pull, args.dry_run, texts.PARKED_MERGED))
                    continue
                if checks.running:
                    kept = texts.CHECKS_RUNNING.format(
                        pull=pull, names=", ".join(checks.running)
                    )
                elif checks.failed and not commented(
                    env.runner,
                    repo,
                    n,
                    RED_MARKER.format(pull=pull),
                    env.settings.trusted_logins,
                ):
                    red = checks.failed
            except CommandError as exc:
                print(
                    texts.FAILED_KEPT.format(n=n, error=exc), file=env.err, flush=True
                )
                broken = True
                continue
        if red and worker.window is not None:
            assert pull is not None
            try:
                env.say(_tell_red(env, n, pull, red, args.dry_run))
            except CommandError as exc:
                print(
                    texts.FAILED_KEPT.format(n=n, error=exc), file=env.err, flush=True
                )
                broken = True
            continue
        if red and worker.window is None and n in records and records[n].session:
            env.say(texts.RESUME_PENDING.format(n=n, pull=pull))
            continue
        watched = _watched_after_merge(env, worker) if merged and kept else None
        if watched is not None:
            env.say(_park(env, worker, watched, args.dry_run, texts.PARKED_MERGED))
            continue
        if merged is False and may_park(worker):
            gave = _give_slot_back(env, worker, args.dry_run, records.get(n))
            if gave is not None:
                env.say(gave)
                continue
        if kept is not None:
            env.say(texts.KEPT.format(n=n, why=kept))
            continue
        if worker.window is not None:
            if args.dry_run:
                env.say(texts.WOULD_CLOSE.format(n=n, pull=pull, window=env.window(n)))
            else:
                runtime(env).stop(n)
                env.say(texts.CLOSED.format(n=n, pull=pull, window=env.window(n)))
        size = size_kb(env.runner, path)
        usage = chantier_usage(transcripts_root(), path)
        try:
            if usage is None:
                measure = texts.NOTHING_TO_POST
            elif usage_posted(env.runner, repo, n, env.settings.trusted_logins):
                measure = texts.USAGE_ALREADY_POSTED
            elif args.dry_run:
                measure = texts.WOULD_POST.format(n=n)
            else:
                post_usage(env.runner, repo, n, usage_comment(n, usage, size))
                measure = texts.POSTED.format(n=n)
            if usage is not None and not args.dry_run:
                _record_chantier(env, n, usage)
        except CommandError as exc:
            print(
                texts.USAGE_NOT_POSTED.format(n=n, error=exc), file=env.err, flush=True
            )
            broken = True
            continue
        if args.dry_run:
            freed.append(size)
            env.say(
                texts.WOULD_REMOVE.format(
                    n=n, measure=measure, path=path, size=describe_size(size)
                )
            )
            if pull is not None and not red:
                try:
                    still = tickets.of(n)
                except CommandError as exc:
                    print(texts.FAILED.format(text=f"#{n}: {exc}"), file=env.err)
                    broken = True
                    continue
                if still:
                    env.say(
                        texts.WOULD_KEEP_SPEC.format(n=n, tickets=ticket_refs(still))
                    )
            continue
        # the spec's fate never hangs on the removal (#1907): a worktree git
        # refuses to remove stays, said, and the spec is closed all the same
        try:
            gone = remove_worktree(env.runner, settings, worker)
        except CommandError as exc:
            failed = texts.REMOVE_FAILED.format(n=n, measure=measure, error=exc)
            print(failed, file=env.err, flush=True)
            broken = True
        else:
            if n in records:
                try:
                    forget(settings, n)
                except (OSError, sqlite3.Error, StoreError) as exc:
                    print(texts.FORGET_FAILED.format(n=n, error=exc), file=env.err)
            forget_aside(env, n)
            forget_wait(env, n)
            removed.append(n)
            if gone or not path.exists():
                freed.append(size)  # a folder left frees nothing
            if gone:
                said = texts.REMOVED.format(
                    n=n, measure=measure, path=path, size=describe_size(size)
                )
            else:
                left = texts.FOLDER_LEFT if path.exists() else ""
                said = texts.NO_LONGER_A_WORKTREE.format(
                    n=n, measure=measure, path=path, left=left
                )
            env.say(said)
            leave(env, n)
        if pull is not None and red:
            names = ", ".join(red)
            try:
                if not args.dry_run:
                    post_usage(env.runner, repo, n, red_comment(pull, red, False))
                env.say(texts.SPEC_KEPT_RED.format(n=n, pull=pull, names=names))
            except CommandError as exc:
                print(texts.FAILED_KEPT.format(n=n, error=exc), file=env.err)
                broken = True
        elif pull is not None:
            try:
                still = tickets.of(n)
                if still:
                    keep_spec_open(
                        env.runner,
                        repo,
                        n,
                        pull,
                        still,
                        settings.progress_label,
                        settings.trusted_logins,
                        settings.launch_label,
                    )
                    env.say(
                        texts.SPEC_KEPT_TICKETS.format(
                            n=n,
                            pull=pull,
                            tickets=ticket_refs(still),
                            launch=settings.launch_label,
                        )
                    )
                elif close_spec(env.runner, repo, n, pull):
                    env.say(texts.SPEC_CLOSED.format(n=n, pull=pull))
            except CommandError as exc:
                print(texts.SPEC_NOT_CLOSED.format(n=n, error=exc), file=env.err)
                broken = True
    # a list git could not give is never « no worktree » (#2077)
    if listed is not None:
        observed_specs = {w.spec for w in observed}
        if not _close_delivered(env, tickets, observed_specs, args.dry_run):
            broken = True
        if not args.dry_run:
            _watch_worktrees(env, listed.keys(), removed)
    if freed:
        known = sum(kb for kb in freed if kb is not None)
        unknown = texts.UNKNOWN_SIZES if None in freed else ""
        verb = texts.WOULD_FREE if args.dry_run else texts.FREED_VERB
        total = describe_size(known)
        env.say(
            texts.FREED.format(verb=verb, size=total, unknown=unknown, count=len(freed))
        )
    if not args.dry_run:
        _advance_queue(env)
        _stop_overdue(env)
    return EXIT_BROKEN if broken else EXIT_OK


def _stop_overdue(env: Env) -> None:
    """A worker's test past `[workers] test_max_minutes` (#2096), last in
    `clean`: its group ended, a line in the history and here, its worker told.
    No /proc (a Mac): nothing. A worker that cannot be told: said here."""
    settings = env.settings

    def now_overdue() -> list[overdue.Overdue] | None:
        return overdue.overdue(
            settings.proc,
            settings.conventions,
            settings.worker_test_commands,
            settings.worker_test_max_minutes,
        )

    found = now_overdue()
    if not found:
        return
    overdue.stop(found, now_overdue, sleep=env.sleep)
    rows = [
        {
            "project": settings.github_repo,
            "spec": one.spec,
            "at": int(env.now()),
            "pid": min(one.pids),
            "command": one.command,
            "minutes": one.minutes,
            "memory_mb": one.memory_mb,
        }
        for one in found
    ]
    try:
        with closing(store.connect(settings.history_db)) as db:
            store.insert(db, "test_kill", rows)
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
    for one in found:
        pids = ", ".join(map(str, one.pids))
        said = texts.TEST_STOPPED.format(
            n=one.spec,
            command=one.command,
            pids=pids,
            minutes=one.minutes,
            mb=one.memory_mb,
        )
        try:
            deliver(
                env,
                one.spec,
                overdue.TOLD.format(
                    command=one.command,
                    pids=pids,
                    minutes=one.minutes,
                    mb=one.memory_mb,
                    limit=settings.worker_test_max_minutes,
                ),
            )
        except (Refused, CommandError) as exc:
            said += texts.TEST_STOPPED_UNTOLD.format(error=exc)
        env.say(said)


def _close_delivered(
    env: Env, tickets: OpenTickets, observed: set[int], dry_run: bool
) -> bool:
    """The open specs with the launch label and no worktree (#2077): one
    delivered — no ticket left open, a merged pull request on its branch,
    none open, its checks done and green, not reopened since — closes, its
    usage posted once.
    False when something failed (said)."""
    settings = env.settings
    repo, trusted = settings.github_repo, settings.trusted_logins
    try:
        specs = [
            i
            for i in tickets.every_open()
            if settings.launch_label in i.labels
            and i.parent is None
            and i.author in trusted
            and i.number not in observed
            and not tickets.of(i.number)
        ]
        if not specs:
            return True
        conventions = settings.conventions

        def spec_of(branch: str) -> int | None:
            parsed = conventions.parse_branch(branch)
            return parsed[0] if parsed else None

        pulls = spec_pulls(env.runner, repo, spec_of)
    except CommandError as exc:
        print(texts.FAILED.format(text=str(exc)), file=env.err, flush=True)
        return False
    fine = True
    for spec in specs:
        n = spec.number
        mine = pulls.get(n)
        if mine is None or not mine.merged or mine.opened:
            continue
        pull, merged_at = mine.merged[0]
        try:
            reopened = last_reopened_at(env.runner, repo, n)
            if reopened is not None and reopened > merged_at:
                continue  # reopened after its merge: the operator's (#2077)
            checks = pull_checks(env.runner, repo, pull)
            if checks.running:
                continue
            if checks.failed:
                _red_without_worktree(env, n, pull, checks, dry_run)
                continue
            measure = _usage_without_worktree(env, n, dry_run)
        except CommandError as exc:
            print(texts.SPEC_NOT_CLOSED.format(n=n, error=exc), file=env.err)
            fine = False
            continue
        if dry_run:
            env.say(texts.WOULD_CLOSE_GONE.format(n=n, pull=pull, measure=measure))
            continue
        try:
            if close_spec(env.runner, repo, n, pull):
                env.say(texts.SPEC_CLOSED_GONE.format(n=n, pull=pull, measure=measure))
        except CommandError as exc:
            print(texts.SPEC_NOT_CLOSED.format(n=n, error=exc), file=env.err)
            fine = False
    return fine


def _red_without_worktree(
    env: Env, n: int, pull: int, checks: Checks, dry_run: bool
) -> None:
    """Red after the merge, no worker left: said once on the spec, which
    stays open — as with a worktree."""
    repo, trusted = env.settings.github_repo, env.settings.trusted_logins
    if commented(env.runner, repo, n, RED_MARKER.format(pull=pull), trusted):
        return
    if not dry_run:
        post_usage(env.runner, repo, n, red_comment(pull, checks.failed, False))
    names = ", ".join(checks.failed)
    env.say(texts.SPEC_KEPT_RED.format(n=n, pull=pull, names=names))


def _usage_without_worktree(env: Env, n: int, dry_run: bool) -> str:
    """The usage of a chantier whose worktree is gone, read from Claude's
    logs filed under its path, posted once; what was done, said."""
    path = env.settings.conventions.worktree_path(n)
    usage = chantier_usage(transcripts_root(), path)
    repo = env.settings.github_repo
    if usage is None:
        return texts.NOTHING_TO_POST
    if usage_posted(env.runner, repo, n, env.settings.trusted_logins):
        return texts.USAGE_ALREADY_POSTED
    if dry_run:
        return texts.WOULD_POST.format(n=n)
    post_usage(env.runner, repo, n, usage_comment(n, usage, None, absent=True))
    _record_chantier(env, n, usage)
    return texts.POSTED.format(n=n)


def _watch_worktrees(env: Env, listed: Collection[int], removed: list[int]) -> None:
    """The worktrees gone since the last pass without `clean` (#2077): said
    once each; a base that fails is said, never a reason to fail `clean`."""
    project = env.settings.github_repo
    try:
        with closing(store.connect(env.settings.history_db)) as db:
            gone = worktree_watch.watch(db, project, listed, removed, env.now())
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.VANISHED_NOT_WRITTEN.format(error=exc), file=env.err)
        return
    for spec, last in gone:
        seen = datetime.fromtimestamp(last, UTC).strftime("%H:%M:%S")
        env.say(texts.WORKTREE_VANISHED.format(n=spec, seen=seen))


def _advance_queue(env: Env) -> None:
    """The merge queue's pass (#1714), last in `clean`: the relauncher runs
    `clean` then `start-next` every minute. GitHub or the base unreadable is
    said, like the history's other writes, and the queue stays as recorded:
    the next pass reads the world again."""
    try:
        said = merge_queue.advance(
            merge_queue.GitHub(env.runner, env.settings), env.settings, env.now()
        )
    except (CommandError, *merge_queue.BASE_ERRORS) as exc:
        print(texts.QUEUE_UNREADABLE.format(error=exc), file=env.err, flush=True)
        said = []
    for line in said:
        env.say(line)
    _tend_open_pulls(env)
    _tend_flaky_bugs(env)


def _tend_flaky_bugs(env: Env) -> None:
    """A test recognised flaky opens, comments and closes its bug (#2224).
    The base unreadable is said; GitHub's refusals are said per test."""
    try:
        said = flaky_bugs.tend(
            merge_queue.GitHub(env.runner, env.settings), env.settings, env.now()
        )
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.FLAKY_SELF_UNREADABLE.format(error=exc), file=env.err, flush=True)
        said = []
    for line in said:
        env.say(line)


def _tend_open_pulls(env: Env) -> None:
    """The open pull requests, read once (#1790, #1791, #2013): the run of a
    worker's head in conflict is cancelled, once per head; a head left without
    CI sees its last run re-run, once per head; a conflict with the base
    wakes its worker, or is commented and alerted. GitHub or the base
    unreadable: said, the next pass again."""
    settings = env.settings
    gh = merge_queue.GitHub(env.runner, settings)

    def tell(spec: int, text: str) -> None:
        try:
            deliver(env, spec, text)
        except Refused as exc:
            raise CommandError(str(exc)) from exc

    conventions = settings.conventions
    windows = {
        spec
        for window in env.tmux.list_windows()
        if (spec := conventions.spec_of_window(window.name)) is not None
    }
    now, repo = int(env.now()), settings.github_repo
    try:
        pulls = ci_revival.open_pulls(gh, settings)
        with closing(store.connect(settings.history_db)) as db:
            said = conflict_runs.tend(db, gh, settings, pulls, now)
            said += ci_revival.revive(db, gh, settings, now, pulls)
            parked = parked_records(env)
            said += conflicts.tend(
                db, gh, settings, pulls, now,
                windows=windows, parked=parked, tell=tell,
            )  # fmt: skip
            said += machine_red.tend_unworked(
                db, settings, pulls, now,
                windows=windows, parked=parked, log=gh.log,
                failed_runs=lambda sha: failed_runs(env.runner, repo, sha),
                rerun=lambda runs: rerun_failed(env.runner, repo, list(runs)),
                memory_short=low_memory.short(settings.meminfo),
                step=gh.step,
            )  # fmt: skip
    except (CommandError, *merge_queue.BASE_ERRORS) as exc:
        print(texts.CI_REVIVAL_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return
    for line in said:
        env.say(line)


def _tell_red(env: Env, n: int, pull: int, red: list[str], dry_run: bool) -> str:
    """Tell the worker its merged PR went red, then mark the spec: once.

    The message goes first: if the comment then fails, the next pass tells
    red_after_rerun rather than closing a worker that never heard of it.
    """
    said = {"n": n, "pull": pull, "names": ", ".join(red), "window": env.window(n)}
    if dry_run:
        return texts.WOULD_TELL_RED.format(**said)
    base = env.settings.base_ref
    text = RED_MESSAGE.format(n=n, pull=pull, names=said["names"], base=base)
    try:
        delivery = runtime(env).send(n, text, stamp(env))
    except (NotIdleError, NoWorkerError) as exc:
        return texts.RED_NOT_TOLD.format(**said, error=exc)
    if not delivery.received:
        return texts.RED_NOT_TOLD.format(**said, error="; ".join(delivery.attempts))
    post_usage(env.runner, env.settings.github_repo, n, red_comment(pull, red, True))
    return texts.RED_TOLD.format(**said)


def _record_chantier(env: Env, spec: int, usage: ChantierUsage) -> None:
    """Into the history base (#836); a base that fails is said, never a
    reason to keep a worktree: the comment on the spec is the record."""
    try:
        record_chantier(env.settings, spec, usage, env.now())
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.HISTORY_NOT_WRITTEN.format(n=spec, error=exc), file=env.err)


def cmd_db(env: Env, args: argparse.Namespace) -> int:
    """The history base, read only (#838): one SQL statement, or its tables."""
    path = env.settings.history_db
    try:
        if args.schema:
            env.say(describe_schema(schema(path)))
            return EXIT_OK
        if not args.sql:
            raise Refused(texts.SQL_OR_SCHEMA)
        columns, rows = query(path, args.sql)
    except QueryError as exc:
        raise Refused(str(exc)) from exc
    env.say(as_json(columns, rows) if args.json else as_table(columns, rows))
    return EXIT_OK


def cmd_alert_test(env: Env, args: argparse.Namespace) -> int:
    """A test alert, then its resolution (#841): proves the bot, the token and
    the chat, without waiting for a real episode."""
    settings = env.settings
    try:
        send = telegram(settings.alerts_token_file, settings.alerts_chat_id)
        send(texts.TEST_ALERT)
        env.say(texts.TEST_ALERT_SENT)
        send(texts.TEST_RESOLVED)
    except AlertError as exc:
        raise Refused(str(exc)) from exc
    env.say(texts.RESOLVED_SENT)
    return EXIT_OK


def cmd_db_backup(env: Env, args: argparse.Namespace) -> int:
    """Today's copy of the history base, now (#842); serve takes it nightly."""
    try:
        copy = take(env.settings.history_db, env.settings.backup_dir, env.now())
    except BackupError as exc:
        raise Refused(str(exc)) from exc
    env.say(texts.BACKUP_TAKEN.format(copy=copy, days=KEEP_DAYS, folder=copy.parent))
    return EXIT_OK


def cmd_provider(env: Env, args: argparse.Namespace) -> int:
    state = load(env.settings.state_file)
    if args.name == "codex" and not state.codex_available:
        raise Refused(CODEX_CLOSED)
    update(env.settings.state_file, lambda now: assign(now, {"provider": args.name}))
    env.say(texts.PROVIDER_SET.format(name=args.name))
    return EXIT_OK


def cmd_quota(env: Env, args: argparse.Namespace) -> int:
    if not 0 <= args.percent <= 100:
        raise Refused(texts.QUOTA_RANGE)
    _, state = update(
        env.settings.state_file,
        lambda now: assign(now, {"claude.weekly_used": args.percent}),
    )
    env.say(describe_quota(state))
    if args.percent >= state.claude_weekly_cap:
        env.say(texts.CAP_REACHED)
    return EXIT_OK


def cmd_stop(env: Env, args: argparse.Namespace) -> int:
    """Refused while anything is unsaved; `--force` is the cockpit's to pass,
    and only after the operator confirmed it in the conversation."""
    spec: int = args.spec
    worker = runtime(env).status(spec)
    if worker is None:
        if _cancel_wait(env, spec):
            return EXIT_OK
        raise Refused(texts.NO_WORKER_FOR.format(n=spec))
    if worker.window is None:
        env.say(texts.ALREADY_STOPPED.format(n=spec, saved=describe_saved(worker)))
        release(env, spec)
        _propose_set_aside(env, worker)
        return EXIT_OK
    unsaved = worker.worktree is not None and (
        worker.saved is None or not worker.saved.everything_pushed
    )
    if unsaved and not args.force:
        reason = texts.UNSAVED.format(n=spec, saved=describe_saved(worker))
        if worker.state is WorkerState.DEAD:
            raise Refused(texts.DEAD_UNSAVED.format(reason=reason))
        try:
            deliver(
                env,
                spec,
                WIP_REQUEST.format(
                    n=spec, operator=env.settings.operator, remote=env.settings.remote
                ),
            )
            asked = texts.WIP_ASKED
        except Refused as exc:
            asked = texts.WIP_NOT_RECEIVED.format(error=exc)
        raise Refused(texts.STOP_AGAIN.format(reason=reason, asked=asked))
    runtime(env).stop(spec)
    kept = (
        texts.WORKTREE_KEPT.format(path=worker.worktree.path) if worker.worktree else ""
    )
    if unsaved:
        kept += texts.NOT_SAVED.format(saved=describe_saved(worker))
    env.say(texts.STOPPED.format(window=env.window(spec), kept=kept))
    release(env, spec)
    _propose_set_aside(env, worker)
    return EXIT_OK


def _cancel_wait(env: Env, spec: int) -> bool:
    """A spec that never started, only waiting for a place (#2118): `stop`
    takes it out. False: it did not wait."""
    try:
        waited = order.forget_place_wait(env.settings, spec)
    except order.BASE_ERRORS as exc:
        raise Refused(texts.PLACE_WAIT_NOT_FORGOTTEN.format(n=spec, error=exc)) from exc
    if waited:
        why = texts.STOPPED_BY.format(who=env.settings.operator)
        env.say(texts.PLACE_WAIT_CLOSED.format(n=spec, why=why))
    return waited


def _propose_set_aside(env: Env, worker: Worker) -> None:
    """A chantier stopped with work left — its worktree, no merged pull
    request — loses its rank (#2118): `set-aside` brings it back before any
    new spec, even now. Said last; nothing when GitHub cannot tell."""
    if worker.worktree is None or worker.worktree.branch is None:
        return
    try:
        merged = merged_pull_request(
            env.runner, env.settings.github_repo, worker.worktree.branch
        )
    except CommandError:
        return
    if merged is None:
        env.say(texts.STOP_SET_ASIDE.format(n=worker.spec))


def release(env: Env, spec: int) -> None:
    """An explicit stop: nothing resumes the chantier — parked or set aside —
    and it leaves the order (#1823); `start` takes it again."""
    try:
        forget(env.settings, spec)
    except (OSError, sqlite3.Error, StoreError) as exc:
        print(texts.FORGET_FAILED.format(n=spec, error=exc), file=env.err)
    forget_aside(env, spec)
    forget_wait(env, spec)
    leave(env, spec)


def cmd_set_aside(env: Env, args: argparse.Namespace) -> int:
    """Set a chantier aside by hand (#1263): its session closed, its id kept;
    start-next resumes it once a session is free. Refused when its session
    could not be resumed."""
    worker = runtime(env).status(args.spec)
    if worker is None:
        raise Refused(texts.NO_WORKER_FOR.format(n=args.spec))
    why_not = _set_aside(env, worker, None)
    if why_not is not None:
        raise Refused(why_not)
    return EXIT_OK


def snapshot_of(
    env: Env, memo: Memo | None = None, cpu: CpuReading | None = None
) -> dict[str, Any]:
    return build_snapshot(
        env.runner,
        env.settings,
        env.tmux,
        load(env.settings.state_file),
        env.now(),
        transcripts_root(),
        memo,
        cpu=cpu,
    )


def cmd_statusline(env: Env, args: argparse.Namespace) -> int:
    """The Claude workers' status line (#822): records the account's usage
    Claude Code hands it on stdin, prints it. Never fails: a status line that
    errs would only blank the worker's footer."""
    payload = env.stdin.read().decode("utf-8", errors="replace")
    line = record_statusline(payload, env.settings.claude_quota_file, env.now())
    if line:
        env.say(line)
    return EXIT_OK


def cmd_snapshot(env: Env, args: argparse.Namespace) -> int:
    """Everything the dashboard shows, as JSON — read only."""
    env.say(json.dumps(snapshot_of(env), ensure_ascii=False, indent=2))
    return EXIT_OK


def tend_waits(
    env: Env, db: sqlite3.Connection, snapshot: dict[str, Any], now: int
) -> list[str]:
    """A worker waiting for a human past `[waits] human_minutes` (#1730):
    the human label on its spec with a comment, taken off when it works
    again. GitHub unreachable: said, tried again at the next snapshot."""
    settings = env.settings

    def gh(argv: list[str], body: str | None) -> None:
        stdin = body.encode("utf-8") if body is not None else None
        checked(env.runner(["gh", *argv], stdin=stdin), f"gh {' '.join(argv[:2])}")

    specs = [w["spec"] for w in snapshot.get("workers") or [] if "spec" in w]
    return waits.tend(
        db,
        gh,
        project=settings.github_repo,
        label=settings.human_label,
        specs=specs,
        now=now,
        after_minutes=settings.waits.human_minutes,
        window=env.window,
        session=settings.tmux_session,
    )


#: How many of its last changes a worker's card carries (#1729).
CARD_HISTORY = 8


def with_history(
    env: Env, db: sqlite3.Connection, snapshot: dict[str, Any], now: int
) -> None:
    """What the workshop shows of the history, on each worker (#1729, #1730):
    since when it is in its state, its last changes with their causes (its
    story in one click), and its alert when parked red past its deadline."""
    project = env.settings.github_repo
    workers = [w for w in snapshot.get("workers") or [] if "spec" in w]
    specs = [w["spec"] for w in workers]
    entered = chantier_state.since(db, project, specs)
    nudged = nudge_words(db, project, specs)
    stale = waits.stale_parked(
        db, project, specs, now, env.settings.waits.parked_red_minutes
    )
    for worker in workers:
        spec = worker["spec"]
        steps = chantier_state.steps(db, project, spec)[-CARD_HISTORY:]
        worker["phase_since"] = entered.get(spec)
        worker["history"] = chantier_state.describe_history(steps, spec)
        worker["nudge"] = nudged.get(spec)
        worker["alert"] = (
            texts.CARD_PARKED_RED.format(
                minutes=(now - int(stale[spec][0])) // 60, why=stale[spec][1]
            )
            if spec in stale
            else None
        )


def recorded_states(env: Env, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Each snapshot of `serve` writes the chantiers' changes of state to the
    history base (#1729), tells the waits past their deadline (#1730), and
    carries back what the workshop shows of that history; a refused change
    is journaled. An unreadable base is said, the snapshot shown all the same."""
    settings = env.settings
    now = int(env.now())
    try:
        with closing(store.connect(settings.history_db)) as db:
            written = chantier_state.record_snapshot(
                db, settings.github_repo, snapshot, now
            )
            told = tend_waits(env, db, snapshot, now)
            with_history(env, db, snapshot, now)
    except order.BASE_ERRORS as exc:
        print(texts.HISTORY_UNREADABLE.format(error=exc), file=env.err, flush=True)
        return snapshot
    for line in told:
        print(line, file=env.err, flush=True)
    for spec, step in written:
        if step.refused:
            said = chantier_state.State(step.phase, step.reason, step.cause)
            print(
                texts.TRANSITION_REFUSED.format(spec=spec, state=said.describe()),
                file=env.err,
                flush=True,
            )
    return snapshot


#: How long a `gh api` read serves `serve`'s threads (#2196): within one
#: cycle of the snapshot and the history's minute, never across two.
READ_ONCE_S = 30.0


def cmd_serve(env: Env, args: argparse.Namespace) -> int:
    """The dashboard, until stopped (a `systemd --user` service). One `gh` at
    a time across its threads, and the heap handed back after each snapshot:
    its memory, bounded (#2195). Its CPU at rest (#2196): one reading of a
    `gh api` source per cycle, whichever thread asks, and a full snapshot
    every few minutes when nobody watches."""
    # a `gh api` read serves every thread for READ_ONCE_S (#2196), outside
    # the gate: a read already made never queues behind another `gh`
    runner = read_once(one_at_a_time(env.runner, "gh"), env.now, READ_ONCE_S)
    env = replace(env, runner=runner)
    settings = env.settings
    try:
        secret = read_secret(settings.serve_secret_file)
    except SecretError as exc:
        raise Refused(str(exc)) from exc
    memo: Memo = {}  # finished runs' runners, asked once for the life of serve
    # The history (#836): recorded by the same service, visitor or not; its
    # CPU reading (a difference between two minutes) is the dashboard's too.
    recorder = Recorder(
        settings,
        env.runner,
        env.now,
        err=env.err,
        resting=lambda: resting_specs(settings, env.tmux),
        asking=lambda: asking_specs(settings, env.tmux),
        strays=find_strays,
    )
    cache = SnapshotCache(
        lambda: recorded_states(env, snapshot_of(env, memo, recorder.cpu)),
        env.now,
        store=settings.cache_dir / "snapshot.json",
        release=release_memory,
    )
    cache.warm()
    stop = threading.Event()
    threading.Thread(
        target=cache.keep_fresh, args=(stop,), name="snapshot-cycle", daemon=True
    ).start()
    threading.Thread(
        target=recorder.keep_recording, args=(stop,), name="history", daemon=True
    ).start()
    history = {
        "trends": lambda _: build_trends(
            settings.history_db,
            settings.github_repo,
            env.now(),
            settings.actions_slope_days,
            settings.claude_week_slope_days,
            settings.claude_week_half_life_hours,
            settings.actions_half_life_hours,
        ),
        "quality": lambda _: build_quality(
            settings.history_db,
            settings.github_repo,
            settings.ci_workflow,
            settings.base_branch,
            env.now(),
            settings.operator,
            settings.human_label,
        ),
    }
    history["measures"] = lambda query: (
        build_measures(
            settings.history_db,
            settings.github_repo,
            settings.ci_workflow,
            settings.base_branch,
            settings.merge_queue.night_event,
            env.now(),
            span,
            pace=Pace(
                settings.actions_slope_days,
                settings.claude_week_slope_days,
                settings.actions_half_life_hours,
                settings.claude_week_half_life_hours,
            ),
        )
        if (span := span_of(query)) is not None
        else {"error": texts.SPAN_EXPECTED}
    )
    history["delivered"] = lambda query: (
        build_delivery(
            settings.history_db,
            settings.github_repo,
            settings.ci_workflow,
            int(query["pr"]),
        )
        if query.get("pr", "").isdigit()
        else {"error": texts.PR_EXPECTED}
    )
    # its writes: the operator's star, through gh (#1877), and the Settings
    # tab, in the state file the command line writes too (#2190)
    star = Star(env.runner, settings.github_repo, settings.star_label)
    dashboard = Dashboard(
        secret,
        PAGE_FILE.read_bytes(),
        cache,
        env.now,
        history,
        star=star,
        state=settings.state_file,
        defaults=state_defaults(settings),
    )
    server = make_server(dashboard, settings.serve_host, settings.serve_port)
    newer: list[int] = []

    def restart(version: int) -> None:
        newer.append(version)
        server.shutdown()

    threading.Thread(
        target=watch_base,
        args=(settings.history_db, stop, restart, BASE_WATCH_S),
        name="base-watch",
        daemon=True,
    ).start()
    env.say(
        texts.DASHBOARD_ON.format(host=settings.serve_host, port=settings.serve_port)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    if newer:
        line = texts.SERVE_BASE_NEWER.format(
            current=newer[0], known=SCHEMA_VERSION, code=EXIT_NEWER_BASE
        )
        print(line, file=env.err, flush=True)
        return EXIT_NEWER_BASE
    return EXIT_OK


def cmd_serve_guarded(env: Env, args: argparse.Namespace) -> int:
    """Exit 1 is a refusal, which the unit does NOT restart: any crash must
    exit 3, which it does."""
    try:
        return cmd_serve(env, args)
    except (Refused, CommandError, StateFileError, OSError):
        raise
    except Exception as exc:
        print(f"tracerworks: serve: {exc!r}", file=env.err, flush=True)
        return EXIT_BROKEN


def cmd_self_update(env: Env, args: argparse.Namespace) -> int:
    installed = self_update(env.runner, env.settings)
    unit = env.settings.systemd_dir / f"{env.settings.serve_service}.service"
    try:
        changed = install_service(env.runner, env.settings)
    except CommandError as exc:
        # The tool IS installed: say so, and that only the service failed.
        raise CommandError(
            texts.SERVICE_FAILED.format(
                current=installed.current[:9], unit=unit.name, error=exc
            )
        ) from exc
    before = installed.previous[:9] if installed.previous else texts.NOTHING
    env.say(
        texts.UPDATED.format(
            before=before,
            current=installed.current[:9],
            base=env.settings.base_ref,
            launcher=env.settings.bin_dir / "tracerworks",
        )
    )
    how = texts.UNIT_WRITTEN if changed else texts.UNIT_UNCHANGED
    env.say(texts.SERVICE_UNIT.format(unit=unit.name, how=how, path=unit))
    return EXIT_OK


def in_process(env: Env, argv: Sequence[str], stdin: bytes | None) -> Outcome:
    """One tracerworks command run here, its output captured: what the MCP door
    hands back. Whatever it prints to the real stdout lands in its output,
    never in the protocol's stream; its stdin is `stdin`, never the door's."""
    out, err = io.StringIO(), io.StringIO()
    inner = replace(env, stdin=io.BytesIO(stdin or b""), out=out, err=err)
    with contextlib.redirect_stdout(out):
        code = run(inner, argv)
    text = "\n".join(p.rstrip("\n") for p in (out.getvalue(), err.getvalue()) if p)
    return Outcome(code, text)


def cmd_mcp(env: Env, args: argparse.Namespace) -> int:
    """The door of a remote session (#2185): MCP on stdio, a closed list of
    tools (`tracerworks.mcp`)."""
    door = Door(
        settings=env.settings,
        runner=env.runner,
        tmux=env.tmux,
        tracerworks=functools.partial(in_process, env),
        uid=os.getuid(),
    )
    return Server(door).serve(env.stdin, env.out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracerworks",
        description="Drive the development workers of a project, stateless.",
    )
    parser.add_argument(
        "--config",
        help="the project file (default: $TRACERWORKS_CONFIG, else"
        " ~/.config/tracerworks/project.toml)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status", help="workers, runner, memory, provider, quota"
    )
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(handler=cmd_status)

    history = commands.add_parser(
        "history", help="a spec's history: each change of its state, its cause"
    )
    history.add_argument("spec", type=int, help="number of the spec")
    history.set_defaults(handler=cmd_history)

    message = commands.add_parser(
        "message", help="send a message now (stdin or --file), verify it was received"
    )
    message.add_argument("spec", type=int, help="number of the spec")
    message.add_argument("--file", type=Path, help="read the message from this file")
    message.add_argument(
        "--wait-idle",
        action="store_true",
        help=f"wait for rest (up to {WAIT_IDLE_SECONDS:.0f} s) before sending",
    )
    message.set_defaults(handler=cmd_message)

    pause = commands.add_parser("pause", help="interrupt the worker (Escape)")
    pause.add_argument("spec", type=int, help="number of the spec")
    pause.set_defaults(handler=cmd_pause)

    resume = commands.add_parser("resume", help="relaunch it with a resume instruction")
    resume.add_argument("spec", type=int, help="number of the spec")
    resume.add_argument("--file", type=Path, help="extra instruction appended")
    resume.set_defaults(handler=cmd_resume)

    start = commands.add_parser("start", help="worktree + branch + window for a spec")
    start.add_argument("spec", type=int, help="number of the spec")
    start.add_argument(
        "--over-cap",
        action="store_true",
        help="admit Claude above the weekly cap: the operator's explicit decision only",
    )
    start.add_argument(
        "--over-limit",
        action="store_true",
        help="start above the in-flight limit (the sessions and the cap still hold):"
        " the operator's explicit decision only",
    )
    start.set_defaults(handler=cmd_start)

    provider = commands.add_parser("provider", help="provider of the NEXT workers")
    provider.add_argument("name", choices=sorted(PROVIDERS))
    provider.set_defaults(handler=cmd_provider)

    quota = commands.add_parser("quota", help="declare the weekly Claude usage (%%)")
    quota.add_argument("percent", type=int)
    quota.set_defaults(handler=cmd_quota)

    start_next = commands.add_parser(
        "start-next", help="launch the oldest ready spec whose head ticket is free"
    )
    start_next.set_defaults(handler=cmd_start_next)

    snapshot = commands.add_parser(
        "snapshot", help="everything the dashboard shows, as JSON (read only)"
    )
    snapshot.set_defaults(handler=cmd_snapshot)

    statusline = commands.add_parser(
        "statusline",
        help="Claude Code's status line for the workers: records the quota (stdin)",
    )
    statusline.set_defaults(handler=cmd_statusline)

    serve = commands.add_parser(
        "serve", help="the dashboard, read only, behind a password (until stopped)"
    )
    serve.set_defaults(handler=cmd_serve_guarded)

    update = commands.add_parser(
        "self-update", help="reinstall tracerworks from the remote base branch"
    )
    update.set_defaults(handler=cmd_self_update)

    usage = commands.add_parser(
        "usage", help="tokens a spec cost, owner and subagents apart"
    )
    usage.add_argument("spec", type=int, help="number of the spec")
    usage.set_defaults(handler=cmd_usage)

    db = commands.add_parser(
        "db", help="query the history base, read only (tables: db --schema)"
    )
    db.add_argument("sql", nargs="?", help="one SELECT statement")
    db.add_argument("--json", action="store_true", help="rows as JSON objects")
    db.add_argument("--schema", action="store_true", help="the tables and columns")
    db.set_defaults(handler=cmd_db)

    alert_test = commands.add_parser(
        "alert-test", help="send a test alert to Telegram, then its resolution"
    )
    alert_test.set_defaults(handler=cmd_alert_test)

    db_backup = commands.add_parser(
        "db-backup", help="copy the history base now (serve does it nightly)"
    )
    db_backup.set_defaults(handler=cmd_db_backup)

    clean = commands.add_parser(
        "clean",
        help="remove the worktrees of merged, fully pushed, closed specs;"
        " post their usage first",
    )
    clean.add_argument(
        "--dry-run", action="store_true", help="say what would be done, do nothing"
    )
    clean.set_defaults(handler=cmd_clean)

    stop = commands.add_parser(
        "stop", help="close the worker once everything is pushed; keeps the worktree"
    )
    stop.add_argument("spec", type=int, help="number of the spec")
    stop.add_argument(
        "--force",
        action="store_true",
        help="close it even with unpushed work — only after the operator confirmed",
    )
    stop.set_defaults(handler=cmd_stop)

    set_aside = commands.add_parser(
        "set-aside",
        help="close a worker's session, keep it: resumed once a session is free",
    )
    set_aside.add_argument("spec", type=int, help="number of the spec")
    set_aside.set_defaults(handler=cmd_set_aside)

    mcp = commands.add_parser(
        "mcp", help="serve a closed list of tools over MCP on stdio (remote session)"
    )
    mcp.set_defaults(handler=cmd_mcp)

    return parser


def dispatch(env: Env, args: argparse.Namespace) -> int:
    handler: Callable[[Env, argparse.Namespace], int] = args.handler
    try:
        return handler(env, args)
    except Refused as exc:
        return env.refuse(str(exc))
    except (CommandError, StateFileError, OSError) as exc:
        print(f"tracerworks: {exc}", file=env.err, flush=True)
        return EXIT_BROKEN


def run(env: Env, argv: Sequence[str]) -> int:
    """With the settings already loaded (the tests)."""
    return dispatch(env, build_parser().parse_args(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"tracerworks: {exc}", file=sys.stderr, flush=True)
        return EXIT_BROKEN
    return dispatch(Env(settings), args)
