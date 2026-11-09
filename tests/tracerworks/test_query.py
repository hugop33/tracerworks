"""`tracerworks db` (#838): the history base, read only, and its description."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FakeRunner
from tests.tracerworks.test_start_stop import make_env
from tracerworks import store
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, run
from tracerworks.query import _only_reads


def base(tmp_path: Path) -> Path:
    path = tmp_path / "history" / "factory.sqlite3"
    with closing(store.connect(path)) as db:
        store.insert(
            db,
            "vps_sample",
            [
                {"at": 60, "source": "live", "memory_available_mb": 5000},
                {"at": 120, "source": "live", "memory_available_mb": None},
            ],
        )
    return path


def db_cli(tmp_path: Path, *args: str) -> tuple[int, str, str]:
    env, out, err = make_env(FakeRunner(), tmp_path)
    env = replace(env, settings=replace(env.settings, history_db=base(tmp_path)))
    return run(env, ["db", *args]), out.getvalue(), err.getvalue()


def test_a_read_gives_its_rows_as_a_table(tmp_path: Path) -> None:
    code, out, _ = db_cli(
        tmp_path, "SELECT at, memory_available_mb FROM vps_sample ORDER BY at"
    )
    assert code == EXIT_OK
    assert out.splitlines() == [
        "at  | memory_available_mb",
        "----+--------------------",
        "60  | 5000",
        "120 | NULL",
        "(2 lignes)",
    ]


def test_a_read_gives_its_rows_as_json(tmp_path: Path) -> None:
    code, out, _ = db_cli(
        tmp_path, "--json", "SELECT at, memory_available_mb AS mb FROM vps_sample"
    )
    assert code == EXIT_OK
    assert json.loads(out) == [{"at": 60, "mb": 5000}, {"at": 120, "mb": None}]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO vps_sample (at, source) VALUES (1, 'live')",
        "UPDATE vps_sample SET memory_available_mb = 0",
        "DELETE FROM vps_sample",
        "DROP TABLE vps_sample",
        "CREATE TABLE x (y)",
        "PRAGMA user_version = 99",
        "ATTACH DATABASE '/tmp/elsewhere.sqlite3' AS other",
        "BEGIN IMMEDIATE",
        "VACUUM",
        "SELECT 1; DELETE FROM vps_sample",
        "WITH gone AS (SELECT 1) DELETE FROM vps_sample",
    ],
)
def test_every_write_is_refused_and_nothing_changes(tmp_path: Path, sql: str) -> None:
    code, out, err = db_cli(tmp_path, sql)
    assert code == EXIT_REFUSED
    assert "refused" in err and out == ""
    with closing(sqlite3.connect(tmp_path / "history" / "factory.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM vps_sample").fetchone() == (2,)
        assert store.version(db) == store.SCHEMA_VERSION
        assert "x" not in store.tables(db)
    assert not Path("/tmp/elsewhere.sqlite3").exists()


def test_the_read_lock_alone_refuses_a_write(tmp_path: Path) -> None:
    """Contre-épreuve: without the authorizer, `mode=ro` still says no."""
    path = base(tmp_path)
    with (
        closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        db.execute("DELETE FROM vps_sample")


def test_the_authorizer_alone_refuses_a_write(tmp_path: Path) -> None:
    """Contre-épreuve: on a WRITABLE connection, the authorizer still says no."""
    path = base(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.set_authorizer(_only_reads)
        assert db.execute("SELECT count(*) FROM vps_sample").fetchone() == (2,)
        for sql in ("DELETE FROM vps_sample", "ATTACH ':memory:' AS other"):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                db.execute(sql)


def test_an_absent_base_is_said_not_created(tmp_path: Path) -> None:
    env, _, err = make_env(FakeRunner(), tmp_path)
    missing = tmp_path / "none" / "factory.sqlite3"
    env = replace(env, settings=replace(env.settings, history_db=missing))
    assert run(env, ["db", "SELECT 1"]) == EXIT_REFUSED
    assert "no history base" in err.getvalue()
    assert not missing.exists()


def test_the_schema_lists_every_table_and_column(tmp_path: Path) -> None:
    code, out, _ = db_cli(tmp_path, "--schema")
    assert code == EXIT_OK
    assert "vps_sample(at INTEGER, source TEXT, memory_available_mb INTEGER," in out
    with closing(store.connect(tmp_path / "fresh.sqlite3")) as db:
        tables = set(store.tables(db))
    assert {line.split("(")[0] for line in out.splitlines()} == tables
