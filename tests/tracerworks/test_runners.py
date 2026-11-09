"""The runners that share the VPS, read as one (#641).

Two runners now take CI jobs on the machine. The memory floor that applies
while a CI runs must apply when EITHER is busy; and a runner named in the
project file but not registered yet (or retired) must not freeze every worker
as « unknown » while the other one answers.
"""

from __future__ import annotations

import json

from tracerworks.github import parse_runners

NAMES = "vps-1, vps-2"


def payload(*runners: tuple[str, str, bool]) -> str:
    return json.dumps(
        {
            "runners": [
                {"name": name, "status": status, "busy": busy}
                for name, status, busy in runners
            ]
        }
    )


def test_one_busy_runner_makes_the_machine_busy() -> None:
    status = parse_runners(
        payload(("vps-1", "online", False), ("vps-2", "online", True)), NAMES
    )
    assert (status.status, status.busy) == ("online", True)


def test_both_idle_is_idle() -> None:
    status = parse_runners(
        payload(("vps-1", "online", False), ("vps-2", "online", False)), NAMES
    )
    assert (status.status, status.busy) == ("online", False)


def test_a_runner_not_registered_yet_does_not_count() -> None:
    status = parse_runners(payload(("vps-1", "online", True)), NAMES)
    assert (status.status, status.busy) == ("online", True)


def test_no_named_runner_at_all_is_unknown() -> None:
    assert parse_runners(payload(("other", "online", False)), NAMES).status == "unknown"
    assert parse_runners("not json", NAMES).status == "unknown"


def test_an_offline_runner_does_not_hide_a_busy_one() -> None:
    status = parse_runners(
        payload(("vps-1", "offline", False), ("vps-2", "online", True)), NAMES
    )
    assert (status.status, status.busy) == ("online", True)


def test_a_single_name_reads_as_before() -> None:
    status = parse_runners(payload(("vps-1", "online", True)), "vps-1")
    assert (status.name, status.status, status.busy) == ("vps-1", "online", True)
