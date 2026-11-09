"""The queue relauncher's next pass (#869): when its `systemd --user` timer runs
`start-next` again.

The timer is monotonic (`OnUnitActiveSec`): `NextElapseUSecRealtime` stays
empty, while `list-timers --output=json` gives the next elapse in wall-clock
microseconds. Whatever cannot be read — no user session, no such timer, a
stopped one, an answer of another shape — is None: never an invented hour.

Its journal (`[paths] relauncher_log`) is systemd's: the unit appends every
pass to it, and nothing cut it — 573 000 lines (27 MB) on 03/11. Each
`start-next` caps it first to its last `RELAUNCHER_LOG_LINES` (#2288).
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from tracerworks.process import Runner


def next_pass(run: Runner, timer: str) -> float | None:
    """Epoch seconds of the timer's next elapse, None when unknown."""
    result = run(
        ["systemctl", "--user", "list-timers", timer, "--all", "--output=json"]
    )
    if not result.ok:
        return None
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("unit") != timer:
            continue
        due = row.get("next")
        if isinstance(due, int) and not isinstance(due, bool) and due > 0:
            return due / 1_000_000
    return None


#: About two weeks of passes at the pace of 03/11 (a pass a minute).
RELAUNCHER_LOG_LINES = 20_000


def cap_log(path: Path, *, keep: int) -> int:
    """Keep the last `keep` lines of `path`; how many lines went.

    Cut IN PLACE: systemd holds the file open in append mode for the pass
    running now, so its next lines still land at the new end — a file moved
    over it (`tail > tmp && mv`, `vps-health`'s way) would send them to the
    deleted one. A line written between the read and the cut is lost: at the
    start of `start-next`, nothing else writes. A journal that cannot be read
    or written is left as it is: its cap never stops a pass."""
    try:
        with path.open("r+b") as journal:
            total = 0
            last: deque[bytes] = deque(maxlen=keep)
            for line in journal:
                total += 1
                last.append(line)
            if total <= keep:
                return 0
            journal.seek(0)
            journal.writelines(last)
            journal.truncate()
    except OSError:
        return 0
    return total - keep
