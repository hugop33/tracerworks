"""`serve`'s memory, bounded (#2195).

Measured on the VPS on 01/11: one `gh` costs 45 to 55 MB in the service's
cgroup, and `serve` ran up to five at once (the snapshot's four readers and
the history's) — with the Python heap kept at its high-water mark, the
200 MB ceiling was reached every day. So `serve` runs one `gh` at a time,
hands its free heap back after each cycle, and keeps nothing that grows from
one cycle to the next.
"""

from __future__ import annotations

import threading
import time
import tracemalloc
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tests.tracerworks.fakes import recorded_world
from tests.tracerworks.test_recorder import NOW as RECORDER_NOW
from tests.tracerworks.test_recorder import settings  # noqa: F401  (a fixture)
from tests.tracerworks.test_recorder import world as recorder_world
from tests.tracerworks.test_snapshot import github_world, snapshot
from tracerworks.control import Memo
from tracerworks.memory import release_memory
from tracerworks.process import Result, one_at_a_time
from tracerworks.serve import SnapshotCache
from tracerworks.settings import Settings

TRACERWORKS = "*/tools/tracerworks/*"


class Overlap:
    """A runner double that says how many calls of each program ran at once.
    A `git` call waits for a second one (a barrier): two can only meet if
    they run together — never a matter of timing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now: dict[str, int] = {}
        self.most: dict[str, int] = {}
        self.met = threading.Barrier(2, timeout=5)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> Result:
        program = argv[0]
        with self._lock:
            self._now[program] = self._now.get(program, 0) + 1
            self.most[program] = max(self.most.get(program, 0), self._now[program])
        if program == "git":
            self.met.wait()
        else:
            time.sleep(0.02)
        with self._lock:
            self._now[program] -= 1
        return Result(0, program)


def together(run: Callable[[Sequence[str]], Result], argv: list[str], n: int) -> None:
    threads = [threading.Thread(target=run, args=(argv,)) for _ in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_serve_runs_one_gh_at_a_time_and_the_rest_freely() -> None:
    inner = Overlap()
    run = one_at_a_time(inner, "gh")
    together(run, ["gh", "api", "x"], 5)
    together(run, ["git", "status"], 2)  # each waits for the other
    assert inner.most["gh"] == 1
    assert inner.most["git"] == 2  # tmux, git: a few MB, never queued behind gh
    assert not inner.met.broken


def test_the_gate_hands_back_what_the_runner_said() -> None:
    def runner(
        argv: Sequence[str], *, cwd: Path | None = None, stdin: bytes | None = None
    ) -> Result:
        return Result(3, f"{argv[1]} {cwd} {stdin!r}", "err")

    run = one_at_a_time(runner, "gh")
    assert run(["gh", "pr"], cwd=Path("/w"), stdin=b"x") == Result(
        3, "pr /w b'x'", "err"
    )


def test_the_heap_is_handed_back_after_each_build_ok_or_not() -> None:
    released: list[str] = []
    fail = [False]

    def build() -> dict[str, Any]:
        if fail[0]:
            raise RuntimeError("gh is down")
        return {"a": 1}

    now = [1000.0]
    cache = SnapshotCache(
        build,
        lambda: now[0],
        spawn=lambda job: job(),
        release=lambda: released.append("released"),
        idle_ttl=60,
    )
    cache.warm()
    assert released == ["released"]
    fail[0] = True
    now[0] += 60
    cache.tick()
    assert released == ["released", "released"]


def test_release_memory_never_fails() -> None:
    release_memory()  # glibc's malloc_trim here; nothing where there is none


def growth(cycle: Callable[[], object], warm: int = 40, more: int = 40) -> int:
    """Bytes still held, allocated by tracerworks's code, after `more` cycles
    past `warm` ones: what a cycle keeps for good. The warm cycles let the
    interpreter's own caches (compiled patterns, threads) settle: measured,
    they hold ~22 KB after 40 snapshots, then 4 KB more over the next 40 —
    a leak of a cycle's data would add its size every cycle."""
    held = [tracemalloc.Filter(True, TRACERWORKS)]  # allocated by its own lines
    # traced from the first cycle: a cache entry replaced is not counted new
    tracemalloc.start(10)
    try:
        for _ in range(warm):
            cycle()
        before = tracemalloc.take_snapshot().filter_traces(held)
        for _ in range(more):
            cycle()
        after = tracemalloc.take_snapshot().filter_traces(held)
    finally:
        tracemalloc.stop()
    return sum(s.size_diff for s in after.compare_to(before, "filename"))


def test_forty_snapshots_keep_nothing_from_one_cycle_to_the_next(
    tmp_path: Path,
) -> None:
    """The memo of finished runs (control.py) is the one thing a snapshot
    keeps: bounded by the runs a post shows, not by the cycles."""
    memo: Memo = {}
    fake = github_world(recorded_world())

    def cycle() -> None:
        fake.calls.clear()  # the double's own log of calls is not serve's
        snapshot(fake, tmp_path, memo=memo)

    kept = growth(cycle)
    # measured: 2 KB, once 7 KB; one snapshot's open issues kept a turn, 265 KB
    assert kept < 16_000, f"{kept} bytes kept by 40 snapshots"
    assert len(memo) <= 10


def test_the_turns_of_the_history_keep_nothing_either(
    settings: Settings,  # noqa: F811
) -> None:
    """The prompts, the blocked issues, the sources said: a dict each, by
    issue or by source — never one entry per turn."""
    turn = [0]
    fake, recorder, journal = recorder_world(
        settings, asking=lambda: {900 + turn[0]: "permission: Bash"}
    )

    def cycle() -> None:
        # the double's log of calls and the journal's text are not serve's
        fake.calls.clear()
        journal.seek(0)
        journal.truncate()
        turn[0] += 1
        recorder.sample(RECORDER_NOW + 60 * turn[0])

    kept = growth(cycle, more=150)
    # measured: 24 bytes; the prompts' dict never pruned, 8 KB
    assert kept < 4_000, f"{kept} bytes kept by 150 turns"
