"""The alerts (#841): Telegram tells the operator what needs him, once.

Evaluated each minute by `serve`'s history cycle, from the history base:

- a worker waiting for the operator (`needs-human`) for more than 30 min —
  a worker's only (#853), blocked as the dashboard says it (#830): at rest,
  the label on its spec or a ticket; counted from the later of the label and
  the worker's halt. An issue keeping the label with no worker on its
  chantier shows on the dashboard, and alerts no one;
- a worker stopped on a permission prompt for more than 5 min (#984) —
  tracerworks never answers it: the operator decides;
- a Claude quota window above 80 %;
- the GitHub Actions minutes past 90 % of the month (#1296), when the
  project names the variable its net sets;
- the disk above 90 % (85 % until 16/10: it alerted all day long);
- a stray (`tracerworks.strays`): a process holding half a core or more for
  10 min outside the CI, the services and the workers — one episode by pid;
- the last CI run of the base branch red;
- the workers' fine-grained GitHub token missing (#1529): absent, empty or
  readable by others — the workers hold the owner's token meanwhile;
- a pull request in conflict with the base that no worker carries (#1791);
- a worktree that vanished outside `clean` (#2077), for a day;
- a pull request the merge queue sent back, green, because its merge kept
  failing (#2156), for a day.

ONE MESSAGE PER EPISODE: a condition that becomes true opens an episode and
sends it; while it stays true nothing more is sent; when it becomes false
the episode closes and « rétabli » is sent. A condition that cannot be read
changes nothing — never a « rétabli » because a reading failed. A message
that could not leave is sent again the next minute, still once; an episode
that ended before any message could leave says nothing.

The episodes are kept in the base (table `alert`) whether Telegram is set up
or not: the Qualité tab lists them. Without a token (absent, empty, or
readable by others), nothing is sent and the service's journal says so once.
The token is never logged, never in a URL shown, never in the snapshot.

SWITCHED OFF (#2193): Telegram as a whole, or a family of `FAMILIES`, from
the dashboard's Settings tab (the state file). An episode of a family off
still opens and closes in the base — the Qualité tab lists it — but it is
silenced as it opens and never sent, even if the family is switched on again
while it is open; nor is its « rétabli ». An episode already told whose
family goes off: its « rétabli » is not sent either. Telegram off silences
every family; on again, only the episodes opened after are told. The
workers' token (`worker token`) has no switch: it guards a secret.
"""

from __future__ import annotations

import json
import sqlite3
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tracerworks import store, texts, worker_token
from tracerworks.budget import ABOVE, low
from tracerworks.strays import Stray

NEEDS_HUMAN_AFTER = 30 * 60
ASKING_AFTER = 5 * 60
QUOTA_ABOVE = 80.0
DISK_ABOVE = 90.0
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

Send = Callable[[str], None]  # raises `AlertError` when the message did not leave


@dataclass(frozen=True)
class Family:
    """A family of alerts the operator may switch off (#2193): its key in
    the state file (`alerts.<key>`), the first word of its episodes' keys,
    its label in the Settings tab."""

    key: str
    prefix: str
    label: str


FAMILIES: tuple[Family, ...] = (
    Family("waiting", "waiting", "A worker waiting for you"),
    Family("asking", "asking", "A worker stopped on a permission prompt"),
    Family("claude", "claude", "Claude quota"),
    Family("actions", "actions", "GitHub Actions minutes"),
    Family("disk", "disk", "Disk"),
    Family("stray", "stray", "A stray process"),
    Family("ci", "ci", "The base branch's CI red"),
    Family("conflict", "conflict", "A pull request in conflict"),
    Family("vanished", "vanished", "A worktree vanished"),
    Family("merge_failed", "merge-failed", "A pull request sent back by the queue"),
    Family("parked_red", "parked-red", "A red nobody resumed"),
)


def family_of(key: str) -> Family | None:
    """The family of an episode's key; None for one without a switch."""
    head = key.split(" ", 1)[0]
    return next((f for f in FAMILIES if f.prefix == head), None)


def muted(telegram: bool, off: frozenset[str]) -> frozenset[str]:
    """The families whose episodes are not sent: all of them with Telegram
    off, else those switched off (keys of `FAMILIES`)."""
    return frozenset(f.key for f in FAMILIES) if not telegram else off


def _muted(key: str, silent: frozenset[str]) -> bool:
    family = family_of(key)
    return family is not None and family.key in silent


class AlertError(RuntimeError):
    """Telegram is not set up, or refused the message."""


@dataclass(frozen=True)
class Condition:
    key: str
    project: str | None
    text: str


def conditions(
    db: sqlite3.Connection,
    project: str,
    base_branch: str,
    ci_workflow: str,
    now: float,
    *,
    operator: str,
    human_label: str,
    blocked: Mapping[int, float] | None,
    asking: Mapping[int, tuple[float, str]] | None = None,
    budget_variable: str | None = None,
    strays: Sequence[Stray] | None = None,
    worker_token_file: Path | None = None,
    stale_parked: Mapping[int, tuple[float, str]] | None = None,
    conflicts: Sequence[tuple[int, int]] | None = None,
    vanished: Sequence[tuple[int, int]] | None = None,
    merge_failed: Sequence[tuple[int, str]] | None = None,
) -> tuple[list[Condition], set[str]]:
    """What is alarming now, and the key families that could be read (a
    family that could not be read leaves its episodes as they are).

    `blocked`: the issues carrying the label whose chantier's worker is at
    rest, each with the time it was first seen so; None when the workers or
    the issues could not be read.

    `asking`: the specs whose worker is stopped on a permission prompt, each
    with the time it was first seen so and what the prompt asks; None when
    the windows could not be read.

    `budget_variable`: the repository variable the GitHub minutes' net sets
    (#1296); None, no net and no alert of it.

    `strays`: the processes holding a core outside the CI, the services and
    the workers, now; None when /proc could not be read.

    `worker_token_file`: the workers' GitHub token (#1529); None, not watched.
    Only its state is told, never its value.

    `stale_parked`: the chantiers parked with a red pull request past
    `[waits] parked_red_minutes`, not resumed (#1730) — since when, why;
    None when the history could not be read.

    `conflicts`: the pull requests in conflict with the base that no worker
    carries, told by a comment (#1791) — (pull, since); None, not read.

    `vanished`: the worktrees gone outside `clean` in the last day (#2077) —
    (spec, last seen); None, not read.

    `merge_failed`: the pull requests the merge queue sent back, green, for
    their merge errors in the last day (#2156) — (pull, why); None, not read."""
    alarming: list[Condition] = []
    known: set[str] = set()
    # a worker waiting for the operator: the label's episodes are read each
    # minute by the recorder, the workers with it
    if blocked is not None:
        known.add(f"waiting {project}")
    for issue, since in db.execute(
        "SELECT issue, started_at FROM needs_human WHERE project = ?"
        " AND ended_at IS NULL AND started_at <= ?",
        (project, now - NEEDS_HUMAN_AFTER),
    ):
        if blocked is None or issue not in blocked:
            continue
        since = max(since, blocked[issue])
        if since > now - NEEDS_HUMAN_AFTER:
            continue
        minutes = int((now - since) // 60)
        alarming.append(
            Condition(
                f"waiting {project} #{issue}",
                project,
                texts.ALERT_WAITING.format(
                    issue=issue,
                    operator=operator,
                    label=human_label,
                    minutes=minutes,
                    project=project,
                ),
            )
        )
    if asking is not None:
        known.add(f"asking {project}")
        for spec, (since, request) in sorted(asking.items()):
            if since > now - ASKING_AFTER:
                continue
            alarming.append(
                Condition(
                    f"asking {project} #{spec}",
                    project,
                    texts.ALERT_ASKING.format(
                        spec=spec,
                        minutes=int((now - since) // 60),
                        request=request or "?",
                        project=project,
                    ),
                )
            )
    if stale_parked is not None:
        known.add(f"parked-red {project}")
        for spec, (since, reason) in sorted(stale_parked.items()):
            alarming.append(
                Condition(
                    f"parked-red {project} #{spec}",
                    project,
                    texts.ALERT_PARKED_RED.format(
                        spec=spec,
                        minutes=int((now - since) // 60),
                        why=reason,
                        project=project,
                    ),
                )
            )
    if conflicts is not None:
        known.add(f"conflict {project}")
        for pull, since in conflicts:
            alarming.append(
                Condition(
                    f"conflict {project} #{pull}",
                    project,
                    texts.ALERT_CONFLICT.format(
                        pull=pull, minutes=int((now - since) // 60), project=project
                    ),
                )
            )
    if vanished is not None:
        known.add(f"vanished {project}")
        for spec, last in vanished:
            seen = datetime.fromtimestamp(last, UTC).strftime("%H:%M:%S")
            alarming.append(
                Condition(
                    f"vanished {project} #{spec}",
                    project,
                    texts.ALERT_VANISHED.format(spec=spec, seen=seen, project=project),
                )
            )
    if merge_failed is not None:
        known.add(f"merge-failed {project}")
        for pull, reason in merge_failed:
            alarming.append(
                Condition(
                    f"merge-failed {project} #{pull}",
                    project,
                    texts.ALERT_MERGE_FAILED.format(
                        pull=pull, why=reason, project=project
                    ),
                )
            )
    for period, label in texts.ALERT_QUOTA_WINDOWS.items():
        last = db.execute(
            "SELECT used_percent, resets_at FROM claude_quota_sample"
            " WHERE period = ? ORDER BY seen_at DESC LIMIT 1",
            (period,),
        ).fetchone()
        if last is None or last[1] <= now:
            continue  # unknown: the window's last reading belongs to a past one
        known.add(f"claude {period}")
        if last[0] > QUOTA_ABOVE:
            alarming.append(
                Condition(
                    f"claude {period}",
                    None,
                    texts.ALERT_QUOTA.format(
                        window=label, percent=round(last[0]), threshold=QUOTA_ABOVE
                    ),
                )
            )
    # the GitHub minutes' net (#1296): this month's last reading only — one of
    # a past month says nothing, a new month's first one resolves the episode
    today = datetime.fromtimestamp(now, UTC)
    reading = None
    if budget_variable is not None:
        reading = db.execute(
            "SELECT used_minutes, included_minutes FROM actions_minutes_sample"
            " WHERE year = ? AND month = ? ORDER BY read_at DESC LIMIT 1",
            (today.year, today.month),
        ).fetchone()
    if reading is not None and reading[1] > 0:
        known.add("actions budget")
        percent = reading[0] / reading[1] * 100
        if low(percent):
            alarming.append(
                Condition(
                    "actions budget",
                    None,
                    texts.ALERT_BUDGET.format(
                        percent=percent, threshold=ABOVE, variable=budget_variable
                    ),
                )
            )
    disk = db.execute(
        "SELECT disk_used_percent FROM vps_sample WHERE source = 'live'"
        " AND disk_used_percent IS NOT NULL AND at >= ? ORDER BY at DESC LIMIT 1",
        (now - 10 * 60,),
    ).fetchone()
    if disk is not None:
        known.add("disk")
        if disk[0] > DISK_ABOVE:
            alarming.append(
                Condition(
                    "disk",
                    None,
                    texts.ALERT_DISK.format(percent=disk[0], threshold=DISK_ABOVE),
                )
            )
    if strays is not None:
        known.add("stray")
        for stray in strays:
            alarming.append(
                Condition(
                    f"stray {stray.pid}",
                    None,
                    texts.ALERT_STRAY.format(
                        pid=stray.pid,
                        name=stray.name,
                        percent=stray.percent,
                        minutes=stray.minutes,
                    ),
                )
            )
    if worker_token_file is not None:
        known.add(f"worker token {project}")
        why = worker_token.missing(worker_token_file)
        if why is not None:
            alarming.append(
                Condition(
                    f"worker token {project}",
                    project,
                    texts.ALERT_WORKER_TOKEN.format(why=why, project=project),
                )
            )
    ci = db.execute(
        "SELECT conclusion, url, title FROM ci_run WHERE project = ? AND branch = ?"
        f" AND {store.workflow_is('workflow_path')}"
        " AND conclusion IN ('success', 'failure')"
        " ORDER BY finished_at DESC, attempt DESC LIMIT 1",
        (project, base_branch, ci_workflow, ci_workflow),
    ).fetchone()
    if ci is not None:
        known.add(f"ci {project}")
        if ci[0] == "failure":
            alarming.append(
                Condition(
                    f"ci {project} {base_branch}",
                    project,
                    texts.ALERT_CI_RED.format(
                        branch=base_branch, project=project, title=ci[2], url=ci[1]
                    ),
                )
            )
    return alarming, known


def _family(key: str, known: set[str]) -> bool:
    return any(key == k or key.startswith(k + " ") for k in known)


def step(
    db: sqlite3.Connection,
    alarming: list[Condition],
    known: set[str],
    now: float,
    silent: frozenset[str] = frozenset(),
) -> None:
    """Open and close the episodes (in the caller's transaction); sending is
    `send_pending`'s, outside it: a slow Telegram never holds the base. An
    episode of a family in `silent` opens silenced: never sent (#2193)."""
    open_ = {
        key: (opened, text)
        for key, opened, text in db.execute(
            "SELECT key, opened_at, text FROM alert WHERE closed_at IS NULL"
        )
    }
    wanted = {c.key: c for c in alarming}
    for condition in alarming:
        if condition.key not in open_:
            store.insert(
                db,
                "alert",
                [{"key": condition.key, "project": condition.project,
                  "opened_at": int(now), "text": condition.text,
                  "silenced": int(_muted(condition.key, silent))}],
            )  # fmt: skip
    for key, (opened, _) in open_.items():
        if key not in wanted and _family(key, known):
            db.execute(
                "UPDATE alert SET closed_at = ? WHERE key = ? AND opened_at = ?",
                (int(now), key, opened),
            )


def send_pending(
    db: sqlite3.Connection,
    now: float,
    send: Send | None,
    silent: frozenset[str] = frozenset(),
) -> list[str]:
    if send is None:
        return []
    sent: list[str] = []
    pending = db.execute(
        "SELECT key, opened_at, text, closed_at, opened_sent_at FROM alert"
        " WHERE silenced = 0 AND (opened_sent_at IS NULL"
        " OR (closed_at IS NOT NULL AND closed_sent_at IS NULL)) ORDER BY opened_at"
    ).fetchall()
    for key, opened, text, closed, opened_sent in pending:
        where = (key, opened)
        if _muted(key, silent):
            # switched off since (#2193): its « rétabli » never leaves, nor
            # an opening still waiting for Telegram — even switched on again
            db.execute(
                "UPDATE alert SET silenced = 1 WHERE key = ? AND opened_at = ?", where
            )
            continue
        if opened_sent is None and closed is not None:
            # over before any message left (Telegram down, or no token yet):
            # nothing to tell — a condition still true is always told, late
            db.execute(
                "UPDATE alert SET silenced = 1 WHERE key = ? AND opened_at = ?", where
            )
            continue
        message = (
            f"🔴 {text}"
            if opened_sent is None
            else texts.ALERT_RESOLVED.format(text=text)
        )
        column = "opened_sent_at" if opened_sent is None else "closed_sent_at"
        try:
            send(message)
        except AlertError:
            return sent  # tried again next minute
        db.execute(
            f"UPDATE alert SET {column} = ? WHERE key = ? AND opened_at = ?",
            (int(now), *where),
        )
        sent.append(message)
    return sent


# --- Telegram -------------------------------------------------------------------------


def read_token(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise AlertError(texts.NO_TELEGRAM_TOKEN.format(path=path)) from exc
    if stat.S_IMODE(mode) & 0o077:
        raise AlertError(texts.READABLE_BY_OTHERS.format(path=path))
    if not text:
        raise AlertError(texts.EMPTY_FILE.format(path=path))
    return text


Post = Callable[[str, bytes], tuple[int, bytes]]


def post(url: str, body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def telegram(token_file: Path, chat_id: str, post: Post = post) -> Send:
    """A sender for the chat; `AlertError` at once when there is no token."""
    token = read_token(token_file)

    def send(text: str) -> None:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        try:
            status, answer = post(TELEGRAM_URL.format(token=token), body)
        except OSError as exc:
            # the URL carries the token: only the kind of failure is said
            kind = type(exc).__name__
            raise AlertError(texts.TELEGRAM_UNREACHABLE.format(kind=kind)) from None
        if status != 200:
            try:
                why = json.loads(answer).get("description", "")
            except (ValueError, AttributeError):
                why = ""
            refused = texts.TELEGRAM_REFUSED.format(status=status, why=why)
            raise AlertError(refused.strip())

    return send
