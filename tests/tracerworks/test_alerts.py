"""The alerts (#841): one message per episode, then « rétabli », never twice."""

from __future__ import annotations

import sqlite3
import urllib.parse
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from tracerworks import store, texts
from tracerworks.alerts import (
    FAMILIES,
    AlertError,
    conditions,
    family_of,
    muted,
    send_pending,
    step,
    telegram,
)
from tracerworks.strays import Stray

REPO = "owner/repo"
NOW = 1_790_000_000
DAY = 86400


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(tmp_path / "f.sqlite3")) as base:
        yield base


class Outbox:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.down = False

    def __call__(self, text: str) -> None:
        if self.down:
            raise AlertError("Telegram injoignable")
        self.sent.append(text)


def disk(db: sqlite3.Connection, at: float, percent: float | None) -> None:
    store.insert(db, "vps_sample", [{"at": int(at), "source": "live",
        "disk_used_percent": percent}])  # fmt: skip


def turn(
    db: sqlite3.Connection,
    now: float,
    outbox: Outbox | None,
    blocked: dict[int, float] | None = None,
    strays: list[Stray] | None = None,
    worker_token_file: Path | None = None,
    silent: frozenset[str] = frozenset(),
) -> list[str]:
    alarming, known = conditions(
        db, REPO, "main", "ci.yml", now, operator="Sam", human_label="needs-human",
        blocked={} if blocked is None else blocked,
        budget_variable="CI_GITHUB_BUDGET_LOW", strays=strays,
        worker_token_file=worker_token_file,
    )  # fmt: skip
    step(db, alarming, known, now, silent)
    return send_pending(db, now, outbox, silent)


def test_one_episode_is_one_message_then_restored(db: sqlite3.Connection) -> None:
    outbox = Outbox()
    disk(db, NOW, 92)
    assert turn(db, NOW, outbox) == ["🔴 VPS disk at 92% (threshold 90%)"]
    disk(db, NOW + 60, 93)
    assert turn(db, NOW + 60, outbox) == []  # still above: nothing more
    disk(db, NOW + 120, 80)
    assert turn(db, NOW + 120, outbox) == [
        "✅ resolved: VPS disk at 92% (threshold 90%)"
    ]
    disk(db, NOW + 180, 79)
    assert turn(db, NOW + 180, outbox) == []
    disk(db, NOW + 240, 91)  # a new episode
    assert turn(db, NOW + 240, outbox) == ["🔴 VPS disk at 91% (threshold 90%)"]
    assert len(outbox.sent) == 3


def test_a_missing_workers_token_is_one_episode_then_restored(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """#1529: while the file is missing the workers hold the owner's token —
    one message, then « resolved » when the operator writes it; never its value."""
    outbox = Outbox()
    path = tmp_path / "worker-github.token"
    said = (
        f"no workers' GitHub token ({path}): the workers of owner/repo hold the"
        " owner's GitHub token until the operator writes theirs (mode 600)"
    )
    assert turn(db, NOW, outbox, worker_token_file=path) == [f"🔴 {said}"]
    assert turn(db, NOW + 60, outbox, worker_token_file=path) == []
    path.write_text("github_pat_NEVERSAID\n", encoding="utf-8")
    path.chmod(0o644)  # readable by others: still not theirs
    assert turn(db, NOW + 120, outbox, worker_token_file=path) == []
    path.chmod(0o600)
    assert turn(db, NOW + 180, outbox, worker_token_file=path) == [
        f"✅ resolved: {said}"
    ]
    assert not any("NEVERSAID" in m for m in outbox.sent)
    assert turn(db, NOW + 240, outbox, worker_token_file=None) == []


def test_the_disk_alerts_above_90_not_from_85(db: sqlite3.Connection) -> None:
    """The operator, 16/10: the disk swings ~10 points a day around 85 %, so an alert
    at 85 % opened and closed all day long; 90 %, as `vps-health.sh` says."""
    outbox = Outbox()
    disk(db, NOW, 89)
    assert turn(db, NOW, outbox) == []
    disk(db, NOW + 60, 90)
    assert turn(db, NOW + 60, outbox) == []  # at the threshold: not above
    disk(db, NOW + 120, 91)
    assert turn(db, NOW + 120, outbox) == ["🔴 VPS disk at 91% (threshold 90%)"]


def test_a_stray_is_told_once_by_pid_and_restored_when_gone(
    db: sqlite3.Connection,
) -> None:
    """16/10: the glob a closed SSH session left held a core for 20 minutes."""
    outbox = Outbox()
    glob = Stray(pid=2069634, name="python3", percent=98, minutes=17)
    said = (
        "pid 2069634 (python3) has held 98% of a CPU core for 17 min, outside"
        " the CI, the services and the workers: kill 2069634 if nobody waits for it"
    )
    assert turn(db, NOW, outbox, strays=[glob]) == [f"🔴 {said}"]
    later = Stray(pid=2069634, name="python3", percent=99, minutes=18)
    assert turn(db, NOW + 60, outbox, strays=[later]) == []  # the same episode
    assert turn(db, NOW + 120, outbox, strays=None) == []  # unread: stays open
    assert turn(db, NOW + 180, outbox, strays=[]) == [f"✅ resolved: {said}"]


def test_an_unreadable_reading_never_restores(db: sqlite3.Connection) -> None:
    outbox = Outbox()
    disk(db, NOW, 92)
    turn(db, NOW, outbox)
    # no disk reading for 20 minutes: unknown, the episode stays open
    assert turn(db, NOW + 20 * 60, outbox) == []
    assert db.execute("SELECT closed_at FROM alert").fetchall() == [(None,)]


def test_a_message_that_did_not_leave_leaves_next_minute_once(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    outbox.down = True
    disk(db, NOW, 92)
    assert turn(db, NOW, outbox) == []
    outbox.down = False
    disk(db, NOW + 60, 92)
    assert turn(db, NOW + 60, outbox) == ["🔴 VPS disk at 92% (threshold 90%)"]
    disk(db, NOW + 120, 92)
    assert turn(db, NOW + 120, outbox) == []


def test_without_telegram_episodes_are_kept_and_still_told_late(
    db: sqlite3.Connection,
) -> None:
    disk(db, NOW, 92)
    assert turn(db, NOW, None) == []  # no token: noted, not sent
    assert db.execute("SELECT count(*) FROM alert").fetchone() == (1,)
    outbox = Outbox()
    later = NOW + 5 * 3600  # the token comes five hours later
    disk(db, later, 92)
    assert turn(db, later, outbox) == ["🔴 VPS disk at 92% (threshold 90%)"]
    disk(db, later + 60, 50)
    assert turn(db, later + 60, outbox)[0].startswith("✅ resolved")


def test_an_episode_over_before_any_message_says_nothing(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    outbox.down = True
    disk(db, NOW, 92)
    turn(db, NOW, outbox)
    disk(db, NOW + 60, 50)
    outbox.down = False
    assert turn(db, NOW + 60, outbox) == []


def test_each_condition(db: sqlite3.Connection) -> None:
    store.insert(db, "needs_human", [
        {"project": REPO, "issue": 812, "started_at": NOW - 31 * 60},
        {"project": REPO, "issue": 813, "started_at": NOW - 29 * 60}])  # fmt: skip
    store.insert(db, "claude_quota_sample", [
        {"period": "seven_day", "seen_at": NOW - 60, "used_percent": 81.4,
         "resets_at": NOW + 3600},
        {"period": "five_hour", "seen_at": NOW - 60, "used_percent": 95,
         "resets_at": NOW - 1}])  # fmt: skip
    for run_id, conclusion, at in ((1, "success", NOW - 600), (2, "failure", NOW - 60)):
        store.insert(db, "ci_run", [{"project": REPO, "run_id": run_id, "attempt": 1,
            "workflow": "CI", "workflow_path": ".github/workflows/ci.yml",
            "event": "push", "branch": "main", "head_sha": "s",
            "conclusion": conclusion, "finished_at": at, "title": "t",
            "url": "https://x/runs/2"}])  # fmt: skip
    alarming, known = conditions(
        db, REPO, "main", "ci.yml", NOW, operator="Sam", human_label="needs-human",
        blocked={812: NOW - DAY, 813: NOW - DAY},
    )  # fmt: skip
    assert sorted(c.key for c in alarming) == [
        "ci owner/repo main",
        "claude seven_day",  # the 5-hour window is past its reset: unknown
        "waiting owner/repo #812",  # #813 waits only 29 min
    ]
    assert "claude five_hour" not in known
    by_key = {c.key: c.text for c in alarming}
    assert by_key["waiting owner/repo #812"].startswith(
        "#812 waiting for Sam (needs-human)"
    )
    assert "https://x/runs/2" in by_key["ci owner/repo main"]


def asking(db: sqlite3.Connection, issue: int, since: float) -> None:
    store.insert(db, "needs_human", [{"project": REPO, "issue": issue,
        "started_at": int(since)}])  # fmt: skip


def test_a_needs_human_issue_no_worker_is_on_never_alerts(
    db: sqlite3.Connection,
) -> None:
    # #853: #667 kept its label by oversight, no worker on it — the dashboard
    # shows it, Telegram says nothing however long it waits
    outbox = Outbox()
    asking(db, 667, NOW - 3 * 3600)
    assert turn(db, NOW, outbox, blocked={}) == []
    assert db.execute("SELECT count(*) FROM alert").fetchone() == (0,)


def test_a_blocked_worker_alerts_after_30_min_then_recovers(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    asking(db, 840, NOW - 29 * 60)
    assert turn(db, NOW, outbox, blocked={840: NOW - DAY}) == []  # 29 min: not yet
    assert turn(db, NOW + 60, outbox, blocked={840: NOW - DAY}) == [
        "🔴 #840 waiting for Sam (needs-human) for 30 min — owner/repo"
    ]
    assert turn(db, NOW + 120, outbox, blocked={840: NOW - DAY}) == []  # once
    # its worker stopped (or Sam lifted the label): over
    assert turn(db, NOW + 180, outbox, blocked={}) == [
        "✅ resolved: #840 waiting for Sam (needs-human) for 30 min — owner/repo"
    ]


def test_lifting_the_label_under_a_resting_worker_resolves(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    asking(db, 840, NOW - 31 * 60)
    assert len(turn(db, NOW, outbox, blocked={840: NOW - DAY})) == 1
    db.execute("UPDATE needs_human SET ended_at = ?", (NOW + 60,))  # Sam answered
    assert turn(db, NOW + 60, outbox, blocked={}) == [
        "✅ resolved: #840 waiting for Sam (needs-human) for 31 min — owner/repo"
    ]


def test_an_old_label_counts_from_the_worker_s_halt(db: sqlite3.Connection) -> None:
    """#667 labelled for days, then a worker on its chantier comes to rest:
    30 min from that halt, not at once (#853)."""
    outbox = Outbox()
    asking(db, 667, NOW - 3 * DAY)
    assert turn(db, NOW, outbox, blocked={667: NOW - 29 * 60}) == []
    assert turn(db, NOW + 60, outbox, blocked={667: NOW - 29 * 60}) == [
        "🔴 #667 waiting for Sam (needs-human) for 30 min — owner/repo"
    ]


def test_unknown_workers_leave_a_waiting_episode_open(db: sqlite3.Connection) -> None:
    outbox = Outbox()
    asking(db, 840, NOW - 31 * 60)
    assert len(turn(db, NOW, outbox, blocked={840: NOW - DAY})) == 1
    alarming, known = conditions(
        db, REPO, "main", "ci.yml", NOW + 60, operator="Sam",
        human_label="needs-human", blocked=None,
    )  # fmt: skip
    assert not alarming and f"waiting {REPO}" not in known
    step(db, alarming, known, NOW + 60)
    assert send_pending(db, NOW + 60, outbox) == []  # never a « resolved »


def test_a_green_ci_on_main_restores_the_red_one(db: sqlite3.Connection) -> None:
    outbox = Outbox()

    def ci(run_id: int, conclusion: str, at: float) -> None:
        store.insert(db, "ci_run", [{"project": REPO, "run_id": run_id, "attempt": 1,
            "workflow": "CI", "workflow_path": ".github/workflows/ci.yml",
            "event": "push", "branch": "main", "head_sha": "s",
            "conclusion": conclusion, "finished_at": at, "title": "t",
            "url": "u"}])  # fmt: skip

    ci(1, "failure", NOW)
    assert len(turn(db, NOW, outbox)) == 1
    ci(2, "cancelled", NOW + 30)  # neither green nor red: still red
    assert turn(db, NOW + 60, outbox) == []
    ci(3, "success", NOW + 90)
    assert turn(db, NOW + 120, outbox)[0].startswith("✅ resolved: main's CI red")


# --- Telegram -------------------------------------------------------------------------


def test_no_token_disables_the_alerts_and_says_why(tmp_path: Path) -> None:
    with pytest.raises(AlertError, match="no Telegram token"):
        telegram(tmp_path / "none", "42")
    loose = tmp_path / "token"
    loose.write_text("123:abc")
    loose.chmod(0o644)
    with pytest.raises(AlertError, match="chmod 600"):
        telegram(loose, "42")


def test_a_message_goes_to_the_chat_and_the_token_is_never_said(
    tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    token.write_text("123:secret\n")
    token.chmod(0o600)
    posted: list[tuple[str, bytes]] = []

    def post(url: str, body: bytes) -> tuple[int, bytes]:
        posted.append((url, body))
        return 200, b'{"ok":true}'

    telegram(token, "1925", post)("🔴 disque")
    [(url, body)] = posted
    assert url == "https://api.telegram.org/bot123:secret/sendMessage"
    assert urllib.parse.parse_qs(body.decode()) == {
        "chat_id": ["1925"],
        "text": ["🔴 disque"],
    }

    def refused(url: str, body: bytes) -> tuple[int, bytes]:
        return 401, b'{"ok":false,"description":"Unauthorized"}'

    with pytest.raises(AlertError) as said:
        telegram(token, "1925", refused)("x")
    assert "Unauthorized" in str(said.value) and "secret" not in str(said.value)

    def unreachable(url: str, body: bytes) -> tuple[int, bytes]:
        raise OSError(f"cannot reach {url}")

    with pytest.raises(AlertError) as said:
        telegram(token, "1925", unreachable)("x")
    assert "secret" not in str(said.value)


# --- a worker stopped on a permission prompt (#984) --------------------------


def prompted(
    db: sqlite3.Connection, now: float, outbox: Outbox, on: dict[int, float] | None
) -> list[str]:
    alarming, known = conditions(
        db, REPO, "main", "ci.yml", now, operator="Sam", human_label="needs-human",
        blocked={},
        asking=None if on is None else {
            spec: (since, "permission: Bash command: rm node_modules")
            for spec, since in on.items()
        },
    )  # fmt: skip
    step(db, alarming, known, now)
    return send_pending(db, now, outbox)


def test_a_worker_on_a_permission_prompt_alerts_after_5_min_then_recovers(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    assert prompted(db, NOW, outbox, {976: NOW - 4 * 60}) == []  # 4 min: not yet
    message = (
        "#976 blocked for 5 min — permission: Bash command: rm node_modules"
        " — owner/repo"
    )
    assert prompted(db, NOW + 60, outbox, {976: NOW - 4 * 60}) == [f"🔴 {message}"]
    assert prompted(db, NOW + 120, outbox, {976: NOW - 4 * 60}) == []  # once
    # the operator answered: the prompt is gone
    assert prompted(db, NOW + 180, outbox, {}) == [f"✅ resolved: {message}"]


def test_unreadable_windows_leave_a_permission_episode_open(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    assert len(prompted(db, NOW, outbox, {976: NOW - 600})) == 1
    assert prompted(db, NOW + 60, outbox, None) == []  # never a « resolved »


# --- the GitHub minutes' safety net (#1296) ------------------------------------------


def minutes(db: sqlite3.Connection, at: float, used: float) -> None:
    from datetime import UTC, datetime

    month = datetime.fromtimestamp(at, UTC)
    store.insert(db, "actions_minutes_sample", [{"read_at": int(at),
        "account": "owner", "year": month.year, "month": month.month,
        "used_minutes": used, "included_minutes": 3000}])  # fmt: skip


def test_github_minutes_above_ninety_percent_alert_once(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    minutes(db, NOW - 60, 2600.0)
    assert turn(db, NOW, outbox) == []
    minutes(db, NOW, 2731.0)
    [sent] = turn(db, NOW + 60, outbox)
    assert "91%" in sent and "CI_GITHUB_BUDGET_LOW" in sent
    minutes(db, NOW + 60, 2800.0)
    assert turn(db, NOW + 120, outbox) == []


def test_the_next_month_resolves_the_github_minutes_alert(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    minutes(db, NOW, 2731.0)
    turn(db, NOW, outbox)
    # A reading of a past month says nothing: never « rétabli » on a guess.
    assert turn(db, NOW + 40 * DAY, outbox) == []
    minutes(db, NOW + 40 * DAY, 12.0)
    [resolved] = turn(db, NOW + 40 * DAY + 60, outbox)
    assert resolved.startswith("✅")


def test_a_pull_request_the_queue_could_not_merge_alerts_for_a_day(
    db: sqlite3.Connection,
) -> None:
    """#2156: sent back green, its merge failing pass after pass — once."""
    from tracerworks import merge_queue

    why = texts.QUEUE_MERGE_FAILED.format(n=3, error="gh: HTTP 403")
    db.execute(
        "INSERT INTO merge_queue (project, pull, entered_at, state, outcome,"
        " reason, left_at) VALUES (?, 7, ?, 'out', 'sent_back', ?, ?),"
        " (?, 8, ?, 'out', 'sent_back', ?, ?)",
        (REPO, int(NOW) - 600, why, int(NOW),
         REPO, int(NOW) - 600, texts.QUEUE_CONFLICT, int(NOW)),
    )  # fmt: skip
    outbox = Outbox()

    def alert(now: float) -> list[str]:
        failed = merge_queue.merge_failures(db, REPO, now)
        alarming, known = conditions(
            db, REPO, "main", "ci.yml", now, operator="Sam",
            human_label="needs-human", blocked={}, merge_failed=failed,
        )  # fmt: skip
        step(db, alarming, known, now)
        return send_pending(db, now, outbox)

    said = f"PR #7 left the merge queue, green: {why} — {REPO}"
    assert alert(NOW + 60) == [f"🔴 {said}"], "a conflict is not this alert"
    assert alert(NOW + 3600) == []
    assert alert(NOW + merge_queue.MERGE_FAILED_ALERT_S + 60) == [
        f"✅ resolved: {said}"
    ]


# --- switched off from the Settings tab (#2193) ------------------------------------

OFF = frozenset({"disk"})


def episodes(db: sqlite3.Connection) -> list[tuple[str, int, int | None, int]]:
    return db.execute(
        "SELECT key, opened_at, closed_at, silenced FROM alert ORDER BY opened_at"
    ).fetchall()


def test_a_family_off_keeps_its_episode_in_the_base_and_sends_nothing(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    disk(db, NOW, 92)
    assert turn(db, NOW, outbox, silent=OFF) == []
    disk(db, NOW + 60, 93)
    assert turn(db, NOW + 60, outbox) == [], "switched on again: still nothing"
    disk(db, NOW + 120, 80)
    assert turn(db, NOW + 120, outbox) == [], "nor its « resolved »"
    assert episodes(db) == [("disk", NOW, NOW + 120, 1)], "listed in Quality"
    disk(db, NOW + 180, 91)
    assert turn(db, NOW + 180, outbox) == ["🔴 VPS disk at 91% (threshold 90%)"]


def test_a_family_switched_off_while_told_sends_no_resolved(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    disk(db, NOW, 92)
    assert turn(db, NOW, outbox) == ["🔴 VPS disk at 92% (threshold 90%)"]
    disk(db, NOW + 60, 80)
    assert turn(db, NOW + 60, outbox, silent=OFF) == []
    disk(db, NOW + 120, 79)
    assert turn(db, NOW + 120, outbox) == [], "switched on again: still nothing"


def test_another_family_is_still_told_while_one_is_off(db: sqlite3.Connection) -> None:
    outbox = Outbox()
    disk(db, NOW, 92)
    strays = [Stray(4242, "yes", 99, 12)]
    sent = turn(db, NOW, outbox, strays=strays, silent=OFF)
    assert len(sent) == 1 and "4242" in sent[0]


def test_telegram_off_sends_nothing_and_on_again_only_what_opens_after(
    db: sqlite3.Connection,
) -> None:
    outbox = Outbox()
    off = muted(telegram=False, off=frozenset())
    disk(db, NOW, 92)
    assert turn(db, NOW, outbox, strays=[Stray(7, "x", 90, 11)], silent=off) == []
    disk(db, NOW + 60, 92)
    on = muted(telegram=True, off=frozenset())
    assert turn(db, NOW + 60, outbox, strays=[Stray(7, "x", 90, 11)], silent=on) == []
    sent = turn(db, NOW + 120, outbox, strays=[Stray(8, "y", 90, 11)], silent=on)
    # pid 7 gone resolves unsaid; pid 8, opened after, is told
    assert len(sent) == 1 and sent[0].startswith("🔴 pid 8 ")
    assert all("pid 7" not in m for m in outbox.sent)


def test_the_workers_token_has_no_switch_and_is_always_told(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    outbox = Outbox()
    path = tmp_path / "worker-github.token"
    everything = muted(telegram=False, off=frozenset(f.key for f in FAMILIES))
    sent = turn(db, NOW, outbox, worker_token_file=path, silent=everything)
    assert len(sent) == 1 and "no workers' GitHub token" in sent[0]
    assert family_of("worker token owner/repo") is None


@pytest.mark.parametrize(
    ("key", "family"),
    [
        ("waiting owner/repo #12", "waiting"),
        ("asking owner/repo #12", "asking"),
        ("claude seven_day", "claude"),
        ("actions budget", "actions"),
        ("disk", "disk"),
        ("stray 4242", "stray"),
        ("ci owner/repo main", "ci"),
        ("conflict owner/repo #7", "conflict"),
        ("vanished owner/repo #7", "vanished"),
        ("merge-failed owner/repo #7", "merge_failed"),
        ("parked-red owner/repo #7", "parked_red"),
    ],
)
def test_each_episode_key_has_its_family(key: str, family: str) -> None:
    found = family_of(key)
    assert found is not None and found.key == family
