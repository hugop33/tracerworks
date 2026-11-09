"""The factory's history: one SQLite file, outside the repository (#836).

The dashboard's snapshot is the present; this is everything before it. It is
fed by `serve`'s cycle (`recorder.py`) whether anyone watches or not, and read
by the Tendances and Qualité tabs and by `tracerworks db`.

- One table per kind of reading. Times are whole seconds since the epoch, UTC.
- What belongs to ONE project carries its `project` (the GitHub repository,
  `owner/name`); what the machine or the account share (the VPS, the Claude
  quota, the Actions minutes) carries none. Nothing shows several projects yet:
  the base is only ready for it.
- The schema is versioned in the file itself (`PRAGMA user_version`): opening
  it brings it to `SCHEMA_VERSION`, once, under a write lock — two processes
  opening a new base at the same time migrate it once.
- A value that could not be read is NULL or no row at all, never a zero.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from tracerworks import texts

#: Each migration is a list of statements, applied in one transaction; the
#: base's `user_version` is the number of migrations it has. Append only.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE vps_sample (
            at INTEGER NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('live', 'health_log')),
            memory_available_mb INTEGER,
            memory_total_mb INTEGER,
            cpu_percent REAL,
            disk_used_percent REAL,
            disk_available_kb INTEGER,
            disk_total_kb INTEGER,
            PRIMARY KEY (at, source)
        )""",
        """CREATE TABLE claude_quota_sample (
            period TEXT NOT NULL CHECK (period IN ('five_hour', 'seven_day')),
            seen_at INTEGER NOT NULL,
            used_percent REAL NOT NULL,
            resets_at INTEGER NOT NULL,
            PRIMARY KEY (period, seen_at)
        )""",
        """CREATE TABLE actions_minutes_sample (
            read_at INTEGER PRIMARY KEY,
            account TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            used_minutes REAL NOT NULL,
            included_minutes INTEGER NOT NULL
        )""",
        """CREATE TABLE ci_run (
            project TEXT NOT NULL,
            run_id INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            workflow TEXT NOT NULL,
            workflow_path TEXT NOT NULL,
            event TEXT NOT NULL,
            branch TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            pr INTEGER,
            conclusion TEXT NOT NULL,
            created_at INTEGER,
            started_at INTEGER,
            finished_at INTEGER,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (project, run_id, attempt)
        )""",
        """CREATE TABLE ci_job (
            project TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            name TEXT NOT NULL,
            runner_name TEXT,
            machine TEXT CHECK (machine IN ('github', 'vps')),
            conclusion TEXT NOT NULL,
            created_at INTEGER,
            started_at INTEGER,
            completed_at INTEGER,
            wait_s INTEGER,
            duration_s INTEGER,
            billed_minutes INTEGER,
            url TEXT NOT NULL,
            PRIMARY KEY (project, job_id),
            FOREIGN KEY (project, run_id, attempt)
                REFERENCES ci_run (project, run_id, attempt)
        )""",
        "CREATE INDEX ci_job_by_run ON ci_job (project, run_id, attempt)",
        """CREATE TABLE deployment (
            project TEXT NOT NULL,
            run_id INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            at INTEGER NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (project, run_id)
        )""",
        """CREATE TABLE pull_merged (
            project TEXT NOT NULL,
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            branch TEXT NOT NULL,
            spec INTEGER,
            created_at INTEGER,
            merged_at INTEGER NOT NULL,
            url TEXT NOT NULL,
            PRIMARY KEY (project, number)
        )""",
        """CREATE TABLE chantier (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            finished_at INTEGER NOT NULL,
            duration_s INTEGER,
            tokens_total INTEGER NOT NULL,
            tokens_input INTEGER NOT NULL,
            tokens_cache_write INTEGER NOT NULL,
            tokens_cache_read INTEGER NOT NULL,
            tokens_output INTEGER NOT NULL,
            messages INTEGER NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('clean', 'comment')),
            PRIMARY KEY (spec, project)
        )""",
        """CREATE TABLE needs_human (
            project TEXT NOT NULL,
            issue INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            PRIMARY KEY (project, issue, started_at)
        )""",
        """CREATE TABLE backfill (
            name TEXT PRIMARY KEY,
            done_at INTEGER NOT NULL,
            rows INTEGER NOT NULL
        )""",
    ),
    # 2 — what the Qualité tab needs (#840)
    (
        """CREATE TABLE spec (
            number INTEGER NOT NULL,
            project TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (number, project)
        )""",
        """CREATE TABLE ci_log (
            project TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            read_at INTEGER NOT NULL,
            tests INTEGER,
            PRIMARY KEY (project, job_id)
        )""",
        """CREATE TABLE ci_test_failure (
            project TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            test TEXT NOT NULL,
            PRIMARY KEY (project, job_id, test)
        )""",
        """CREATE TABLE branch_read (
            project TEXT NOT NULL,
            branch TEXT NOT NULL,
            read_at INTEGER NOT NULL,
            pr INTEGER,
            files INTEGER,
            PRIMARY KEY (project, branch)
        )""",
        """CREATE TABLE branch_file (
            project TEXT NOT NULL,
            branch TEXT NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (project, branch, path)
        )""",
    ),
    # 3 — the alerts (#841): one episode per crossing, its two messages
    (
        """CREATE TABLE alert (
            key TEXT NOT NULL,
            project TEXT,
            opened_at INTEGER NOT NULL,
            text TEXT NOT NULL,
            closed_at INTEGER,
            opened_sent_at INTEGER,
            closed_sent_at INTEGER,
            silenced INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, opened_at)
        )""",
        # one open episode per alert, and per waiting issue: held by the base
        "CREATE UNIQUE INDEX alert_open ON alert (key) WHERE closed_at IS NULL",
        "CREATE UNIQUE INDEX needs_human_open ON needs_human (project, issue)"
        " WHERE ended_at IS NULL",
    ),
    # 4 — the history from the project's start (#851): the parts imported
    # since a date are imported again, from the first run; every row is
    # `INSERT OR IGNORE`, so what is already there stays, once
    (
        "DELETE FROM backfill"
        " WHERE name IN ('ci', 'pull_merged', 'chantier', 'health_log')",
    ),
    # 5 — the size of a merged pull request, for the Delivered panel (#860);
    # the merged PRs are read again so that those already there get theirs
    (
        "ALTER TABLE pull_merged ADD COLUMN changed_files INTEGER",
        "ALTER TABLE pull_merged ADD COLUMN additions INTEGER",
        "ALTER TABLE pull_merged ADD COLUMN deletions INTEGER",
        "ALTER TABLE pull_merged ADD COLUMN closed_issues INTEGER",
        "DELETE FROM backfill WHERE name = 'pull_merged'",
    ),
    # 6 — a worker parked while its CI runs (#1025): the Claude session that
    # `claude --resume` reopens if the CI goes red; what it was already told
    (
        """CREATE TABLE parked (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            session_id TEXT,
            pull INTEGER NOT NULL,
            parked_at INTEGER NOT NULL,
            resumed_for TEXT,
            resumed_at INTEGER,
            PRIMARY KEY (spec, project)
        )""",
    ),
    # 7 — the lane (« poste ») each chantier is worked on (#1262)
    (
        """CREATE TABLE lane (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            lane INTEGER NOT NULL CHECK (lane >= 1),
            attached_at INTEGER NOT NULL,
            PRIMARY KEY (spec, project)
        )""",
    ),
    # 8 — a chantier set aside for the red CI of its lane (#1263): the Claude
    # session `claude --resume` reopens once its lane has nothing in dev
    (
        """CREATE TABLE set_aside (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            set_aside_at INTEGER NOT NULL,
            for_spec INTEGER,
            PRIMARY KEY (spec, project)
        )""",
    ),
    # 9 — a red the worker judged unrelated is rerun once per commit (#1280):
    # the head commit rerun, and when
    (
        "ALTER TABLE parked ADD COLUMN rerun_for TEXT",
        "ALTER TABLE parked ADD COLUMN rerun_at INTEGER",
    ),
    # 10 — the merge queue (#1714): one row per stay of a pull request in
    # it, from its entry to its exit; at most one stay open per pull request
    (
        """CREATE TABLE merge_queue (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            entered_at INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('waiting', 'testing', 'out')),
            tested_main TEXT,
            updated_from TEXT,
            tested_head TEXT,
            started_at INTEGER,
            run_url TEXT,
            machine_rerun_at INTEGER,
            left_at INTEGER,
            outcome TEXT CHECK (outcome IN ('merged', 'sent_back', 'gone')),
            reason TEXT
        )""",
        "CREATE UNIQUE INDEX merge_queue_open ON merge_queue (project, pull)"
        " WHERE state != 'out'",
    ),
    # 11 — one state per chantier (#1729): each change, with its cause; a
    # change the transitions table refused is kept, marked
    (
        """CREATE TABLE chantier_state (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            at INTEGER NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('queued', 'development',
                'light_check', 'merge_queue', 'human', 'blocked', 'parked', 'done')),
            reason TEXT,
            lane INTEGER,
            cause TEXT NOT NULL,
            refused INTEGER NOT NULL DEFAULT 0 CHECK (refused IN (0, 1))
        )""",
        "CREATE INDEX chantier_state_spec ON chantier_state (project, spec, at)",
    ),
    # 12 — a wait for a human past its deadline (#1730): the label tracerworks
    # put on the spec, and its comment, once per episode; taken off when the
    # worker works again
    (
        """CREATE TABLE human_wait (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            entered_at INTEGER NOT NULL,
            labeled_at INTEGER NOT NULL,
            cleared_at INTEGER,
            UNIQUE (project, spec, entered_at)
        )""",
    ),
    # 13 — the merge queue's adoptions (#1783): a pull request the queue
    # took in (its one comment written), or refused for an auto-merge that
    # would not come off — what `status` says
    (
        """CREATE TABLE merge_queue_adoption (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            at INTEGER NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('adopted', 'refused')),
            reason TEXT
        )""",
        "CREATE INDEX merge_queue_adoption_pull"
        " ON merge_queue_adoption (project, pull, at)",
    ),
    # 14 — the nudges of a worker that asked a permission it need not ask
    # (#1789): each one sent, with its worktree's commit and upstream then —
    # the third without progress puts the human label instead
    (
        """CREATE TABLE nudge (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            at INTEGER NOT NULL,
            progress TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('sent', 'needs_human'))
        )""",
        "CREATE INDEX nudge_spec ON nudge (project, spec, at)",
    ),
    # 15 — a pull request's head left without CI (#1790): when it was first
    # seen so, when tracerworks re-ran its last run (or said there was none) —
    # once per head
    (
        """CREATE TABLE ci_revival (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            seen_at INTEGER NOT NULL,
            acted_at INTEGER,
            run INTEGER,
            UNIQUE (project, pull, head)
        )""",
    ),
    # 16 — a pull request in conflict with the base (#1791): one episode
    # from `CONFLICTING` to `MERGEABLE` again, told once — its worker woken,
    # left to the parking's resume, or a comment and an alert
    (
        """CREATE TABLE pull_conflict (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            seen_at INTEGER NOT NULL,
            told_at INTEGER,
            how TEXT CHECK (how IN ('message', 'parked', 'comment')),
            cleared_at INTEGER
        )""",
        "CREATE UNIQUE INDEX pull_conflict_open ON pull_conflict (project, pull)"
        " WHERE cleared_at IS NULL",
    ),
    # 17 — a machine's red in a light lane, re-run once without waking
    # anyone (#1792): per pull request and head, its worker parked or none;
    # a test's red read once, never re-run (its logs are not read again)
    (
        """CREATE TABLE machine_rerun (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            at INTEGER NOT NULL,
            by TEXT NOT NULL CHECK (by IN ('parked', 'no_worker', 'test_red')),
            UNIQUE (project, pull, head)
        )""",
    ),
    # 18 — a process the kernel's OOM killer took (#1822): the systemd unit of
    # the operator's session that failed with `oom-kill`, read from its journal
    (
        """CREATE TABLE oom_kill (
            at INTEGER NOT NULL,
            unit TEXT NOT NULL,
            PRIMARY KEY (at, unit)
        )""",
    ),
    # 19 — the factory's order replaces the lanes (#1823): a chantier's entry
    # into the factory, its attachment's time carried over; the lanes go
    (
        """CREATE TABLE entry (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            entered_at INTEGER NOT NULL,
            PRIMARY KEY (spec, project)
        )""",
        "INSERT INTO entry (project, spec, entered_at)"
        " SELECT project, spec, attached_at FROM lane",
        "DROP TABLE lane",
    ),
    # 20 — a light lane's green reused for the same code diff (#1844): one
    # verdict per pull request and head, its comment written once; and the
    # light lane the queue cancelled to run its full suite, re-run once over
    (
        """CREATE TABLE merge_queue_reuse (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            at INTEGER NOT NULL,
            reused_from TEXT,
            why TEXT NOT NULL,
            UNIQUE (project, pull, head)
        )""",
        "ALTER TABLE merge_queue ADD COLUMN replaced_run INTEGER",
    ),
    # 21 — the runs of a worker's head in conflict, force-cancelled (#2013):
    # once per pull request and head, written when one was cancelled
    (
        """CREATE TABLE conflict_cancel (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            at INTEGER NOT NULL,
            runs TEXT NOT NULL,
            UNIQUE (project, pull, head)
        )""",
    ),
    # 22 — the worktrees `clean` saw on its last pass, and those that vanished
    # since without `clean` removing them (#2077): one row each, said once
    (
        """CREATE TABLE worktree_seen (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY (spec, project)
        )""",
        """CREATE TABLE worktree_vanished (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            noticed_at INTEGER NOT NULL
        )""",
    ),
    # 23 — a full suite's red its worker judged unrelated sends the pull
    # request back to the head of the queue (#2078): once per head, the stay
    # it entered as
    (
        """CREATE TABLE merge_queue_readmit (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            at INTEGER NOT NULL,
            stay INTEGER NOT NULL,
            run_url TEXT,
            UNIQUE (project, pull, head)
        )""",
        "ALTER TABLE merge_queue ADD COLUMN ahead INTEGER NOT NULL DEFAULT 0",
    ),
    # 24 — a red on a step of the code's, never a machine's (#2080): once
    # per pull request and head, its step; the worker at once, nothing re-run
    (
        """CREATE TABLE code_red (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            at INTEGER NOT NULL,
            step TEXT NOT NULL,
            UNIQUE (project, pull, head)
        )""",
    ),
    # 25 — a worker's test stopped by the relauncher, overdue (#2096): its
    # command's entry, never its command line
    (
        """CREATE TABLE test_kill (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            at INTEGER NOT NULL,
            pid INTEGER NOT NULL,
            command TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            memory_mb INTEGER NOT NULL,
            PRIMARY KEY (project, at, pid)
        )""",
    ),
    # 26 — a spec whose `start` was refused for want of a place (#2118): it
    # takes the next one, before any new spec; out once relaunched, closed
    # or cleaned
    (
        """CREATE TABLE place_wait (
            project TEXT NOT NULL,
            spec INTEGER NOT NULL,
            asked_at INTEGER NOT NULL,
            PRIMARY KEY (spec, project)
        )""",
    ),
    # 27 — the runs of a pull request waiting in the merge queue, cancelled
    # (#2109): once per run and attempt; no red until the queue tests it again
    (
        """CREATE TABLE merge_queue_cancel (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            run INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            at INTEGER NOT NULL,
            url TEXT NOT NULL,
            UNIQUE (project, run, attempt)
        )""",
        "CREATE INDEX merge_queue_cancel_pull"
        " ON merge_queue_cancel (project, pull, at)",
    ),
    # 28 — the passes in a row where the merge of the pull request in test
    # failed for another reason than « not mergeable » (#2156): past
    # `merge_error_passes`, it leaves with the error
    ("ALTER TABLE merge_queue ADD COLUMN merge_errors INTEGER NOT NULL DEFAULT 0",),
    # 29 — a short test's red rerun once by the merge queue (#2130, spec
    # #2127): `by` = 'flaky', its tests (one per line) for the bugs a green
    # rerun opens. SQLite changes no CHECK in place: the table is rebuilt
    (
        """CREATE TABLE machine_rerun_29 (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            pull INTEGER NOT NULL,
            head TEXT NOT NULL,
            at INTEGER NOT NULL,
            by TEXT NOT NULL
                CHECK (by IN ('parked', 'no_worker', 'test_red', 'flaky')),
            tests TEXT,
            UNIQUE (project, pull, head)
        )""",
        "INSERT INTO machine_rerun_29 (id, project, pull, head, at, by)"
        " SELECT id, project, pull, head, at, by FROM machine_rerun",
        "DROP TABLE machine_rerun",
        "ALTER TABLE machine_rerun_29 RENAME TO machine_rerun",
    ),
    # 30 — the bug a flaky test opened by itself (#2224, spec #2219): one per
    # test while open, its issue, and the last failure it has said
    (
        """CREATE TABLE flaky_bug (
            project TEXT NOT NULL,
            test TEXT NOT NULL,
            issue INTEGER NOT NULL,
            opened_at INTEGER NOT NULL,
            said_until INTEGER NOT NULL,
            PRIMARY KEY (project, test)
        )""",
    ),
    # 31 — the model and the effort a finished chantier's worker last ran
    # with, as its log says (#2253): NULL for the chantiers of before, and
    # whatever its log does not say
    (
        "ALTER TABLE chantier ADD COLUMN model TEXT",
        "ALTER TABLE chantier ADD COLUMN effort TEXT",
    ),
    # 32 — the VPS load every 5 minutes (#2279), read from the health log:
    # the CPU averaged over the period, and each consumer's share of it;
    # NULL for what the reading said `?`
    (
        """CREATE TABLE vps_load (
            at INTEGER PRIMARY KEY,
            cpu_percent REAL,
            iowait_percent REAL,
            load1 REAL,
            load5 REAL,
            load15 REAL,
            cores INTEGER,
            memory_available_mb INTEGER,
            cpu_workers REAL,
            cpu_ci REAL,
            cpu_factory REAL,
            cpu_containers REAL,
            cpu_rest REAL
        )""",
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)
#: The size of a merged pull request (migration 5, #860): NULL when unknown.
PULL_SIZE = ("changed_files", "additions", "deletions", "closed_issues")


def workflow_is(column: str) -> str:
    """The runs of one workflow, named by its file (`ci.yml`) or its path;
    two parameters, both the name."""
    return f"({column} = ? OR {column} LIKE '%/' || ?)"


BUSY_MS = 10_000  # serve and clean may write at once: wait, never fail


class StoreError(RuntimeError):
    """The base is newer than this `tracerworks`, or cannot be opened."""


def connect(path: Path) -> sqlite3.Connection:
    """The base, created if absent (owner only), brought to the schema."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    db = sqlite3.connect(path, timeout=BUSY_MS / 1000, isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    migrate(db)
    return db


def version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def migrate(db: sqlite3.Connection) -> int:
    """Apply the migrations the base lacks; the version it ends at."""
    if version(db) == SCHEMA_VERSION:
        return SCHEMA_VERSION
    db.execute("BEGIN IMMEDIATE")
    try:
        current = version(db)  # again, under the lock: another may have done it
        if current > SCHEMA_VERSION:
            raise StoreError(
                texts.BASE_TOO_NEW.format(current=current, known=SCHEMA_VERSION)
            )
        for number, statements in enumerate(MIGRATIONS[current:], start=current + 1):
            for statement in statements:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {number}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


def insert(
    db: sqlite3.Connection, table: str, rows: Sequence[dict[str, object]]
) -> int:
    """`INSERT OR IGNORE` each row (a reading already there stays as it was);
    how many were new. The table and column names are the code's, never input."""
    added = 0
    for row in rows:
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cursor = db.execute(
            f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({marks})",
            tuple(row.values()),
        )
        added += cursor.rowcount
    return added


def tables(db: sqlite3.Connection) -> list[str]:
    return [
        str(name)
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
