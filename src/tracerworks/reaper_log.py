"""The VPS cleaner's last pass, for the atelier (#1726).

`tools/container_reaper.py`, run by the health cron, ends each pass with one
line `<date> <time> container_reaper pass removed=… disk=…%` in its log, and
writes an `ALERTE` line for what it could not clean — as does the runners'
job-start hook (`ci_hook ALERTE`), which makes no pass line. The alerts shown
are those since the pass before the last. Only the end of the log is read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_TAIL_BYTES = 64 * 1024
_PASS = re.compile(r"^(\S+ \S+) container_reaper pass (.*)$")
_COUNT = re.compile(r"([a-z_]+)=([0-9]+)")
_ALERTE = re.compile(r"^\S+ \S+ (?:container_reaper|ci_hook) ALERTE (.*)$")


def last_pass(log: Path | None) -> dict[str, Any] | None:
    """The last pass: when, its counts, and its own alerts; None without one."""
    if log is None:
        return None
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    passes = [i for i, line in enumerate(lines) if _PASS.match(line)]
    if not passes:
        return None
    found = _PASS.match(lines[passes[-1]])
    assert found is not None
    since = passes[-2] + 1 if len(passes) > 1 else 0
    return {
        "at": found.group(1),
        "counts": {k: int(v) for k, v in _COUNT.findall(found.group(2))},
        "alerts": [
            alert.group(1)
            for line in lines[since:]
            if (alert := _ALERTE.match(line)) and not _PASS.match(line)
        ],
    }
