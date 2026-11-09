"""The factory's order (#1823): one rank for every chantier, at most
`in_flight` of them in the factory, and `max_workers` sessions for the best
ranked that have work. It replaced the lanes (#1261).

- IN FLIGHT: a chantier with a role — IN DEV (its window is open), IN CI
  (parked: a `parked` row, no window) or SET ASIDE (a `set_aside` row, no
  window: its session closed so that a better ranked red is fixed first).
  At most `in_flight` (6): the queue starts nothing above it.
- THE ORDER: the chantiers in flight by their entry into the factory (the
  `entry` table, written at launch; the oldest first), then the queue, as
  `start-next` takes it (`priority` first, then the oldest). A spec's
  priority counts when it enters: the queue takes it first.
- WAITING FOR A PLACE: a spec whose `start` the order refused (a
  `place_wait` row, #2118). It takes the next free place after a red to fix
  and a chantier set aside, before any new spec — the oldest request first.
- SESSIONS: at most `max_workers` windows. A red to fix and a chantier set
  aside take a free session before any new chantier; with none free, a red
  PREEMPTS the most recent chantier in dev ranked after it, at a safe point
  (its worker at rest, or the red waiting for `PREEMPT_WAIT` already), never
  one resumed less than `HOLD` ago — no back and forth.

The roles are rebuilt at each call, from the windows and the base, never
stored: after a crash or a reboot the next pass sees the new reality.
"""

from __future__ import annotations

import dataclasses
import enum
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tracerworks import store, texts
from tracerworks.observe import Worker
from tracerworks.settings import Settings

#: A red preempts a worker still working once it has waited this long (s).
PREEMPT_WAIT = 10 * 60
#: A chantier resumed keeps its session at least this long (s).
HOLD = 20 * 60


class Role(enum.Enum):
    DEVELOPMENT = texts.ROLE_DEVELOPMENT
    CI = texts.ROLE_CI
    SET_ASIDE = texts.ROLE_SET_ASIDE

    @property
    def key(self) -> str:
        """Its name in `status --json`."""
        return self.name.lower()


@dataclass(frozen=True)
class SetAside:
    spec: int
    session: str
    set_aside_at: int
    for_spec: int | None  # the red it made way for; None: the operator's

    @property
    def short_session(self) -> str:
        return self.session[:8]


#: Why a chantier set aside is resumed — French, as every instruction to a
#: worker; a file it is told to read, like the reason of a parked one.
SET_ASIDE_RESUME = (
    "Reprise de ta session : tracerworks l'avait mise de côté le {when} {why}."
    " Une session est libre de nouveau : tu reprends. Rien n'a été perdu"
    " (worktree, branche, fichiers non commités, conversation), mais le dépôt et"
    " GitHub ont pu bouger entre-temps : relis d'abord l'état réel (git status,"
    " git log, ta PR et ses checks s'il y en a une, le ticket de tête et ses"
    " commentaires) avant de continuer là où tu en étais.\n"
)
WHY_FOR_RED = "pour laisser le chantier #{red}, mieux classé, corriger sa CI rouge"
WHY_BY_HAND = "à la demande de {operator}"
WHEN = "%d/%m à %Hh%M"


def resume_text(record: SetAside, operator: str) -> str:
    why = (
        WHY_BY_HAND.format(operator=operator)
        if record.for_spec is None
        else WHY_FOR_RED.format(red=record.for_spec)
    )
    when = datetime.fromtimestamp(record.set_aside_at).strftime(WHEN)
    return SET_ASIDE_RESUME.format(when=when, why=why)


def describe_when(at: int) -> str:
    return datetime.fromtimestamp(at).strftime("%d/%m %H:%M")


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th… 11th, 12th, 13th, 21st."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


@dataclass(frozen=True)
class Factory:
    roles: Mapping[int, Role]
    entered: Mapping[int, int]  # spec → its entry; every chantier in flight has one
    set_aside: Mapping[int, SetAside]
    limit: int  # chantiers in flight
    sessions_max: int  # windows
    place_waits: Mapping[int, int] = dataclasses.field(default_factory=dict)

    @property
    def waiting(self) -> list[int]:
        """The specs waiting for a place (#2118), the oldest request first."""
        return sorted(self.place_waits, key=lambda s: (self.place_waits[s], s))

    def place_wait(self, spec: int) -> dict[str, int] | None:
        """Its rank among the specs waiting for a place, and since when;
        None: it does not wait."""
        if spec not in self.place_waits:
            return None
        return {"rank": self.waiting.index(spec) + 1, "since": self.place_waits[spec]}

    def describe_waits(self) -> list[str]:
        """One line per spec waiting for a place, the next to start first."""
        return [
            texts.PLACE_WAIT_LINE.format(
                rank=ordinal(i + 1), n=s, since=describe_when(self.place_waits[s])
            )
            for i, s in enumerate(self.waiting)
        ]

    @property
    def ranked(self) -> list[int]:
        """The chantiers in flight, best first: by entry, then spec."""
        return sorted(self.roles, key=lambda s: (self.entered.get(s, 0), s))

    def rank(self, spec: int) -> int | None:
        order = self.ranked
        return order.index(spec) + 1 if spec in order else None

    @property
    def in_flight(self) -> int:
        return len(self.roles)

    @property
    def in_dev(self) -> list[int]:
        return [s for s in self.ranked if self.roles[s] is Role.DEVELOPMENT]

    @property
    def sessions_free(self) -> int:
        return max(0, self.sessions_max - len(self.in_dev))

    def why_full(self, spec: int | None = None, over_limit: bool = False) -> str | None:
        """Why no new chantier — nor `spec` — may start; None: one may. A
        chantier already in flight adds nothing to the count: only a
        session may hold it back; nor does one the operator starts
        `--over-limit` (#2118), `start` only."""
        full = self.in_flight >= self.limit and not over_limit
        if full and spec not in self.roles:
            return texts.ORDER_FULL.format(n=self.in_flight, limit=self.limit)
        return self.why_no_session()

    def why_no_session(self) -> str | None:
        if self.sessions_free:
            return None
        return texts.SESSIONS_FULL.format(
            n=len(self.in_dev), max=self.sessions_max, held=self.describe_dev()
        )

    def victim(self, red: int, safe: Callable[[int], bool]) -> int | None:
        """The chantier a red preempts: the most recent in dev ranked after
        it whose session may close now (`safe`); None: none — the red waits."""
        rank = self.rank(red)
        later = [s for s in self.in_dev if rank is None or (self.rank(s) or 0) > rank]
        return next((s for s in reversed(later) if safe(s)), None)

    def describe_dev(self) -> str:
        return " · ".join(f"#{s} ({ordinal(self.rank(s) or 0)})" for s in self.in_dev)

    def describe(self, states: Mapping[int, str] | None = None) -> list[str]:
        """One line per chantier in flight, in its order (#1729's words)."""
        said = states or {}
        return [
            texts.ORDER_LINE.format(
                rank=ordinal(i + 1), n=s, state=said.get(s, self.roles[s].value)
            )
            for i, s in enumerate(self.ranked)
        ]


def roles(
    workers: Iterable[Worker], parked: Collection[int], aside: Collection[int]
) -> dict[int, Role]:
    """In dev: a window; set aside or in CI: its row, no window — set aside
    first, the newer (parking forgets a set-aside row). Anything else is not
    in flight."""
    found = {}
    for worker in workers:
        if worker.window is not None:
            found[worker.spec] = Role.DEVELOPMENT
        elif worker.spec in aside:
            found[worker.spec] = Role.SET_ASIDE
        elif worker.spec in parked:
            found[worker.spec] = Role.CI
    return found


def arrange(
    workers: Iterable[Worker],
    parked: Collection[int],
    aside: Mapping[int, SetAside],
    entered: Mapping[int, int],
    limit: int,
    sessions_max: int,
    now: float,
    place_waits: Mapping[int, int] | None = None,
) -> tuple[Factory, list[int]]:
    """The factory as it is — and the chantiers in flight without an entry
    (launched before the order), entered now, for the caller to write. A
    spec waiting for a place yet in flight again is no longer waiting."""
    held = roles(workers, parked, aside)
    known = dict(entered)
    added = sorted(s for s in held if s not in known)
    for spec in added:
        known[spec] = int(now)
    waits = {s: at for s, at in (place_waits or {}).items() if s not in held}
    return Factory(held, known, aside, limit, sessions_max, waits), added


def view_json(
    factory: Factory | None, states: Mapping[int, str] | None = None
) -> dict[str, Any]:
    """The order as `status --json` and the dashboard give it: the
    chantiers in flight best first, how many and the limit, the sessions.
    None: the base unreadable — unknown, never room."""
    if factory is None:
        return {
            "order": None,
            "in_flight": None,
            "in_flight_limit": None,
            "place_waits": None,
        }
    return {
        "order": [
            {"spec": s, "rank": i + 1, "role": factory.roles[s].key}
            for i, s in enumerate(factory.ranked)
        ],
        # the specs waiting for a place (#2118), the next to start first
        "place_waits": [
            {"spec": s, "rank": i + 1, "since": factory.place_waits[s]}
            for i, s in enumerate(factory.waiting)
        ],
        "in_flight": factory.in_flight,
        "in_flight_limit": factory.limit,
        "order_lines": factory.describe(states),
    }


def chantier_json(factory: Factory | None, spec: int) -> dict[str, Any]:
    """One chantier's `rank`, `role`, `set_aside` and `place_wait` (a
    stopped one whose `start` was refused, #2118), in its JSON."""
    if factory is None:
        return {"rank": None, "role": None, "set_aside": None, "place_wait": None}
    role, aside = factory.roles.get(spec), factory.set_aside.get(spec)
    return {
        "rank": factory.rank(spec),
        "role": role.key if role else None,
        "set_aside": None
        if aside is None
        else {
            "session": aside.session,
            "for": aside.for_spec,
            "set_aside_at": aside.set_aside_at,
        },
        "place_wait": factory.place_wait(spec),
    }


# --- the base -------------------------------------------------------------------


def entries(settings: Settings) -> dict[int, int]:
    """spec → its entry; a base that cannot be read is raised (`StoreError`,
    `sqlite3.Error`, `OSError`): the caller says it."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT spec, entered_at FROM entry WHERE project = ?",
            (settings.github_repo,),
        ).fetchall()
    return {int(spec): int(at) for spec, at in rows}


def enter(settings: Settings, spec: int, now: float) -> None:
    """Its entry, once: a chantier relaunched keeps its place."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT OR IGNORE INTO entry (project, spec, entered_at) VALUES (?, ?, ?)",
            (settings.github_repo, spec, int(now)),
        )


def leave(settings: Settings, spec: int) -> None:
    """Out of the factory (stopped, removed): a new launch enters it again, last."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "DELETE FROM entry WHERE project = ? AND spec = ?",
            (settings.github_repo, spec),
        )


def set_aside(
    settings: Settings, spec: int, session: str, for_spec: int | None, now: float
) -> None:
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT INTO set_aside (project, spec, session_id, set_aside_at, for_spec)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (spec, project) DO UPDATE SET"
            " session_id = excluded.session_id,"
            " set_aside_at = excluded.set_aside_at, for_spec = excluded.for_spec",
            (settings.github_repo, spec, session, int(now), for_spec),
        )


def set_aside_records(settings: Settings) -> dict[int, SetAside]:
    """Raised like `entries` when the base cannot be read."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT spec, session_id, set_aside_at, for_spec FROM set_aside"
            " WHERE project = ?",
            (settings.github_repo,),
        ).fetchall()
    return {int(row[0]): SetAside(*row) for row in rows}


def forget_set_aside(settings: Settings, spec: int) -> None:
    """Resumed, relaunched, stopped or removed: nothing left set aside."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "DELETE FROM set_aside WHERE project = ? AND spec = ?",
            (settings.github_repo, spec),
        )


def wait_for_place(settings: Settings, spec: int, now: float) -> None:
    """Waits for a place (#2118), once: a second request keeps the first's
    time — its rank."""
    with closing(store.connect(settings.history_db)) as db:
        db.execute(
            "INSERT OR IGNORE INTO place_wait (project, spec, asked_at)"
            " VALUES (?, ?, ?)",
            (settings.github_repo, spec, int(now)),
        )


def place_waits(settings: Settings) -> dict[int, int]:
    """spec → when its `start` was refused; raised like `entries`."""
    with closing(store.connect(settings.history_db)) as db:
        rows = db.execute(
            "SELECT spec, asked_at FROM place_wait WHERE project = ?",
            (settings.github_repo,),
        ).fetchall()
    return {int(spec): int(at) for spec, at in rows}


def forget_place_wait(settings: Settings, spec: int) -> bool:
    """Relaunched, closed, stopped or cleaned: no longer waiting. True: it was."""
    with closing(store.connect(settings.history_db)) as db:
        gone = db.execute(
            "DELETE FROM place_wait WHERE project = ? AND spec = ?",
            (settings.github_repo, spec),
        ).rowcount
    return bool(gone)


@dataclass(frozen=True)
class Recorded:
    entered: dict[int, int]
    aside: dict[int, SetAside]
    place_waits: dict[int, int]


def recorded(settings: Settings) -> Recorded:
    """The entries, the chantiers set aside and the specs waiting for a
    place, as the base has them; raised like `entries`."""
    return Recorded(
        entries(settings), set_aside_records(settings), place_waits(settings)
    )


def stale(aside: Mapping[int, SetAside], workers: Iterable[Worker]) -> list[int]:
    """Set aside, yet in a window again (reopened by hand): never two
    sessions on one chantier, never a session older than the one open."""
    return sorted(w.spec for w in workers if w.spec in aside and w.window is not None)


#: What reading or writing the base may raise: said by the caller.
BASE_ERRORS = (OSError, sqlite3.Error, store.StoreError)
