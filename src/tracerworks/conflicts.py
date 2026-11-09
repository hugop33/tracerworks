"""A pull request in conflict with the base no longer stays silent (#1791).

GitHub starts no CI on a pull request in conflict, and says nothing: #1578
(20/10) stayed « in CI » 4 h 30 with no run, #1779 (24/10) after a merge
next to it. So, in the same pass as the CI's revival (#1790), over the same
open pull requests of trusted authors: one `CONFLICTING` for
`[waits] conflict_minutes` is an EPISODE (table `pull_conflict`), told once —

- its worker in its window: woken by `message`'s path, told to merge the
  base, resolve, push;
- its worker parked (its session closed while its CI ran): nothing here —
  the relauncher resumes a parked chantier whose pull request conflicts;
- no worker of tracerworks (a session of the Mac): one comment on the pull
  request, and an alert while it lasts (`alerts.conditions`: Telegram, the
  Quality tab), which `status` says too.

The episode ends when GitHub says `MERGEABLE` again, or the pull request is
no longer open: a new conflict is a new episode, told again. `UNKNOWN`
(GitHub still computing) changes nothing. A pull request of the merge queue
leaves it on a conflict already (#1715): the rule holds for it too.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tracerworks import texts
from tracerworks.process import CommandError
from tracerworks.settings import Settings

if TYPE_CHECKING:  # the alerts read `silent`: merge_queue → recorder → here
    from tracerworks.ci_revival import Open
    from tracerworks.merge_queue import GitHub

#: French, as every instruction to a worker (waits.MEMORY_RESUME).
WAKE = (
    "Ta PR #{pull} est en conflit avec {base} depuis {minutes} min (relevé"
    " d'tracerworks, #1791) : GitHub n'y lance aucune CI. Fusionne {base} dans ta"
    " branche (git fetch puis git merge {base}), résous, rejoue les preuves"
    " rapides et pousse, sans attendre de feu vert."
)
#: French, as every comment for the operator on GitHub (merge_queue's).
COMMENT = (
    "**PR en conflit avec `{base}` depuis {minutes} min** (#1791) : GitHub n'y"
    " lance aucune CI, et aucun worker d'tracerworks ne la porte.\n\n"
    "**Le geste** : fusionner `{base}` dans la branche, régler le conflit, pousser."
    " L'alerte se ferme quand GitHub la redit fusionnable.\n"
)

#: How an episode was told: its worker woken, left to the parking's resume,
#: or a comment and an alert.
WOKEN, PARKED, COMMENTED = "message", "parked", "comment"

Tell = Callable[[int, str], None]  # spec, text; raises `CommandError` if unheard


@dataclass(frozen=True)
class Episode:
    pull: int
    seen_at: int
    told_at: int | None
    how: str | None


def open_episodes(db: sqlite3.Connection, project: str) -> dict[int, Episode]:
    rows = db.execute(
        "SELECT pull, seen_at, told_at, how FROM pull_conflict"
        " WHERE project = ? AND cleared_at IS NULL",
        (project,),
    )
    return {
        int(pull): Episode(
            int(pull), int(seen), None if told is None else int(told), how
        )
        for pull, seen, told, how in rows
    }


def concerned(pull: Open, settings: Settings) -> bool:
    return not pull.draft and pull.author in settings.trusted_logins


def tend(
    db: sqlite3.Connection,
    gh: GitHub,
    settings: Settings,
    pulls: Sequence[Open],
    now: int,
    *,
    windows: Collection[int],
    parked: Collection[int],
    tell: Tell,
) -> list[str]:
    """One pass: open, tell and close the episodes; what it did, one line
    each. `windows`: the specs whose worker is in its window; `parked`: the
    specs parked. A failure is said, and tried again at the next pass."""
    project, minutes = settings.github_repo, settings.waits.conflict_minutes
    base = settings.base_ref
    episodes = open_episodes(db, project)
    said: list[str] = []
    read = set()
    for pull in pulls:
        if not concerned(pull, settings):
            continue
        read.add(pull.number)
        episode = episodes.get(pull.number)
        if pull.mergeable == "MERGEABLE" and episode is not None:
            _clear(db, project, pull.number, now)
            said.append(texts.CONFLICT_CLEARED.format(pull=pull.number))
            continue
        if pull.mergeable != "CONFLICTING":
            continue
        if episode is None:
            db.execute(
                "INSERT INTO pull_conflict (project, pull, seen_at) VALUES (?, ?, ?)",
                (project, pull.number, now),
            )
            continue
        if episode.told_at is not None or now - episode.seen_at < minutes * 60:
            continue
        parsed = settings.conventions.parse_branch(pull.branch)
        spec = parsed[0] if parsed else None
        lasted = (now - episode.seen_at) // 60
        try:
            if spec is not None and spec in windows:
                how = WOKEN
                tell(spec, WAKE.format(pull=pull.number, base=base, minutes=lasted))
            elif spec is not None and spec in parked:
                how = PARKED
            else:
                how = COMMENTED
                gh.comment(pull.number, COMMENT.format(base=base, minutes=lasted))
        except CommandError as exc:
            said.append(texts.CONFLICT_NOT_TOLD.format(pull=pull.number, error=exc))
            continue
        db.execute(
            "UPDATE pull_conflict SET told_at = ?, how = ?"
            " WHERE project = ? AND pull = ? AND cleared_at IS NULL",
            (now, how, project, pull.number),
        )
        said.append(
            texts.CONFLICT_TOLD[how].format(pull=pull.number, spec=spec, minutes=lasted)
        )
    for number in episodes.keys() - read:  # no longer open
        _clear(db, project, number, now)
    return said


def _clear(db: sqlite3.Connection, project: str, pull: int, now: int) -> None:
    db.execute(
        "UPDATE pull_conflict SET cleared_at = ?"
        " WHERE project = ? AND pull = ? AND cleared_at IS NULL",
        (now, project, pull),
    )


def silent(db: sqlite3.Connection, project: str) -> list[tuple[int, int]]:
    """The conflicts told by a comment, still open: (pull, since) — what the
    alerts and `status` say."""
    return [
        (int(pull), int(seen))
        for pull, seen in db.execute(
            "SELECT pull, seen_at FROM pull_conflict WHERE project = ?"
            " AND cleared_at IS NULL AND how = ? ORDER BY pull",
            (project, COMMENTED),
        )
    ]
