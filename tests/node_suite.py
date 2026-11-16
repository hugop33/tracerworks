"""The verdict of a front suite run under `node --test`, read from its summary.

ONE CHECK FOR EVERY WRAPPER (spec #2484): each Python test that runs a node
suite used to assert an exact `"# pass 15" in out`. A case added to the
TypeScript suite, green under node, turned the CI red until someone raised the
number in Python (16 of the 74 reds fixed by a commit from 10/10 to 13/11).
And as a substring, `"# pass 1"` also passed on 10, 12 or 150 tests.

The number is now a FLOOR, read exactly from its own line: at least N passed,
none failed, both summary lines present. A suite that gains cases stays green;
one that loses cases below its floor, runs nothing or fails one turns red.
"""

from __future__ import annotations

import re

#: The TAP summary lines, at the start of a line (a subtest's are indented).
_PASS = re.compile(r"^# pass (\d+)$", re.MULTILINE)
_FAIL = re.compile(r"^# fail (\d+)$", re.MULTILINE)


def node_suite_problem(out: str, at_least: int) -> str | None:
    """What is wrong with a node suite's result, or None when it holds.

    `out` is the raw TAP of `node --test --test-reporter=tap`, or the summary
    the selection module's batch synthesises (`# pass N\\n# fail 0\\n`). The
    LAST summary line counts: node writes its summary at the end, after any
    diagnostic a test printed."""
    if at_least < 1:
        raise ValueError(f"a floor of {at_least}: a suite must run something")
    passed = _PASS.findall(out)
    failed = _FAIL.findall(out)
    if not passed:
        return "no `# pass` summary line"
    if not failed:
        return "no `# fail` summary line"
    if int(failed[-1]):
        return f"{int(failed[-1])} failed"
    count = int(passed[-1])
    if count == 0:
        return "0 passed: the suite ran nothing"
    if count < at_least:
        return f"{count} passed, at least {at_least} expected"
    return None


def assert_node_suite(out: str, at_least: int, suite: str = "") -> None:
    """Fails with the output when the suite passed fewer than `at_least`
    tests, failed one, or left no summary (`node_suite_problem`)."""
    problem = node_suite_problem(out, at_least)
    where = f"{suite}: " if suite else ""
    assert problem is None, f"{where}{problem}\n{out}"
