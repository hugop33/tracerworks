"""The floor of a front suite's result (spec #2484, #2485), against the pure check."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.node_suite import assert_node_suite, node_suite_problem


def _tap(passed: int, failed: int = 0) -> str:
    """The tail of `node --test --test-reporter=tap`, as node 24 writes it."""
    return (
        "TAP version 13\n"
        "# Subtest: a case\n"
        "ok 1 - a case\n"
        "  ---\n  duration_ms: 1.2\n  ...\n"
        f"1..{passed + failed}\n"
        f"# tests {passed + failed}\n"
        "# suites 0\n"
        f"# pass {passed}\n"
        f"# fail {failed}\n"
        "# cancelled 0\n"
        "# skipped 0\n"
        "# todo 0\n"
        "# duration_ms 42.1\n"
    )


def _batch(passed: int) -> str:
    """The summary the selection module's batch synthesises."""
    return f"# pass {passed}\n# fail 0\n"


@pytest.mark.parametrize("shape", [_tap, _batch], ids=["tap", "batch"])
@pytest.mark.parametrize(
    ("passed", "floor"),
    [(15, 15), (17, 15), (150, 15), (1, 1)],
    ids=["exactly", "more", "prefix-150-of-15", "floor-1"],
)
def test_a_suite_at_or_above_its_floor_holds(
    shape: Callable[[int], str], passed: int, floor: int
) -> None:
    assert node_suite_problem(shape(passed), floor) is None


@pytest.mark.parametrize("shape", [_tap, _batch], ids=["tap", "batch"])
@pytest.mark.parametrize(
    ("passed", "floor", "problem"),
    [
        (14, 15, "14 passed, at least 15 expected"),
        # Read exactly: the 1 of `# pass 1` is not the start of a 15.
        (1, 15, "1 passed, at least 15 expected"),
        (0, 15, "0 passed: the suite ran nothing"),
        (0, 1, "0 passed: the suite ran nothing"),
    ],
    ids=["fewer", "prefix-1-of-15", "nothing", "nothing-floor-1"],
)
def test_a_suite_below_its_floor_fails(
    shape: Callable[[int], str], passed: int, floor: int, problem: str
) -> None:
    assert node_suite_problem(shape(passed), floor) == problem


def test_one_failure_fails_even_above_the_floor() -> None:
    assert node_suite_problem(_tap(20, failed=1), 15) == "1 failed"


@pytest.mark.parametrize(
    ("out", "problem"),
    [
        ("# fail 0\n", "no `# pass` summary line"),
        ("# pass 15\n", "no `# fail` summary line"),
        ("", "no `# pass` summary line"),
        # A subtest's own line is indented: never the file's summary.
        ("    # pass 15\n    # fail 0\n", "no `# pass` summary line"),
    ],
    ids=["no-pass", "no-fail", "empty", "indented-only"],
)
def test_a_missing_summary_line_fails(out: str, problem: str) -> None:
    assert node_suite_problem(out, 1) == problem


def test_the_last_summary_counts() -> None:
    """A test that printed `# pass 99` before node's own summary."""
    out = "# pass 99\n" + _tap(3)
    assert node_suite_problem(out, 15) == "3 passed, at least 15 expected"


def test_a_floor_below_one_is_a_mistake() -> None:
    with pytest.raises(ValueError, match="must run something"):
        node_suite_problem(_tap(3), 0)


def test_the_assertion_names_the_suite_the_problem_and_the_output() -> None:
    with pytest.raises(AssertionError) as raised:
        assert_node_suite(_batch(14), 15, suite="kpi.test.ts")
    assert str(raised.value).startswith(
        "kpi.test.ts: 14 passed, at least 15 expected\n# pass 14"
    )
    assert_node_suite(_batch(16), 15)
