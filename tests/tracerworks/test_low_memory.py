"""The memory low for long (#2096): its streak from the history's readings, the
largest processes from a /proc laid out as the VPS's."""

from __future__ import annotations

from pathlib import Path

from tracerworks.conventions import Conventions
from tracerworks.low_memory import STALE_S, largest, low_since

CONVENTIONS = Conventions("worker-", "agent/", Path("/w"))

NOW = 1_793_340_000.0
LOW, HIGH = 1005, 5084  # the health log's 07:15 and 07:30 of 30/10
WORKER = (
    "/user.slice/user-1001.slice/user@1001.service/agents.slice/"
    "tracerworks-worker-1958-1793300000.scope"
)
RUNNER = "/user.slice/user-1001.slice/user@1001.service/ci.slice/runner-1.service"


def minutes_ago(*readings: tuple[int, int]) -> list[tuple[float, int]]:
    return [(NOW - m * 60, mb) for m, mb in readings]


def test_the_streak_starts_at_its_first_low_reading_since_the_last_high() -> None:
    samples = minutes_ago((40, LOW), (30, HIGH), (16, LOW), (8, LOW), (1, LOW))
    assert low_since(samples, NOW) == NOW - 16 * 60


def test_memory_above_the_threshold_now_is_no_streak() -> None:
    assert low_since(minutes_ago((30, LOW), (1, HIGH)), NOW) is None
    assert low_since([], NOW) is None


def test_a_streak_whose_readings_stopped_is_unknown() -> None:
    samples = [(NOW - STALE_S - 60, LOW), (NOW - STALE_S - 120, LOW)]
    assert low_since(samples, NOW) is None


def process(proc: Path, pid: int, name: str, mb: int, cgroup: str) -> None:
    here = proc / str(pid)
    here.mkdir(parents=True)
    fields = ["S", "1", str(pid)] + ["0"] * 18 + [str(mb * 1024 // 4)]
    here.joinpath("stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
    here.joinpath("cgroup").write_text(f"0::{cgroup}\n")


def test_the_three_largest_processes_and_their_worker(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    process(proc, 201, "node", 7100, WORKER)
    process(proc, 300, "Runner.Worker", 900, RUNNER)
    process(proc, 301, "python3", 1200, RUNNER)
    process(proc, 100, "claude", 400, WORKER)
    (proc / "self").mkdir()
    (proc / "uptime").write_text("2045000.00 9000000.00\n")

    assert largest(proc, CONVENTIONS, page_kb=4) == [
        {"pid": 201, "name": "node", "mb": 7100, "spec": 1958},
        {"pid": 301, "name": "python3", "mb": 1200, "spec": None},
        {"pid": 300, "name": "Runner.Worker", "mb": 900, "spec": None},
    ]
    assert largest(tmp_path / "absent", CONVENTIONS) == []


def test_the_streak_is_read_from_the_history_base(tmp_path: Path) -> None:
    from contextlib import closing

    from tracerworks import store
    from tracerworks.low_memory import streak

    path = tmp_path / "factory.sqlite3"
    assert streak(path, NOW) is None, "no base: unknown"
    with closing(store.connect(path)) as db:
        store.insert(db, "vps_sample", [
            {"at": int(NOW) - m * 60, "source": "live", "memory_available_mb": mb}
            for m, mb in ((30, HIGH), (20, LOW), (1, LOW))
        ])  # fmt: skip
    assert streak(path, NOW) == int(NOW) - 20 * 60
