"""`tracerworks serve` as a `systemd --user` service, installed by self-update (#812).

The unit is written from the project file; `systemctl` is only ever asked to
reload units and to RESTART a service that already runs (`try-restart`):
starting it the first time is the operator's `enable --now`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FakeRunner, recorded_settings
from tests.tracerworks.test_start_stop import make_env
from tracerworks.cli import EXIT_OK, run
from tracerworks.self_update import Installed
from tracerworks.service import install_service, unit_text

SERVICE = "tracerworks-serve-test"


def systemctl(fake: FakeRunner) -> FakeRunner:
    fake.answer("systemctl", "--user", "daemon-reload")
    fake.answer("systemctl", "--user", "try-restart", f"{SERVICE}.service")
    return fake


def settings_in(tmp_path: Path):  # type: ignore[no-untyped-def]
    return replace(
        recorded_settings(tmp_path),
        serve_service=SERVICE,
        systemd_dir=tmp_path / "systemd",
    )


def test_the_unit_runs_the_installed_launcher_and_restarts_on_failure(
    tmp_path: Path,
) -> None:
    text = unit_text(settings_in(tmp_path))
    assert f"ExecStart={tmp_path / 'bin' / 'tracerworks'} serve" in text
    assert "Restart=on-failure" in text
    # A refusal (no secret) is a configuration problem: no restart loop.
    assert "RestartPreventExitStatus=1" in text
    # A base newer than serve (exit 2, #2156) restarts it on the new code.
    prevented = next(x for x in text.splitlines() if x.startswith("RestartPrevent"))
    assert prevented.split("=", 1)[1].split() == ["1"]
    assert "MemoryMax=" in text  # the machine has no swap
    # two malloc arenas: a build thread's heap is not kept apart (#2195)
    assert "Environment=MALLOC_ARENA_MAX=2" in text
    # the page cache taken back before the ceiling, not the service swapped
    assert "MemoryHigh=160M" in text and "MemoryMax=200M" in text
    assert "WantedBy=default.target" in text


def test_a_new_unit_is_written_reloaded_and_the_running_service_restarted(
    tmp_path: Path,
) -> None:
    fake = systemctl(FakeRunner())
    settings = settings_in(tmp_path)
    assert install_service(fake, settings) is True
    unit = settings.systemd_dir / f"{SERVICE}.service"
    assert unit.read_text(encoding="utf-8") == unit_text(settings)
    assert [c.argv[2] for c in fake.ran("systemctl")] == [
        "daemon-reload",
        "try-restart",
    ]


def test_an_unchanged_unit_is_not_reloaded_but_the_new_code_is_picked_up(
    tmp_path: Path,
) -> None:
    fake = systemctl(FakeRunner())
    settings = settings_in(tmp_path)
    install_service(fake, settings)
    fake.calls.clear()
    assert install_service(fake, settings) is False
    assert [c.argv[2] for c in fake.ran("systemctl")] == ["try-restart"]


def test_self_update_installs_the_service_after_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tracerworks.cli as cli

    monkeypatch.setattr(
        cli, "self_update", lambda runner, settings: Installed("a" * 40, "b" * 40)
    )
    fake = systemctl(FakeRunner())
    env, out, _ = make_env(fake, tmp_path)
    env.settings = replace(
        env.settings, serve_service=SERVICE, systemd_dir=tmp_path / "systemd"
    )
    assert run(env, ["self-update"]) == EXIT_OK
    assert f"{SERVICE}.service" in out.getvalue()
    assert (tmp_path / "systemd" / f"{SERVICE}.service").exists()
