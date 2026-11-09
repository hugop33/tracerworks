"""The queue relauncher's next pass (#869): its hour, read from the timer.

`systemd --user` runs `start-next` on a timer; the dashboard says when it will
run again. The timer is monotonic (`OnUnitActiveSec`): its realtime property
is empty, but `list-timers --output=json` gives the next elapse in wall-clock
microseconds. Whatever cannot be read is None — never an invented hour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FakeRunner
from tracerworks.relauncher import cap_log, next_pass

TIMER = "relauncher.timer"
LIST = ("systemctl", "--user", "list-timers", TIMER, "--all", "--output=json")


def timers(stdout: str, returncode: int = 0) -> FakeRunner:
    fake = FakeRunner()
    fake.answer(*LIST, stdout=stdout, returncode=returncode)
    return fake


def test_the_next_pass_is_the_timers_next_elapse() -> None:
    row = {
        "next": 1790859949444590,
        "left": 1790859949444590,
        "last": 1790859649429092,
        "passed": 755816570891,
        "unit": TIMER,
        "activates": "relauncher.service",
    }
    assert next_pass(timers(json.dumps([row])), TIMER) == pytest.approx(
        1790859949.44459
    )


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("[]", 0),  # no such timer
        (json.dumps([{"unit": TIMER, "next": None}]), 0),  # stopped: nothing due
        (json.dumps([{"unit": TIMER, "next": 0}]), 0),
        (json.dumps([{"unit": "other.timer", "next": 1790859949444590}]), 0),
        (json.dumps([{"unit": TIMER, "next": "soon"}]), 0),
        (json.dumps({"unit": TIMER}), 0),
        ("not json", 0),
        ("Failed to connect to bus", 1),  # no user session
    ],
)
def test_an_unreadable_timer_has_no_hour(stdout: str, returncode: int) -> None:
    assert next_pass(timers(stdout, returncode), TIMER) is None


def test_a_missing_systemctl_has_no_hour() -> None:
    fake = FakeRunner()
    fake.answer(*LIST, returncode=127, stdout="systemctl: not found")
    assert next_pass(fake, TIMER) is None


# --- its journal, capped (#2288) --------------------------------------------
# `~/tracerworks-next.log` held 573 000 lines (27 MB) on 03/11: systemd appends
# every pass to it and nothing cut it. The relauncher caps it itself.


def test_past_the_cap_only_the_last_lines_stay(tmp_path: Path) -> None:
    log = tmp_path / "next.log"
    log.write_text("".join(f"line {i}\n" for i in range(1, 11)))

    assert cap_log(log, keep=4) == 6
    assert log.read_text() == "line 7\nline 8\nline 9\nline 10\n"


def test_under_the_cap_the_journal_is_left_alone(tmp_path: Path) -> None:
    log = tmp_path / "next.log"
    log.write_text("one\ntwo\n")
    before = log.stat().st_mtime_ns

    assert cap_log(log, keep=2) == 0
    assert log.read_text() == "one\ntwo\n" and log.stat().st_mtime_ns == before


def test_the_journal_is_cut_in_place_for_the_writer_that_holds_it(
    tmp_path: Path,
) -> None:
    # systemd opened it in append mode for this very pass: a new file moved over
    # it (`tail > tmp && mv`) would send the rest of the pass to a deleted inode.
    log = tmp_path / "next.log"
    log.write_text("old\n" * 5 + "kept\n")
    with log.open("a") as systemd:
        assert cap_log(log, keep=1) == 5
        systemd.write("--- next pass\n")

    assert log.read_text() == "kept\n--- next pass\n"


def test_an_absent_journal_is_no_error(tmp_path: Path) -> None:
    assert cap_log(tmp_path / "absent.log", keep=1) == 0
    assert not (tmp_path / "absent.log").exists()
