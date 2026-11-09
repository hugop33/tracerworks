"""The GitHub minutes' safety net (#1296, spec #1294): above 90 % of the
month's included minutes, a repository variable sends `ci-github` CI to the
VPS; it goes when the month starts again. Minutes that cannot be read change
nothing — never an « exhausted » made up."""

from __future__ import annotations

import json

import pytest

from tests.tracerworks.fakes import FakeRunner
from tracerworks.budget import ABOVE, apply, low
from tracerworks.process import CommandError

REPO = "owner/repo"
VARIABLE = "CI_GITHUB_BUDGET_LOW"
LIST = ("gh", "variable", "list", "--repo", REPO, "--json", "name")
SET = ("gh", "variable", "set", VARIABLE, "--repo", REPO, "--body", "true")
DELETE = ("gh", "variable", "delete", VARIABLE, "--repo", REPO)


NOW = 1_790_000_000.0


def read(
    percent: float | None, error: str | None = None, resets_at: float = NOW + 3600
) -> dict[str, object]:
    return {"used_percent": percent, "error": error, "resets_at": resets_at}


def world(*names: str) -> FakeRunner:
    fake = FakeRunner()
    fake.answer(*LIST, stdout=json.dumps([{"name": n} for n in names]))
    fake.answer(*SET)
    fake.answer(*DELETE)
    return fake


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(None, None), (0.0, False), (89.9, False), (ABOVE, True), (104.0, True)],
)
def test_low_from_ninety_percent(percent: float | None, expected: bool | None) -> None:
    assert ABOVE == 90.0
    assert low(percent) is expected


def test_above_ninety_percent_the_variable_is_set_and_said() -> None:
    fake = world("RUNS_ON")
    said = apply(fake, REPO, VARIABLE, read(91.2), NOW)
    assert fake.ran(*SET)
    assert said is not None and "91" in said and VARIABLE in said


def test_set_once_while_it_stays_above() -> None:
    fake = world("RUNS_ON", VARIABLE)
    assert apply(fake, REPO, VARIABLE, read(95.0), NOW) is None
    assert not fake.ran(*SET)


def test_the_month_starting_again_removes_it_and_says_so() -> None:
    fake = world(VARIABLE)
    said = apply(fake, REPO, VARIABLE, read(2.0), NOW)
    assert fake.ran(*DELETE)
    assert said is not None and VARIABLE in said


def test_below_and_absent_nothing_is_done() -> None:
    fake = world("RUNS_ON")
    assert apply(fake, REPO, VARIABLE, read(40.0), NOW) is None
    assert not fake.ran(*DELETE)


@pytest.mark.parametrize(
    "unread",
    [read(None), read(95.0, "token absent"), read(95.0, resets_at=NOW - 60)],
)
def test_unreadable_minutes_change_nothing(unread: dict[str, object]) -> None:
    fake = FakeRunner()  # any command would be « unexpected »
    assert apply(fake, REPO, VARIABLE, unread, NOW) is None
    assert fake.calls == []


def test_unreadable_variables_are_an_error_not_a_switch() -> None:
    fake = FakeRunner()
    fake.answer(*LIST, returncode=1, stdout="HTTP 403")
    with pytest.raises(CommandError):
        apply(fake, REPO, VARIABLE, read(95.0), NOW)
    assert not fake.ran("gh", "variable", "set")
