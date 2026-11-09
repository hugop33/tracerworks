"""`tracerworks db`: the history base, READ ONLY — Claude Code first (#838).

Three locks, each enough alone:

- the file is opened `mode=ro`: SQLite itself refuses any write;
- `PRAGMA query_only`: no statement may change the base;
- an authorizer that lets through reads only — no ATTACH (another file,
  possibly writable), no write of any kind, no transaction.

One statement per call (`sqlite3` refuses more). The answer is a text table or
JSON; `--schema` lists every table with its columns and types, what a reader
consults before writing a query (the meaning of each column is in the
project's runbook).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from tracerworks import texts

#: What a read may do: read columns, select, call functions, read pragmas
#: (`table_info` and the like; a pragma that writes is refused by `mode=ro`).
_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_RECURSIVE,
}


class QueryError(ValueError):
    """The base is absent, or the query was refused or failed."""


def _only_reads(action: int, *_: object) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise QueryError(texts.NO_HISTORY_BASE.format(path=path))
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.execute("PRAGMA query_only = ON")
    db.set_authorizer(_only_reads)
    return db


def query(path: Path, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    """(column names, rows) of one read-only statement."""
    with closing(open_read_only(path)) as db:
        try:
            cursor = db.execute(sql)
            rows = cursor.fetchall()
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise QueryError(texts.STATEMENT_FAILED.format(error=exc)) from exc
        columns = [c[0] for c in cursor.description or []]
    return columns, rows


def schema(path: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """Every table and its columns (name, type), in name order."""
    with closing(open_read_only(path)) as db:
        names = [
            str(n)
            for (n,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return [
            (
                name,
                [
                    (str(c[1]), str(c[2]))
                    for c in db.execute(f"PRAGMA table_info({name})")
                ],
            )
            for name in names
        ]


def _cell(value: Any) -> str:
    return "NULL" if value is None else str(value)


def as_table(columns: Sequence[str], rows: Sequence[tuple[Any, ...]]) -> str:
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [max([len(c), *(len(r[i]) for r in cells)]) for i, c in enumerate(columns)]
    line = " | ".join
    out = [line(c.ljust(w) for c, w in zip(columns, widths, strict=True))]
    out.append("-+-".join("-" * w for w in widths))
    out += [line(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in cells]
    out.append(f"({len(rows)} ligne{'s' if len(rows) != 1 else ''})")
    return "\n".join(s.rstrip() for s in out)


def as_json(columns: Sequence[str], rows: Sequence[tuple[Any, ...]]) -> str:
    return json.dumps(
        [dict(zip(columns, r, strict=True)) for r in rows], ensure_ascii=False
    )


def describe_schema(tables: Sequence[tuple[str, list[tuple[str, str]]]]) -> str:
    return "\n".join(
        f"{name}({', '.join(f'{c} {t}' for c, t in columns)})"
        for name, columns in tables
    )
