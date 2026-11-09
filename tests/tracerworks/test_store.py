"""The history base (#836): versioned schema, idempotent migration, private file."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from tracerworks import store


def test_a_new_base_is_created_private_at_the_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "history" / "factory.sqlite3"
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION
        assert {
            "vps_sample",
            "claude_quota_sample",
            "actions_minutes_sample",
            "ci_run",
            "ci_job",
            "deployment",
            "pull_merged",
            "chantier",
            "needs_human",
            "backfill",
        } <= set(store.tables(db))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_opening_again_migrates_nothing_and_keeps_the_rows(tmp_path: Path) -> None:
    path = tmp_path / "factory.sqlite3"
    with closing(store.connect(path)) as db:
        store.insert(db, "backfill", [{"name": "ci", "done_at": 1, "rows": 3}])
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION
        assert db.execute("SELECT name, rows FROM backfill").fetchall() == [("ci", 3)]


def test_a_base_left_behind_is_brought_up_one_migration_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "factory.sqlite3"
    first = (("CREATE TABLE a (x INTEGER)",),)
    monkeypatch.setattr(store, "MIGRATIONS", first)
    monkeypatch.setattr(store, "SCHEMA_VERSION", 1)
    with closing(store.connect(path)) as db:
        db.execute("INSERT INTO a VALUES (7)")
    later = (*first, ("CREATE TABLE b (y INTEGER)",))
    monkeypatch.setattr(store, "MIGRATIONS", later)
    monkeypatch.setattr(store, "SCHEMA_VERSION", 2)
    with closing(store.connect(path)) as db:
        assert store.version(db) == 2
        assert db.execute("SELECT x FROM a").fetchall() == [(7,)]
        assert "b" in store.tables(db)


def test_a_failed_migration_leaves_the_base_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "factory.sqlite3"
    broken = (("CREATE TABLE a (x INTEGER)", "CREATE TABLE a (x INTEGER)"),)
    monkeypatch.setattr(store, "MIGRATIONS", broken)
    monkeypatch.setattr(store, "SCHEMA_VERSION", 1)
    with pytest.raises(sqlite3.OperationalError):
        store.connect(path)
    with closing(sqlite3.connect(path)) as db:
        assert store.version(db) == 0
        assert store.tables(db) == []


def test_a_base_newer_than_the_tool_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "factory.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    with pytest.raises(store.StoreError, match="schema"):
        store.connect(path)


def test_a_reading_already_there_is_ignored_not_overwritten(tmp_path: Path) -> None:
    with closing(store.connect(tmp_path / "f.sqlite3")) as db:
        row = {"at": 10, "source": "live", "memory_available_mb": 5000}
        assert store.insert(db, "vps_sample", [row]) == 1
        again = {**row, "memory_available_mb": 1}
        assert store.insert(db, "vps_sample", [again]) == 0
        assert db.execute("SELECT memory_available_mb FROM vps_sample").fetchall() == [
            (5000,)
        ]


def test_one_open_episode_per_alert_and_per_waiting_issue(tmp_path: Path) -> None:
    """Idempotence held by the base: two writers cannot open the same twice."""
    with closing(store.connect(tmp_path / "f.sqlite3")) as db:
        first = {"key": "disk", "opened_at": 1, "text": "90 %"}
        assert store.insert(db, "alert", [first]) == 1
        assert store.insert(db, "alert", [{**first, "opened_at": 2}]) == 0
        waiting = {"project": "o/r", "issue": 7, "started_at": 1}
        assert store.insert(db, "needs_human", [waiting]) == 1
        assert store.insert(db, "needs_human", [{**waiting, "started_at": 2}]) == 0
        db.execute("UPDATE alert SET closed_at = 3")
        assert store.insert(db, "alert", [{**first, "opened_at": 4}]) == 1


def at_version(
    path: Path, version: int, monkeypatch: pytest.MonkeyPatch
) -> sqlite3.Connection:
    """A base as `version` left it: only its first migrations applied."""
    real = store.MIGRATIONS
    monkeypatch.setattr(store, "MIGRATIONS", real[:version])
    monkeypatch.setattr(store, "SCHEMA_VERSION", version)
    db = store.connect(path)
    monkeypatch.setattr(store, "MIGRATIONS", real)
    monkeypatch.setattr(store, "SCHEMA_VERSION", len(real))
    return db


def test_migration_4_forgets_the_backfills_cut_at_a_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#851: the CI runs, merged pull requests and usage comments were
    imported since `[history] since` (01/09), and the health journal: their
    backfill is redone, from the project's start."""
    path = tmp_path / "factory.sqlite3"
    with closing(at_version(path, 3, monkeypatch)) as db:
        for name in ("ci", "pull_merged", "chantier", "health_log"):
            store.insert(db, "backfill", [{"name": name, "done_at": 1, "rows": 1}])
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 4
        assert db.execute("SELECT name FROM backfill").fetchall() == []


def test_migration_5_gives_merged_prs_a_size_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#860: a PR recorded before has an unknown size (NULL, never 0), and the
    merged PRs are read again to fill it; the other backfills stay done."""
    path = tmp_path / "factory.sqlite3"
    with closing(at_version(path, 4, monkeypatch)) as db:
        store.insert(db, "pull_merged", [{"project": "o/r", "number": 1,
            "title": "t", "branch": "b", "spec": None, "created_at": 1,
            "merged_at": 2, "url": "u"}])  # fmt: skip
        for name in ("ci", "pull_merged"):
            store.insert(db, "backfill", [{"name": name, "done_at": 1, "rows": 1}])
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 5
        assert db.execute(
            "SELECT changed_files, additions, deletions, closed_issues FROM pull_merged"
        ).fetchall() == [(None, None, None, None)]
        assert db.execute("SELECT name FROM backfill").fetchall() == [("ci",)]


def test_migration_19_carries_each_lane_into_the_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1823: a base at 18 keeps each chantier's time of attachment as its
    entry into the factory; the lanes go; one entry per chantier."""
    path = tmp_path / "factory.sqlite3"
    with closing(at_version(path, 18, monkeypatch)) as db:
        store.insert(db, "lane", [
            {"project": "o/r", "spec": 1, "lane": 2, "attached_at": 30},
            {"project": "o/r", "spec": 5, "lane": 1, "attached_at": 10},
        ])  # fmt: skip
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 19
        assert db.execute(
            "SELECT spec, entered_at FROM entry ORDER BY spec"
        ).fetchall() == [(1, 30), (5, 10)]
        assert "lane" not in store.tables(db)
        row = {"project": "o/r", "spec": 1, "entered_at": 99}
        assert store.insert(db, "entry", [row]) == 0, "one entry per chantier"


def test_migration_8_keeps_a_set_aside_session_per_chantier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1263: a base at 7 keeps its lanes — its entries since #1823 — and
    gets `set_aside`."""
    path = tmp_path / "factory.sqlite3"
    with closing(at_version(path, 7, monkeypatch)) as db:
        store.insert(db, "lane", [{"project": "o/r", "spec": 1, "lane": 2,
            "attached_at": 3}])  # fmt: skip
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 8
        assert db.execute("SELECT spec, entered_at FROM entry").fetchall() == [(1, 3)]
        row = {"project": "o/r", "spec": 1, "session_id": "s", "set_aside_at": 4,
               "for_spec": 5}  # fmt: skip
        assert store.insert(db, "set_aside", [row]) == 1
        assert store.insert(db, "set_aside", [{**row, "session_id": "t"}]) == 0


def test_migration_9_lets_a_parked_chantier_remember_its_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1280: a base at 8 keeps its parked sessions, each without a rerun yet."""
    path = tmp_path / "factory.sqlite3"
    with closing(at_version(path, 8, monkeypatch)) as db:
        store.insert(db, "parked", [{"project": "o/r", "spec": 1, "session_id": "s",
            "pull": 2, "parked_at": 3, "resumed_for": "c:red"}])  # fmt: skip
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 9
        assert db.execute(
            "SELECT spec, resumed_for, rerun_for, rerun_at FROM parked"
        ).fetchall() == [(1, "c:red", None, None)]


def test_migration_29_keeps_the_machine_reruns_and_lets_the_queue_say_flaky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2130: a base at 28 keeps its rows, each without tests; `flaky` is
    accepted, any other word still refused, one row per head still."""
    path = tmp_path / "factory.sqlite3"
    row = {"project": "o/r", "pull": 7, "head": "h", "at": 3, "by": "parked"}
    with closing(at_version(path, 28, monkeypatch)) as db:
        store.insert(db, "machine_rerun", [row])
    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 29
        assert db.execute(
            "SELECT pull, head, at, by, tests FROM machine_rerun"
        ).fetchall() == [(7, "h", 3, "parked", None)]
        flaky = {**row, "head": "k", "by": "flaky", "tests": "a.py::t"}
        assert store.insert(db, "machine_rerun", [flaky]) == 1
        assert store.insert(db, "machine_rerun", [{**flaky, "at": 4}]) == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO machine_rerun (project, pull, head, at, by)"
                " VALUES ('o/r', 7, 'z', 5, 'x')"
            )
