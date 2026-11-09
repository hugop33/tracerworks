"""The nightly copy of the history base (#842).

A clean copy, taken while `serve` keeps writing: SQLite's online backup
(`Connection.backup`, what `sqlite3 .backup` calls), checked by
`PRAGMA integrity_check` before it replaces anything, written owner only,
named by its day. Seven days are kept; older copies go.

`serve`'s history cycle takes it at the first turn past `HOUR` (UTC) of a day
without one; `tracerworks db-backup` takes one now.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tracerworks import texts

HOUR = 3  # UTC: after the nightly CI has started, before the workers' morning
KEEP_DAYS = 7
_NAME = re.compile(r"^factory-(\d{4}-\d{2}-\d{2})\.sqlite3$")


class BackupError(RuntimeError):
    """The copy could not be taken, or is not sound."""


def copy_name(day: str) -> str:
    return f"factory-{day}.sqlite3"


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


def due(folder: Path, now: float) -> bool:
    """Past `HOUR` today, and today's copy not taken yet."""
    moment = datetime.fromtimestamp(now, UTC)
    return moment.hour >= HOUR and not (folder / copy_name(_day(now))).exists()


def take(base: Path, folder: Path, now: float) -> Path:
    """Copy `base` into `folder` as today's copy, then drop the old ones."""
    if not base.is_file():
        raise BackupError(texts.NO_BASE_TO_COPY.format(path=base))
    target = folder / copy_name(_day(now))
    # its own partial file: serve and `db-backup` may copy at the same moment
    partial = target.with_name(f"{target.name}.{os.getpid()}.partial")
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial.unlink(missing_ok=True)
        os.close(os.open(partial, os.O_WRONLY | os.O_CREAT, 0o600))
        with (
            closing(
                sqlite3.connect(f"{base.resolve().as_uri()}?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(partial)) as copy,
        ):
            source.backup(copy)
            sound = copy.execute("PRAGMA integrity_check").fetchone()
        if sound != ("ok",):
            raise BackupError(texts.FAULTY_COPY.format(check=sound))
        partial.replace(target)
    except (sqlite3.Error, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(texts.COPY_FAILED.format(error=exc)) from exc
    finally:
        partial.unlink(missing_ok=True)
    rotate(folder, now)
    return target


def rotate(folder: Path, now: float) -> list[Path]:
    """Drop the copies older than `KEEP_DAYS` days (today counts as one)."""
    oldest = (datetime.fromtimestamp(now, UTC) - timedelta(days=KEEP_DAYS - 1)).date()
    gone = []
    for path in sorted(folder.iterdir()):
        match = _NAME.match(path.name)
        if match and datetime.strptime(match.group(1), "%Y-%m-%d").date() < oldest:
            path.unlink()
            gone.append(path)
    return gone
