"""The spec's mode labels follow its chantier to the pull request (#1297).

The operator puts `ci-github` (or `ci-vps`) on a spec at launch; the primer
tells the worker to open its pull request with it, and `clean` puts it back
on the open pull request if the worker forgot — once: a pull request on
which anyone ever set or removed one of those labels is the operator's.
"""

from __future__ import annotations

import json

import pytest

from tests.tracerworks.fakes import FakeRunner
from tracerworks.carry import carry, spec_labels
from tracerworks.primer import primer
from tracerworks.process import CommandError

REPO = "owner/repo"
CARRIED = ("ci-hybrid", "ci-vps", "ci-github")
BRANCH = "agent/600-console"
SPEC = ("gh", "issue", "view", "600", "--repo", REPO, "--json", "labels")
PULL = ("gh", "pr", "list", "--repo", REPO, "--head", BRANCH, "--state", "open",
        "--json", "number,labels", "--limit", "1")  # fmt: skip
LABEL_EVENTS = (
    '.[] | select(.event == "labeled" or .event == "unlabeled") | .label.name'
)
EVENTS = (
    "gh",
    "api",
    "--paginate",
    f"repos/{REPO}/issues/77/events",
    "-q",
    LABEL_EVENTS,
)
EDIT = ("gh", "pr", "edit", "77", "--repo", REPO, "--add-label", "ci-github")


def labels(*names: str) -> list[dict[str, str]]:
    return [{"name": n} for n in names]


def world(
    spec: tuple[str, ...] = ("spec", "ci-github"),
    pull: tuple[str, ...] | None = ("in-progress",),
    events: list[dict[str, object]] | None = None,
) -> FakeRunner:
    fake = FakeRunner()
    fake.answer(*SPEC, stdout=json.dumps({"labels": labels(*spec)}))
    found = [] if pull is None else [{"number": 77, "labels": labels(*pull)}]
    fake.answer(*PULL, stdout=json.dumps(found))
    # What `gh api --paginate -q` prints: the label of each label event.
    names = [str(e["label"]["name"]) for e in events or []]  # type: ignore[index]
    fake.answer(*EVENTS, stdout="".join(f"{n}\n" for n in names))
    fake.answer(*EDIT)
    return fake


def test_the_spec_s_mode_label_is_read() -> None:
    assert spec_labels(world(), REPO, 600, CARRIED) == ["ci-github"]


def test_no_carried_label_configured_asks_nothing() -> None:
    fake = FakeRunner()
    assert spec_labels(fake, REPO, 600, ()) == []
    assert carry(fake, REPO, 600, BRANCH, ()) is None
    assert fake.calls == []


def test_a_missing_label_is_put_back_and_said() -> None:
    fake = world()
    said = carry(fake, REPO, 600, BRANCH, CARRIED)
    assert fake.ran(*EDIT)
    assert said is not None and "ci-github" in said and "#77" in said


def test_once_the_label_was_ever_on_the_pull_request_it_is_left_alone() -> None:
    # Set then removed by the operator (hybrid again): never put back.
    fake = world(events=[
        {"event": "labeled", "label": {"name": "ci-github"}},
        {"event": "unlabeled", "label": {"name": "ci-github"}},
    ])  # fmt: skip
    assert carry(fake, REPO, 600, BRANCH, CARRIED) is None
    assert not fake.ran("gh", "pr", "edit")


def test_another_mode_chosen_on_the_pull_request_is_left_alone() -> None:
    fake = world(pull=("ci-vps",), events=[
        {"event": "labeled", "label": {"name": "ci-vps"}},
    ])  # fmt: skip
    assert carry(fake, REPO, 600, BRANCH, CARRIED) is None
    assert not fake.ran("gh", "pr", "edit")


@pytest.mark.parametrize(
    ("spec", "pull"),
    [(("spec",), ("in-progress",)), (("ci-github",), ("ci-github",)),
     (("ci-github",), None)],
)  # fmt: skip
def test_nothing_to_carry(spec: tuple[str, ...], pull: tuple[str, ...] | None) -> None:
    fake = world(spec=spec, pull=pull)
    assert carry(fake, REPO, 600, BRANCH, CARRIED) is None
    assert not fake.ran("gh", "pr", "edit")


def test_an_unreadable_pull_request_is_loud() -> None:
    fake = world()
    fake.answer(*PULL, stdout='[{"labels": []}]')
    with pytest.raises(CommandError):
        carry(fake, REPO, 600, BRANCH, CARRIED)


def test_an_unreadable_spec_is_loud() -> None:
    fake = FakeRunner()
    fake.answer(*SPEC, returncode=1, stdout="HTTP 502")
    with pytest.raises(CommandError):
        carry(fake, REPO, 600, BRANCH, CARRIED)


def test_the_primer_says_which_label_the_pull_request_opens_with() -> None:
    text = primer(REPO, 600, "Spec — x", BRANCH, "claude", ["r"], resuming=False,
                  carried=["ci-github"])  # fmt: skip
    assert "--label ci-github" in text


def test_without_a_mode_label_the_primer_says_nothing_of_it() -> None:
    text = primer(REPO, 600, "Spec — x", BRANCH, "claude", ["r"], resuming=False)
    assert "--label" not in text
