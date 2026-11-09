"""The kernel's OOM kills (#1822), as the operator's session journal says them:
a systemd unit of the session (a runner, `serve`…) failed with `oom-kill`.
The kernel's own log is not readable by the session; this one is."""

from __future__ import annotations

import re

from tracerworks.process import Runner

#: `journalctl --user -o short-unix`: « 1792915189.123456 host systemd[71060]:
#: actions-runner-vps-1.service: Failed with result 'oom-kill'. »
_OOM = re.compile(
    r"^(\d+)(?:\.\d+)? \S+ systemd\[\d+\]: (\S+): Failed with result 'oom-kill'\.$"
)
#: A worker's scope is `OOMPolicy=continue` (#2095): the kernel kills its test,
#: the scope lives on and never fails — this line is all the journal says. A
#: worker's only (its unit's prefix, then `<spec>-<time>.scope`): another
#: scope's kill (a probe, a container) is no one's.
_WORKER_KILL = (
    r"^(\d+)(?:\.\d+)? \S+ systemd\[\d+\]: ({prefix}\d+-\d+\.scope):"
    r" The kernel OOM killer killed some processes in this unit\.$"
)
_GREP = "Failed with result 'oom-kill'|The kernel OOM killer killed some processes"
OOM_SINCE = 2 * 86400  # each reading looks back two days: a missed one is caught up


def rows(text: str, worker_unit: str) -> list[dict[str, object]]:
    """The units the OOM killer failed (#1822), from the session's journal,
    and the workers' scopes it killed in (#2095), read in their name. A
    worker's scope that failed too (one launched before its policy) is
    counted once, by its kill."""
    worker_kill = re.compile(_WORKER_KILL.replace("{prefix}", re.escape(worker_unit)))
    found: list[dict[str, object]] = []
    for line in text.splitlines():
        kill = worker_kill.match(line)
        failed = _OOM.match(line)
        if failed and failed.group(2).startswith(worker_unit):
            failed = None
        if m := kill or failed:
            found.append({"at": int(m.group(1)), "unit": m.group(2)})
    return found


def read(
    runner: Runner, now: float, worker_unit: str
) -> list[dict[str, object]] | None:
    """The journal's OOM kills of the last two days; None when unreadable.
    `journalctl -g` answers 1 when nothing matches: none, not a failure."""
    result = runner([
        "journalctl", "--user", "--no-pager", "-q", "-o", "short-unix",
        "--since", f"@{int(now - OOM_SINCE)}", "-g", _GREP,
    ])  # fmt: skip
    if result.ok or (result.returncode == 1 and not result.stdout.strip()):
        return rows(result.stdout, worker_unit)
    return None
