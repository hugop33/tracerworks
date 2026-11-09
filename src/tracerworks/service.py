"""The dashboard as a `systemd --user` service, written by `self-update` (#812).

`self-update` swaps the installed copy; a running `serve` would keep the old
code. So after each install the unit is (re)written from the project file,
units are reloaded when it changed, and the service is restarted ONLY if it
already runs (`try-restart`). Enabling it the first time — once its secret
exists — is the operator's: `systemctl --user enable --now <service>`.
"""

from __future__ import annotations

from tracerworks.process import Runner, checked
from tracerworks.settings import Settings

#: The machine has no swap and also carries the workers and the runners: a
#: dashboard that grew past this is killed and restarted, not left to grow.
MEMORY_MAX = "200M"
#: Below it, where the kernel starts taking back the page cache (the history
#: base, the logs read) before it swaps the service's own memory (#2195: on
#: 01/11, 37 MB swapped out at the ceiling with 39 MB of cache in the cgroup).
MEMORY_HIGH = "160M"

UNIT = """# Written by `tracerworks self-update` — do not edit; rerun it to update.
[Unit]
Description=tracerworks serve — the factory dashboard, read only ({config})
After=network-online.target

[Service]
ExecStart={launcher} serve
Restart=on-failure
RestartSec=5
# Exit 1 is a refusal (no secret, a readable secret): fix it, then start.
# Exit 2 is a history base newer than this serve: restarted on the new code.
RestartPreventExitStatus=1
# Two malloc arenas, not one per thread (#2195): a build thread's heap is not
# kept apart at its own high-water mark for good.
Environment=MALLOC_ARENA_MAX=2
MemoryHigh={high}
MemoryMax={memory}

[Install]
WantedBy=default.target
"""


def unit_text(settings: Settings) -> str:
    return UNIT.format(
        config=settings.config_file,
        launcher=settings.bin_dir / "tracerworks",
        memory=MEMORY_MAX,
        high=MEMORY_HIGH,
    )


def install_service(runner: Runner, settings: Settings) -> bool:
    """Write the unit; True when it changed. The running service, if any,
    is restarted on the new code either way."""
    unit = settings.systemd_dir / f"{settings.serve_service}.service"
    text = unit_text(settings)
    try:
        changed = unit.read_text(encoding="utf-8") != text
    except FileNotFoundError:
        changed = True
    if changed:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(text, encoding="utf-8")
        checked(
            runner(["systemctl", "--user", "daemon-reload"]), "systemctl daemon-reload"
        )
    checked(
        runner(["systemctl", "--user", "try-restart", unit.name]),
        f"systemctl try-restart {unit.name}",
    )
    return changed
