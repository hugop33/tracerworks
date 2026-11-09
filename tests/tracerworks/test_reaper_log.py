"""The atelier shows the VPS cleaner's last pass (#1726).

`tools/container_reaper.py` ends each pass with one `container_reaper pass …`
line in its log, and writes an ALERTE line, with the same time, for what it
could not clean. The atelier reads the last pass and only its own alerts.
"""

from __future__ import annotations

from pathlib import Path

from tracerworks.reaper_log import last_pass

LOG = "".join(
    [
        "2026-10-23 20:15:01 container_reaper removing"
        " jovial_neumann postgres:17 owner_dead created=…\n",
        "2026-10-23 20:15:01 container_reaper ALERTE jovial_neumann refused to go\n",
        "2026-10-23 20:15:01 container_reaper pass removed=1"
        " failed=1 owner_dead=1 tmux_servers=0 processes=0"
        " worktrees=0 freed=0MB disk=91%\n",
        "2026-10-23 20:30:01 container_reaper stopped orphan"
        " gpg-agent pid=1307520 age=4244min\n",
        "2026-10-23 20:30:01 container_reaper ALERTE disk still at"
        " 91% after the pass\n",
        "2026-10-23 20:30:02 ci_hook ALERTE actions-runner-2: removed=0 failed=1\n",
        "2026-10-23 20:30:01 container_reaper pass removed=2"
        " failed=0 owner_dead=2 tmux_servers=8 processes=2"
        " worktrees=1 freed=1534MB disk=91%\n",
    ]
)


def test_the_last_pass_and_only_its_alerts(tmp_path: Path) -> None:
    log = tmp_path / "vps-reaper.log"
    log.write_text(LOG)

    assert last_pass(log) == {
        "at": "2026-10-23 20:30:01",
        "counts": {
            "removed": 2,
            "failed": 0,
            "owner_dead": 2,
            "tmux_servers": 8,
            "processes": 2,
            "worktrees": 1,
            "freed": 1534,
            "disk": 91,
        },
        "alerts": [
            "disk still at 91% after the pass",
            "actions-runner-2: removed=0 failed=1",
        ],
    }


def test_no_log_or_no_pass_yet_is_none(tmp_path: Path) -> None:
    assert last_pass(None) is None
    assert last_pass(tmp_path / "absent.log") is None
    (tmp_path / "empty.log").write_text(
        "2026-10-23 20:30:01 container_reaper removing x\n"
    )
    assert last_pass(tmp_path / "empty.log") is None
