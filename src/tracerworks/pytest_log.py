"""The tests a CI job's log names as failed — a module of its own (#1923).

Read by the recorder (the Qualité tab, #840) and by the rule of a machine's
red (`machine_red`, the merge queue, #1715, #1792). It lived in `recorder`,
which imports `snapshot`, which imports `merge_queue`: `merge_queue` reading
it back from `recorder` was a cycle, and `test_recorder.py` run alone could
not even be collected (25/10). It imports nothing of tracerworks's.
"""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STAMPED = re.compile(r"^\S+Z ")
#: A test's id: its path, its name, and its parameters WHOLE — spaces and
#: all (#2223: `[… new.org_id = '…' …]` was read as no test, 29 and 30/10).
_FAILED_TEST = re.compile(
    r"^(?:FAILED|ERROR) (\S+?\.py(?:::[^\s\[]+(?:\[.*?\])?)?)(?: - |\s*$)"
)


def failed_tests(log: str) -> set[str]:
    """The tests pytest's summary names as failed (`FAILED path::test - …`, or
    `ERROR path` for one that could not even run). A job that failed for
    another reason (lint, types, a cancelled tranche) names none."""
    found = set()
    for line in log.splitlines():
        line = _STAMPED.sub("", _ANSI.sub("", line))
        if match := _FAILED_TEST.match(line):
            found.add(match.group(1))
    return found
