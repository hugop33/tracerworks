"""A worker that ended its turn asking a permission it need not ask is told to
go on (#1789, spec #1788).

Its primer forbids asking whether to push again after a red of the machine:
it does it once itself, and asks the operator only what `CLAUDE.md` keeps for
him, by `needs-human`. A worker asks all the same at times (worker-1529,
19/10; worker-1687 and worker-1780, 24/10, 40 min each), and the night stops.

So the relauncher's pass reads each worker AT REST IN DEVELOPMENT — its
history's cause `worker: at rest`, which no needs-human label, CI, merge
queue, parking or memory wait gives — whose turn ended `[waits]
permission_ask_minutes` ago or more on a demand of permission (the
`permission_ask` patterns of the project file) and on no decision
(`decision` patterns: money, data, V1, production, secrets — those wait for
the operator). It sends it the standard nudge, by `message`'s path.

At most one nudge per worker an hour. The third without progress — no
commit, no push since the first — is not sent: the human label goes on the
spec instead, with a comment (table `nudge`, what `status` and the workshop
say). The words are read, never obeyed: a pattern only decides whether the
nudge leaves; the clock is the caller's.
"""

from __future__ import annotations

import enum
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tracerworks import texts

#: One nudge per worker in this window.
SPACING_SECONDS = 60 * 60
#: The nudge that would be the third without progress puts the human label.
GIVE_UP_AT = 3
#: A demand sits at the end of a turn: only this many characters are read.
TAIL_CHARS = 800
#: Its history's cause: in development, at rest — nothing else holds it.
AT_REST = "worker: at rest"
UNSAVED_CAUSES = frozenset(
    {
        AT_REST,
        "worker at rest, its CI runs",
        "merge queue: waiting",
        "merge queue: testing",
    }
)

#: French, as every instruction to a worker (waits.MEMORY_RESUME).
NUDGE = (
    "Relance d'tracerworks (#1789) : tu as fini ton tour sur une demande de"
    " permission que tu n'as pas à poser. Continue sans attendre de réponse :"
    " un hook ou une CI rouge pour cause de machine (mémoire, runner tombé,"
    " processus tué) se relance une fois toi-même ; un rouge de ton code se"
    " corrige. Une vraie décision (argent, données réelles, V1, production,"
    " secrets, sécurité) se pose par {label} sur le ticket, puis tu"
    " t'arrêtes."
)
UNSAVED_NUDGE = (
    "Relance d'tracerworks (#2148) : ton tour est fini et ta PR est ouverte,"
    " mais du travail non commité ou non poussé retient encore ta session."
    " Relis ton état Git, le diff et ta PR, puis vérifie et termine ce travail"
    " avec ses preuves ciblées, son commit et son push. Préserve les fichiers :"
    " aucun reset, suppression ou restauration aveugle. En cas d'ambiguïté ou"
    " de décision réelle de l'opérateur, explique-la par {label} sur le ticket."
    " Puis suis les consignes de la voie légère et termine ton tour pour que"
    " ta session puisse être mise en veille. Ne fusionne pas et ne déploie rien."
)
#: French, as every comment for the operator on GitHub (waits.HUMAN_COMMENT).
GIVE_UP_COMMENT = (
    "**Le worker de #{spec} redemande la permission malgré {count} relances"
    " d'tracerworks**, sans commit ni push depuis la première ({first}).\n\n"
    "Son dernier message finit par :\n\n> {tail}\n\n"
    "**Le geste** : lire la fenêtre `{window}` (`tmux attach -t {session}`),"
    " puis lui répondre par `tracerworks message {spec}`.\n\n"
    "_Étiquette `{label}` posée par tracerworks (#1789)._\n"
)


def _matches(patterns: Sequence[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def asks_permission(
    text: str | None, asks: Sequence[str], decisions: Sequence[str]
) -> bool:
    """The end of a turn asks leave to go on — and no decision of the
    operator's: a decision word anywhere in it holds the nudge back."""
    if not text:
        return False
    tail = text[-TAIL_CHARS:]
    return _matches(asks, tail) and not _matches(decisions, tail)


@dataclass(frozen=True)
class Sent:
    at: int
    progress: str  # the worktree's commit and upstream when it was sent
    outcome: str  # sent, needs_human


def history(db: sqlite3.Connection, project: str, spec: int) -> list[Sent]:
    """Its nudges since the last label tracerworks put for them, oldest first:
    the operator answered then, the count starts again."""
    rows = list(
        db.execute(
            "SELECT at, progress, outcome FROM nudge WHERE project = ? AND spec = ?"
            " ORDER BY at, id",
            (project, spec),
        )
    )
    sent = [
        Sent(int(at), str(progress), str(outcome)) for at, progress, outcome in rows
    ]
    last_label = max(
        (i for i, s in enumerate(sent) if s.outcome == "needs_human"), default=-1
    )
    return sent[last_label + 1 :]


def last(db: sqlite3.Connection, project: str, spec: int) -> Sent | None:
    row = db.execute(
        "SELECT at, progress, outcome FROM nudge WHERE project = ? AND spec = ?"
        " ORDER BY at DESC, id DESC LIMIT 1",
        (project, spec),
    ).fetchone()
    return None if row is None else Sent(int(row[0]), str(row[1]), str(row[2]))


class Gesture(enum.Enum):
    NONE = "none"
    NUDGE = "nudge"
    LABEL = "label"


@dataclass(frozen=True)
class Seen:
    """What the pass knows of one worker."""

    spec: int
    cause: str | None  # its history's last accepted cause, None: none
    rested_at: float | None  # when its turn ended (its Stop hook)
    last_words: str | None  # the end of its last message
    progress: str  # its commit and its upstream's, as read now
    unsaved: bool = False  # at rest, open PR, known Git, no human hold or pending field


def decide(
    seen: Seen,
    before: Sequence[Sent],
    now: int,
    *,
    after_minutes: int,
    asks: Sequence[str],
    decisions: Sequence[str],
) -> Gesture:
    """Nothing, a nudge, or — the third without progress — the label."""
    unfinished = seen.unsaved and seen.cause in UNSAVED_CAUSES
    if (seen.cause != AT_REST and not unfinished) or seen.rested_at is None:
        return Gesture.NONE
    if now - seen.rested_at < after_minutes * 60:
        return Gesture.NONE
    if unfinished:
        if _matches(decisions, seen.last_words or ""):
            return Gesture.NONE
    elif not asks_permission(seen.last_words, asks, decisions):
        return Gesture.NONE
    if before and now - before[-1].at < SPACING_SECONDS:
        return Gesture.NONE
    without_progress = [s for s in before if s.progress == seen.progress]
    if len(without_progress) + 1 >= GIVE_UP_AT:
        return Gesture.LABEL
    return Gesture.NUDGE


def record(
    db: sqlite3.Connection,
    project: str,
    spec: int,
    now: int,
    progress: str,
    outcome: str,
) -> None:
    db.execute(
        "INSERT INTO nudge (project, spec, at, progress, outcome)"
        " VALUES (?, ?, ?, ?, ?)",
        (project, spec, now, progress, outcome),
    )


def give_up_comment(
    spec: int,
    before: Sequence[Sent],
    last_words: str,
    *,
    label: str,
    window: str,
    session: str,
    unsaved: bool = False,
) -> str:
    first = datetime.fromtimestamp(before[0].at).strftime("%d/%m %H:%M")
    tail = " ".join(last_words[-300:].split())
    template = GIVE_UP_COMMENT
    if unsaved:
        template = (
            "**Le worker de #{spec} garde du travail non poussé malgré {count} relances"
            " d'tracerworks**, sans commit ni push depuis la première ({first}).\n\n"
            "Son dernier message finit par :\n\n> {tail}\n\n"
            "**Le geste** : lire la fenêtre `{window}` (`tmux attach -t {session}`),"
            " puis lui répondre par `tracerworks message {spec}`.\n\n"
            "_Étiquette `{label}` posée par tracerworks (#2148)._\n"
        )
    return template.format(
        spec=spec,
        count=len(before),
        first=first,
        tail=tail,
        window=window,
        session=session,
        label=label,
    )


def describe(sent: Sent | None, count: int) -> str | None:
    """The last nudge, as `status` and the workshop say it."""
    if sent is None:
        return None
    when = datetime.fromtimestamp(sent.at).strftime("%d/%m %H:%M")
    if sent.outcome == "needs_human":
        return texts.NUDGE_GAVE_UP.format(when=when)
    return texts.NUDGE_SENT.format(when=when, count=count, most=GIVE_UP_AT - 1)
