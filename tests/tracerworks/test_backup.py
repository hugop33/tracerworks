"""The nightly copy of the history base (#842)."""

from __future__ import annotations

import sqlite3
import stat
import threading
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FakeRunner
from tests.tracerworks.test_start_stop import make_env
from tracerworks import store
from tracerworks.backup import KEEP_DAYS, BackupError, copy_name, due, rotate, take
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, run

NIGHT = datetime(2026, 10, 1, 3, 5, tzinfo=UTC).timestamp()
DAY = 86400


def base(tmp_path: Path, rows: int = 500) -> Path:
    path = tmp_path / "db" / "factory.sqlite3"
    with closing(store.connect(path)) as db:
        store.insert(
            db,
            "vps_sample",
            [
                {"at": i, "source": "live", "memory_available_mb": i}
                for i in range(rows)
            ],
        )
    return path


def counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in store.tables(db)}  # fmt: skip


def test_the_copy_reopens_with_the_same_counts(tmp_path: Path) -> None:
    source = base(tmp_path)
    copy = take(source, tmp_path / "backups", NIGHT)
    assert copy.name == "factory-2026-10-01.sqlite3"
    assert counts(copy) == counts(source) and counts(copy)["vps_sample"] == 500
    with closing(sqlite3.connect(copy)) as db:
        assert store.version(db) == store.SCHEMA_VERSION
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_the_copy_is_clean_while_the_base_is_being_written(tmp_path: Path) -> None:
    source = base(tmp_path, rows=0)
    stop = threading.Event()

    def write() -> None:
        with closing(store.connect(source)) as db:
            i = 0
            while not stop.is_set():
                store.insert(db, "vps_sample", [{"at": i, "source": "live"}])
                i += 1

    writer = threading.Thread(target=write)
    writer.start()
    try:
        copy = take(source, tmp_path / "backups", NIGHT)
    finally:
        stop.set()
        writer.join()
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_seven_days_are_kept(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in range(1, 11):  # 21/09 … 30/09
        (folder / copy_name(f"2026-09-{20 + day}")).write_text("")
    (folder / "notes.txt").write_text("not a copy: kept")
    take(base(tmp_path), folder, NIGHT)  # 01/10
    kept = sorted(p.name for p in folder.iterdir())
    assert kept == [copy_name(d) for d in (
        "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29",
        "2026-09-30", "2026-10-01")] + ["notes.txt"]  # fmt: skip
    assert len(kept) - 1 == KEEP_DAYS
    assert rotate(folder, NIGHT) == []  # nothing more to drop


def test_once_a_night_after_its_hour(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    evening = datetime(2026, 10, 1, 2, 59, tzinfo=UTC).timestamp()
    assert not due(folder, evening)
    assert due(folder, NIGHT)
    take(base(tmp_path), folder, NIGHT)
    assert not due(folder, NIGHT + 3600)
    assert due(folder, NIGHT + DAY)


def test_no_base_no_copy(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no base"):
        take(tmp_path / "none.sqlite3", tmp_path / "backups", NIGHT)
    assert not (tmp_path / "backups").exists()


def test_db_backup_takes_one_now(tmp_path: Path) -> None:
    env, out, _ = make_env(FakeRunner(), tmp_path)
    source = base(tmp_path)
    env = replace(
        env,
        settings=replace(env.settings, history_db=source, backup_dir=tmp_path / "b"),
        now=lambda: NIGHT,
    )
    assert run(env, ["db-backup"]) == EXIT_OK
    assert "factory-2026-10-01.sqlite3" in out.getvalue()
    env = replace(env, settings=replace(env.settings, history_db=tmp_path / "x"))
    assert run(env, ["db-backup"]) == EXIT_REFUSED
