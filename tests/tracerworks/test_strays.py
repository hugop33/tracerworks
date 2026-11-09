"""The strays: a process holding a core outside the CI, the services and the
workers' tmux — read from a /proc laid out as the VPS's was on 16/10."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracerworks.strays import STRAY_ABOVE, STRAY_AFTER, Stray, strays

UID = 1001
TICKS = 100
UPTIME = 2_045_000.0  # seconds since boot, as /proc/uptime says it
USER = f"/user.slice/user-{UID}.slice"
SERVICE = f"{USER}/user@{UID}.service"


def process(
    proc: Path,
    pid: int,
    name: str | bytes,
    cgroup: str,
    *,
    age: float,
    cpu: float,
    parent: int = 1,
    uid: int = UID,
    state: str = "R",
) -> None:
    """A /proc/<pid> started `age` seconds ago that has used `cpu` seconds."""
    here = proc / str(pid)
    here.mkdir()
    start = round((UPTIME - age) * TICKS)
    used = round(cpu * TICKS)
    # pid (comm) state ppid ... utime(14) stime(15) ... starttime(22)
    fields = [state, str(parent)] + ["0"] * 9 + [str(used), "0"] + ["0"] * 6
    raw = name if isinstance(name, bytes) else name.encode()
    tail = " ".join([*fields, str(start), "0", "0"]).encode()
    here.joinpath("stat").write_bytes(f"{pid} (".encode() + raw + b") " + tail + b"\n")
    here.joinpath("status").write_bytes(
        b"Name:\t" + raw + f"\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n".encode()
    )
    here.joinpath("cgroup").write_text(f"0::{cgroup}\n")


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    root.joinpath("uptime").write_text(f"{UPTIME} 9000000.00\n")
    root.joinpath("self").mkdir()  # not a pid: skipped
    return root


def found(proc: Path) -> list[Stray] | None:
    return strays(proc, uid=UID, ticks=TICKS)


def test_the_glob_left_by_a_closed_ssh_session_is_a_stray(proc: Path) -> None:
    """16/10, 11:05 → 11:26 UTC: a diagnostic's `python3 -` walking ~/.local,
    its SSH session gone, held one core while the CI's timing tests went red."""
    process(proc, 2069634, "python3", f"{USER}/session-86141.scope", age=17 * 60,
            cpu=16.6 * 60)  # fmt: skip

    assert found(proc) == [Stray(pid=2069634, name="python3", percent=98, minutes=17)]


def test_the_ci_the_services_and_the_workers_are_never_strays(proc: Path) -> None:
    def busy(pid: int, name: str, cgroup: str) -> None:
        process(proc, pid, name, cgroup, age=3600, cpu=3500)  # near a full core

    busy(10, "pytest", f"{SERVICE}/app.slice/actions-runner-vps-1.service")
    busy(11, "postgres", f"{SERVICE}/user.slice/docker-479a7ef459a4.scope")
    busy(12, "python3", f"{SERVICE}/app.slice/tracerworks-serve.service")
    # the workers descend from the tmux server, in the session that started it
    tmux = f"{USER}/session-313.scope"
    process(proc, 20, "tmux: server", tmux, age=3600, cpu=60)
    process(proc, 21, "bash", tmux, age=3600, cpu=1, parent=20)
    process(proc, 22, "claude", tmux, age=3600, cpu=3500, parent=21)
    process(proc, 23, "node", tmux, age=3600, cpu=3500, parent=22)

    assert found(proc) == []


def test_next_to_tmux_but_outside_it_is_still_a_stray(proc: Path) -> None:
    """The SSH session attached to tmux, and the one that started its server,
    hide nothing of what runs beside tmux."""
    tmux = f"{USER}/session-313.scope"
    process(proc, 20, "tmux: server", tmux, age=3600, cpu=60)
    process(proc, 30, "python3", tmux, age=1200, cpu=1200, parent=1)
    attached = f"{USER}/session-90001.scope"
    process(proc, 40, "bash", attached, age=1800, cpu=2)
    process(proc, 41, "tmux: client", attached, age=1800, cpu=3, parent=40)
    process(proc, 42, "python3", attached, age=1200, cpu=1100, parent=40)

    assert [stray.pid for stray in found(proc) or []] == [30, 42]


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("my) (x", "my) (x"),  # parentheses in the name: the last one closes it
        (b"py\xff3", "py\ufffd3"),  # not UTF-8: still read, never blind
    ],
)
def test_any_name_is_read(proc: Path, name: str | bytes, said: str) -> None:
    process(proc, 60, name, f"{USER}/session-1.scope", age=1200, cpu=1200)

    assert [stray.name for stray in found(proc) or []] == [said]


@pytest.mark.parametrize(
    ("age", "cpu", "why"),
    [
        (STRAY_AFTER - 60, STRAY_AFTER - 60, "a full core, not for long yet"),
        (3600, 3600 * (STRAY_ABOVE - 1) / 100, "long, but under half a core"),
        (3 * 3600, 2, "an SSH shell left open, asleep"),
    ],
)
def test_short_or_quiet_is_no_stray(
    proc: Path, age: float, cpu: float, why: str
) -> None:
    process(proc, 30, "bash", f"{USER}/session-90000.scope", age=age, cpu=cpu)

    assert found(proc) == [], why


def test_only_this_user_s_processes_are_read(proc: Path) -> None:
    """The sshd of a session sits in its scope as root: not ours to name."""
    process(proc, 40, "sshd-session", f"{USER}/session-86141.scope", age=3600,
            cpu=3600, uid=0)  # fmt: skip

    assert found(proc) == []


def test_a_process_gone_while_read_is_skipped(proc: Path) -> None:
    proc.joinpath("50").mkdir()  # exited between the listing and the read
    process(proc, 51, "python3", f"{USER}/session-1.scope", age=1200, cpu=1200)

    assert [s.pid for s in found(proc) or []] == [51]


def test_without_an_uptime_nothing_can_be_said(proc: Path) -> None:
    proc.joinpath("uptime").unlink()

    assert found(proc) is None
