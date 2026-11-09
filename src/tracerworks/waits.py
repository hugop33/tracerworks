"""Each wait has a deadline (#1730, spec #1727).

- A worker WAITING FOR A HUMAN — a Claude Code prompt, a question in its
  window, an expired login, a limit (its hooks said it, #1728) — for
  `[waits] human_minutes`: tracerworks puts the human label on its spec, with a
  comment that says why and the exact gesture; GitHub tells the operator.
  One label and one comment per episode (table `human_wait`), never two;
  tracerworks takes off the label it put once the worker works again.
- A worker that ENDED ITS TURN WAITING FOR MEMORY says so with a marker
  (its primer asks for it); the relauncher's pass tells it to go on once
  `[waits] memory_resume_mb` is available — no morning green light (#1493).
- A chantier PARKED WITH A RED OR CONFLICTING PULL REQUEST, not resumed for
  `[waits] parked_red_minutes`: an alert (`alerts.conditions`).

The time is the history's (`chantier_state`): when the chantier entered its
state, as `serve` saw it — the clock is the caller's `now`, injected.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tracerworks import chantier_state, texts
from tracerworks.chantier_state import Phase, Step
from tracerworks.process import CommandError

#: Waiting for someone, as its worker said it — not a label already there:
#: the cause is `worker: <kind>` (permission, question, login, limit).
_WAITING = (Phase.HUMAN, Phase.BLOCKED)
_SAID_BY_THE_WORKER = "worker: "

#: French, as every comment for the operator on GitHub (cleanup.RED_COMMENT).
HUMAN_COMMENT = (
    "**Le worker de #{spec} attend un humain depuis {minutes} min** : {reason}.\n\n"
    "**Le geste** : {gesture}\n\n"
    "_Étiquette `{label}` posée par tracerworks (#1730) ; il la retire quand le"
    " worker repart._\n"
)
GESTURES = {
    "permission": (
        "répondre à l'invite dans la fenêtre `{window}` du worker"
        " (`tmux attach -t {session}`) ; tracerworks n'y répond jamais."
    ),
    "question": "lui répondre par `tracerworks message {spec}`.",
    "login": (
        "`/login` dans la fenêtre `{window}` du worker (`tmux attach -t {session}`),"
        " puis lui dire de reprendre par `tracerworks message {spec}`."
    ),
    "limit": (
        "rien à répondre : attendre la remise à zéro de la limite, ou changer de"
        " compte Claude ; puis `tracerworks message {spec}` pour qu'il reprenne."
    ),
}
GESTURE_UNKNOWN = (
    "regarder la fenêtre `{window}` du worker (`tmux attach -t {session}`)."
)


@dataclass(frozen=True)
class Episode:
    spec: int
    entered_at: int  # when the chantier entered its waiting state
    labeled_at: int


def waiting(step: Step | None) -> bool:
    """Waiting for someone, as its worker said it (hooks or screen)."""
    return (
        step is not None
        and step.phase in _WAITING
        and step.cause.startswith(_SAID_BY_THE_WORKER)
    )


def gesture(cause: str, spec: int, window: str, session: str) -> str:
    """The gesture its kind asks — read from the cause, never the words."""
    kind = cause.removeprefix(_SAID_BY_THE_WORKER)
    template = GESTURES.get(kind, GESTURE_UNKNOWN)
    return template.format(spec=spec, window=window, session=session)


def comment(
    spec: int, step: Step, now: int, label: str, window: str, session: str
) -> str:
    return HUMAN_COMMENT.format(
        spec=spec,
        minutes=(now - step.at) // 60,
        reason=step.reason or "?",
        gesture=gesture(step.cause, spec, window, session),
        label=label,
    )


def open_episodes(db: sqlite3.Connection, project: str) -> list[Episode]:
    rows = db.execute(
        "SELECT spec, entered_at, labeled_at FROM human_wait"
        " WHERE project = ? AND cleared_at IS NULL ORDER BY spec",
        (project,),
    )
    return [Episode(int(s), int(e), int(at)) for s, e, at in rows]


def labeled(db: sqlite3.Connection, project: str, spec: int, entered_at: int) -> bool:
    row = db.execute(
        "SELECT 1 FROM human_wait WHERE project = ? AND spec = ? AND entered_at = ?",
        (project, spec, entered_at),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class Due:
    """A wait past its deadline, not told yet."""

    spec: int
    step: Step  # its waiting state
    entered_at: int  # the episode


@dataclass(frozen=True)
class Plan:
    """What to do this pass: label (with its comment) and unlabel."""

    label: list[Due]
    unlabel: list[Episode]


def plan(
    db: sqlite3.Connection,
    project: str,
    specs: Iterable[int],
    now: int,
    after_minutes: int,
) -> Plan:
    """The chantiers whose wait for a human passed its deadline and are not
    labeled for this episode yet; the episodes whose worker no longer waits.

    No longer waiting is anything but the worker's own word that it waits:
    tracerworks's label, read back by the snapshot, makes the chantier « waits
    for a human (asked on #n) » — its cause the label, not the worker — so
    the label never holds itself once the worker answered."""
    specs = sorted(set(specs))
    entered = chantier_state.since(db, project, specs)
    label = []
    for spec in specs:
        step = chantier_state.current(db, project, spec)
        if step is None or not waiting(step) or spec not in entered:
            continue
        started = entered[spec]
        if now - started < after_minutes * 60 or labeled(db, project, spec, started):
            continue
        label.append(Due(spec, step, started))
    unlabel = [
        episode
        for episode in open_episodes(db, project)
        if not waiting(chantier_state.current(db, project, episode.spec))
    ]
    return Plan(label, unlabel)


def mark_labeled(
    db: sqlite3.Connection, project: str, spec: int, entered_at: int, now: int
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO human_wait (project, spec, entered_at, labeled_at)"
        " VALUES (?, ?, ?, ?)",
        (project, spec, entered_at, now),
    )


def mark_cleared(
    db: sqlite3.Connection, project: str, episode: Episode, now: int
) -> None:
    db.execute(
        "UPDATE human_wait SET cleared_at = ? WHERE project = ? AND spec = ?"
        " AND entered_at = ?",
        (now, project, episode.spec, episode.entered_at),
    )


Gh = Callable[[list[str], str | None], None]  # argv after `gh`, stdin; raises


def tend(
    db: sqlite3.Connection,
    gh: Gh,
    *,
    project: str,
    label: str,
    specs: Iterable[int],
    now: int,
    after_minutes: int,
    window: Callable[[int], str],
    session: str,
) -> list[str]:
    """Unlabel, then label and comment — each recorded only once done, so
    that a failure is tried again at the next pass, never doubled (but for a
    crash between a comment and its record); one failing chantier does not
    hold the others. What was done, and what failed, for the journal."""
    said = []
    todo = plan(db, project, specs, now, after_minutes)
    for episode in todo.unlabel:
        n = str(episode.spec)
        try:
            gh(["issue", "edit", n, "-R", project, "--remove-label", label], None)
        except CommandError as exc:
            said.append(texts.WAIT_NOT_TOLD.format(spec=n, error=exc))
            continue
        mark_cleared(db, project, episode, now)
        said.append(texts.WAIT_UNLABELED.format(spec=n, label=label))
    for due in todo.label:
        n = str(due.spec)
        body = comment(due.spec, due.step, now, label, window(due.spec), session)
        try:
            gh(["issue", "edit", n, "-R", project, "--add-label", label], None)
            gh(["issue", "comment", n, "-R", project, "--body-file", "-"], body)
        except CommandError as exc:
            said.append(texts.WAIT_NOT_TOLD.format(spec=n, error=exc))
            continue
        mark_labeled(db, project, due.spec, due.entered_at, now)
        said.append(texts.WAIT_LABELED.format(spec=n, label=label, why=due.step.reason))
    return said


# --- memory --------------------------------------------------------------------

#: French, as every instruction to a worker (order.SET_ASIDE_RESUME).
MEMORY_RESUME = (
    "La mémoire est revenue : {mb} Mo disponibles (relevé d'tracerworks). Reprends là"
    " où tu t'étais arrêté, sans attendre de feu vert."
)


# --- parked red ----------------------------------------------------------------

_RED_CAUSES = (chantier_state.CAUSE_PARKED_RED, "parked: conflict")


def stale_parked(
    db: sqlite3.Connection,
    project: str,
    specs: Iterable[int],
    now: int,
    after_minutes: int,
) -> dict[int, tuple[float, str]]:
    """The chantiers parked with a red or conflicting pull request for
    longer than allowed, without a resume: since when, and why."""
    entered = chantier_state.since(db, project, specs)
    stale: dict[int, tuple[float, str]] = {}
    for spec, at in entered.items():
        step = chantier_state.current(db, project, spec)
        if step is None or step.phase is not Phase.PARKED:
            continue
        if step.cause not in _RED_CAUSES or now - at < after_minutes * 60:
            continue
        stale[spec] = (float(at), step.reason or "?")
    return stale


def specs_recorded(db: sqlite3.Connection, project: str) -> list[int]:
    return [
        int(spec)
        for (spec,) in db.execute(
            "SELECT DISTINCT spec FROM chantier_state WHERE project = ?", (project,)
        )
    ]
