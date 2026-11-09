"""The merge queue, the green path (#1714, spec #1708), against a fake GitHub.

Seen from outside: what the queue does with a pull request — enters it or
not, tests it on the base of the moment, posts the status, merges — and what
the history base keeps across a restart. Never the order of its reads.
"""

from __future__ import annotations

import dataclasses
from contextlib import closing
from pathlib import Path

import pytest

from tests.tracerworks.fakes import (
    FIXTURES,
    PACKAGE,
    FakeRunner,
    recorded_settings,
    with_root,
)
from tests.tracerworks.git_world import Repos
from tests.tracerworks.queue_world import (
    COLORED_TEST_RED,
    DOCS,
    GREEN,
    MACHINE_RED,
    RED,
    RUNNING,
    TEST_RED,
    Check,
    Hub,
    Move,
    check,
    wire,
)
from tests.tracerworks.test_clean import clean_world, cleaned
from tests.tracerworks.test_clean import transcripts as transcripts  # the fixture
from tracerworks import low_memory, machine_red, merge_queue, store
from tracerworks.merge_queue import GitHub, advance, view
from tracerworks.process import CommandError, run_process
from tracerworks.settings import Settings


@pytest.fixture
def hub() -> Hub:
    return Hub()


def settings_of(tmp_path: Path) -> Settings:
    """`make_env`'s settings: the same history base as `clean`'s."""
    return with_root(recorded_settings(tmp_path), tmp_path / "wt")


def step(hub: Hub, tmp_path: Path) -> list[str]:
    """One pass of the relauncher — a fresh `tracerworks` each time, as the
    timer runs it: nothing but the history base carries over."""
    settings = settings_of(tmp_path)
    hub.clock += 30
    return advance(GitHub(wire(hub), settings), settings, hub.clock)


def moves(hub: Hub, kind: str) -> list[Move]:
    return [m for m in hub.moves if m[0] == kind]


# --- entering ---------------------------------------------------------------- #


@pytest.mark.parametrize("rollup", [RED, RUNNING, []], ids=["red", "running", "none"])
def test_a_pull_request_not_green_does_not_enter(
    hub: Hub, tmp_path: Path, rollup: list[Check]
) -> None:
    hub.open(7, rollup)
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert (found.testing, found.waiting) == (None, [])
    assert hub.moves == []


def test_a_pull_request_in_conflict_does_not_enter(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN).conflict_on_update = True
    step(hub, tmp_path)
    assert view(settings_of(tmp_path)).testing is None
    assert hub.moves == []


def test_a_ready_green_pull_request_enters_without_a_label(
    hub: Hub, tmp_path: Path
) -> None:
    """No label asks for the queue (#1801): it is the only path."""
    hub.open(7, GREEN)
    said = step(hub, tmp_path)
    assert hub.pulls[7].labels == {"merge-queue-testing"}, "tracerworks's alone"
    assert any("#7" in line for line in said)
    assert view(settings_of(tmp_path)).testing is not None


@pytest.mark.parametrize(
    "world",
    [{"draft": True}, {"author": "stranger"}, {"labels": {"hors-file"}}],
    ids=["draft", "untrusted-author", "hors-file"],
)
def test_never_the_queue_s(hub: Hub, tmp_path: Path, world: dict[str, object]) -> None:
    pr = hub.open(7, GREEN)
    for name, value in world.items():
        setattr(pr, name, value)
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert (found.testing, found.waiting) == (None, [])
    assert hub.moves == []


# --- testing, one at a time -------------------------------------------------- #


def test_one_pull_request_at_a_time_in_the_order_of_entry(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(9, GREEN)
    hub.push_to_main("m1", merging=3)
    step(hub, tmp_path)
    hub.open(8, GREEN, on="m1")  # enters later, lower number: still second
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 9
    assert [s.pull for s in found.waiting] == [8]
    assert {m[1] for m in hub.moves} == {9}, "only the head of the queue is touched"


def test_the_branch_is_updated_with_the_base_then_tested(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    hub.push_to_main("m1", merging=3)
    said = step(hub, tmp_path)
    # The testing label first — the CI reads it live — then GitHub's update.
    assert hub.moves[:2] == [("label", 7, True), ("update", 7, "m1")]
    assert any("branch updated" in line for line in said)
    pr = hub.pulls[7]
    assert "m1" in hub.ancestors(pr.head), "the tested head contains the base"
    assert "merge-queue-testing" in pr.labels
    said = step(hub, tmp_path)
    assert any("in test" in line for line in said)
    assert moves(hub, "merge") == []


def test_a_branch_up_to_date_has_its_head_run_rerun(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    assert hub.moves == [("label", 7, True), ("rerun", 7, "h7")]
    assert moves(hub, "update") == []
    step(hub, tmp_path)
    assert moves(hub, "merge") == [], "the light lane's green is not the test's"


# --- merging ------------------------------------------------------------------ #


def test_a_green_full_run_gives_the_status_then_the_merge(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    hub.push_to_main("m1", merging=3)
    step(hub, tmp_path)
    tested = hub.pulls[7].head
    hub.finish(tested)
    said = step(hub, tmp_path)
    assert hub.moves[-2:] == [
        ("status", 7, "success", "merge-queue"),
        ("merge", 7, tested),
    ]
    assert any("merged" in line for line in said)
    found = view(settings_of(tmp_path))
    assert found.testing is None and found.last_out is not None
    assert (found.last_out.pull, found.last_out.outcome) == (7, "merged")


def test_the_next_pull_request_is_taken_in_the_same_pass(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    hub.open(8, GREEN)
    step(hub, tmp_path)
    hub.finish(hub.pulls[7].head)
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, "h7")]
    # #8 is behind the base #7's merge moved: updated, under test.
    assert hub.moves[-2:] == [("label", 8, True), ("update", 8, "merge-7")]


def test_a_red_full_run_leaves_the_queue_with_its_label_off(
    hub: Hub, tmp_path: Path
) -> None:
    """The worker's resume is #1715; here the queue must not stay stuck."""
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.finish("h7", "failure")
    step(hub, tmp_path)
    assert moves(hub, "merge") == []
    assert "merge-queue-testing" not in hub.pulls[7].labels
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"
    assert last.reason is not None and "red" in last.reason


# --- the base moving under the test ----------------------------------------- #


def test_the_base_moved_by_code_runs_the_test_again(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)  # up to date: rerun
    hub.push_to_main("m1", merging=3)
    hub.rollups[3] = GREEN  # its CI ran everything: code
    hub.finish("h7")
    said = step(hub, tmp_path)
    assert moves(hub, "merge") == []
    assert moves(hub, "update") == [("update", 7, "m1")]
    assert any("moved by code" in line for line in said)
    hub.finish(hub.pulls[7].head)
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, "h7+m1")]


def test_the_base_moved_by_documentation_merges_without_a_new_test(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.push_to_main("m1", merging=3)
    hub.rollups[3] = DOCS  # its CI ran nothing
    hub.finish("h7")
    step(hub, tmp_path)
    assert moves(hub, "update") == []
    assert moves(hub, "merge") == [("merge", 7, "h7")]


def test_a_push_straight_to_the_base_is_code(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.push_to_main("m1")  # no pull request merged it
    hub.finish("h7")
    step(hub, tmp_path)
    assert moves(hub, "update") == [("update", 7, "m1")]


# --- documentation only ------------------------------------------------------- #


def test_documentation_only_merges_on_its_turn_without_a_suite(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, DOCS)
    hub.push_to_main("m1", merging=3)
    step(hub, tmp_path)
    assert hub.moves == [
        ("status", 7, "success", "merge-queue"),
        ("merge", 7, "h7"),
    ]
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "merged"


#: #1902's rollup (27/10): the run of its draft (40487679352) skipped
#: everything, the next one (40488288722) ran `checks` green.
PR_1902_CHECKS = [
    {
        "__typename": "CheckRun",
        "name": "checks (lint, types, api client, auth, front)",
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": started,
        "detailsUrl": f"https://github.com/acme/widgets/actions/runs/{run}/job/{job}",
    }
    for conclusion, started, run, job in (
        ("SKIPPED", "2026-10-27T10:20:33Z", 40487679352, 122133522701),
        ("SUCCESS", "2026-10-27T10:31:00Z", 40488288722, 122135666138),
    )
]
CHECKS = "checks ("


@pytest.mark.parametrize("order", [1, -1], ids=["as-listed", "reversed"])
def test_code_after_a_draft_run_all_skipped_is_never_documentation(order: int) -> None:
    """#1902: code read as documentation — the draft's run skipped `checks`,
    the next one ran it — and merged without its full suite. Only the
    head's latest run says « nothing ran »."""
    assert not merge_queue.ran_nothing(PR_1902_CHECKS[::order], CHECKS)
    assert merge_queue.ran_nothing(PR_1902_CHECKS[:1], CHECKS)  # a real doc
    # and the other way round: code first, then a documentation-only push
    flipped = [
        {**PR_1902_CHECKS[0], "conclusion": "SUCCESS"},
        {**PR_1902_CHECKS[1], "conclusion": "SKIPPED"},
    ]
    assert merge_queue.ran_nothing(flipped[::order], CHECKS)
    assert not merge_queue.ran_nothing([], CHECKS)


def test_code_after_a_draft_run_all_skipped_gets_its_full_suite(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, [check("changes"), *PR_1902_CHECKS])
    hub.push_to_main("m1", merging=3)
    step(hub, tmp_path)
    assert moves(hub, "merge") == []
    assert ("label", 7, True) in hub.moves, "in test: its full suite"


def test_documentation_only_never_waits_behind_a_test(hub: Hub, tmp_path: Path) -> None:
    """Story 15: a doc merges while #7 runs its suite — and #7 then merges
    without a new test, the base having moved by documentation only."""
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.open(8, DOCS)
    hub.rollups[8] = DOCS
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 8, "h8")]
    hub.merged_by[hub.main] = 8
    hub.finish("h7")
    step(hub, tmp_path)
    assert moves(hub, "merge")[-1] == ("merge", 7, "h7")
    assert moves(hub, "update") == []


def test_documentation_whose_merge_fails_never_ends_the_pass(
    hub: Hub, tmp_path: Path
) -> None:
    """#2156: a doc's merge refused leaves; one that errs waits its passes —
    the pull request behind it still starts in the same pass."""
    hub.open(5, DOCS).unmergeable.add("h5")
    hub.open(6, DOCS)
    hub.open(7, GREEN)
    step(hub, tmp_path)
    assert ("merge-refused", 5) in hub.moves
    assert moves(hub, "merge") == [("merge", 6, "h6")]
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7
    out = merge_queue.stays(settings_of(tmp_path), open_only=False)
    assert [(s.pull, s.reason) for s in out if s.outcome == "sent_back"] == [
        (5, "a conflict with the base branch")
    ]
    hub.open(8, DOCS)
    hub.merge_error = "gh: HTTP 502"
    said = step(hub, tmp_path)
    assert any("#8 merge failed (1/3), tried again" in line for line in said)


# --- the night ----------------------------------------------------------------- #


def test_no_full_suite_starts_during_the_night(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN)
    hub.night = True
    said = step(hub, tmp_path)
    assert hub.moves == []
    assert any("night" in line for line in said)
    hub.night = False
    step(hub, tmp_path)
    assert moves(hub, "rerun") == [("rerun", 7, "h7")]


def test_documentation_only_merges_during_the_night(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, DOCS)
    hub.night = True
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, "h7")]


# --- leaving, surviving a restart ------------------------------------------- #


def test_a_closed_pull_request_leaves(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN)
    hub.open(8, GREEN)
    step(hub, tmp_path)
    hub.pulls[8].open = False
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert found.waiting == []
    assert found.last_out is not None and found.last_out.outcome == "gone"


def test_the_state_survives_a_restart(hub: Hub, tmp_path: Path) -> None:
    """Each `step` is a new `tracerworks`; here the machine restarts mid-test:
    no second update, no second rerun — the test goes on to the merge."""
    hub.open(7, GREEN)
    hub.push_to_main("m1", merging=3)
    step(hub, tmp_path)
    hub.open(8, GREEN, on="m1")
    step(hub, tmp_path)
    moved = list(hub.moves)
    restarted = dataclasses.replace(settings_of(tmp_path))
    found = view(restarted)
    assert found.testing is not None and found.testing.pull == 7
    assert [s.pull for s in found.waiting] == [8]
    step(hub, tmp_path)
    assert hub.moves == moved, "nothing done twice"
    hub.finish(hub.pulls[7].head)
    step(hub, tmp_path)
    assert ("merge", 7, "h7+m1") in hub.moves


def test_the_view_reads_the_base_only(hub: Hub, tmp_path: Path) -> None:
    """`status` and the workshop never call GitHub for the queue."""
    hub.open(7, GREEN)
    step(hub, tmp_path)
    assert merge_queue.view(settings_of(tmp_path)).testing is not None


# --- what is shown, and the relauncher's pass -------------------------------- #


def test_status_shows_the_one_in_test_those_waiting_and_the_last_exit(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(5, DOCS)
    step(hub, tmp_path)  # merged: the last exit
    hub.open(7, GREEN)
    hub.open(8, GREEN)
    step(hub, tmp_path)
    lines = merge_queue.describe(view(settings_of(tmp_path)), hub.clock + 600)
    assert lines[0].startswith("merge queue: #7 in test since 1")
    assert lines[0].endswith("its run not created yet"), "updated: no run yet"
    assert lines[1] == "merge queue: waiting #8"
    assert lines[2].startswith("merge queue: last out #5, merged — documentation only")
    shown = merge_queue.as_json(view(settings_of(tmp_path)))
    assert shown is not None and shown["testing"]["pull"] == 7
    assert [s["pull"] for s in shown["waiting"]] == [8]


def test_an_unreadable_base_is_said_never_an_empty_queue() -> None:
    assert merge_queue.describe(None, 0) == [
        "merge queue: unknown (the history base cannot be read)"
    ]
    assert merge_queue.as_json(None) is None


def test_clean_moves_the_queue(hub: Hub, tmp_path: Path, transcripts: Path) -> None:
    """The relauncher's `clean` is the queue's pass: no service of its own."""
    fake = clean_world(tmp_path)
    for prefix in (
        ("gh", "pr", "list", "--repo", "owner/repo", "--state", "open", "--label"),
        ("gh", "pr", "list", "--repo", "owner/repo", "--state", "open", "--limit"),
        ("gh", "api", "-X", "POST"),
        ("gh", "pr", "merge"),
    ):
        fake.on(*prefix, prefix=True)(hub)
    hub.open(7, DOCS)
    code, _, out, _ = cleaned(tmp_path, fake)
    assert code == 0
    assert "merge queue: #7 merged — documentation only" in out
    assert moves(hub, "merge") == [("merge", 7, "h7")]
    _, _, again, _ = cleaned(tmp_path, fake, "--dry-run")
    assert "merge queue" not in again, "a dry run moves nothing"


# --- the red paths (#1715) ---------------------------------------------------- #


def in_full_test(hub: Hub, tmp_path: Path, number: int = 7) -> str:
    """`number` in test on the base of the moment; its tested head."""
    hub.open(number, GREEN)
    hub.push_to_main(f"m-{number}", merging=3)
    step(hub, tmp_path)
    return hub.pulls[number].head


def test_a_test_s_red_leaves_and_the_next_one_is_taken_at_once(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.open(8, GREEN, on=hub.main)
    step(hub, tmp_path)  # #8 enters, waits
    hub.finish(tested, "failure")  # a failed test in its log
    said = step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == [], "a test's red is never rerun blind"
    assert "merge-queue-testing" not in hub.pulls[7].labels
    assert moves(hub, "merge") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "sent_back")
    assert last.reason is not None and "integration (1/2)" in last.reason
    # the next one in the same pass: up to date with the base, its run rerun
    assert hub.moves[-2:] == [("label", 8, True), ("rerun", 8, "h8")]
    assert any("#7 left" in line for line in said)
    # what the resumed worker is told (the resume itself: test_parking)
    note = merge_queue.resume_note(settings_of(tmp_path), 7)
    assert "sortie de la file de fusion" in note and "integration (1/2)" in note
    assert merge_queue.resume_note(settings_of(tmp_path), 8) == ""


@pytest.mark.parametrize("log", [MACHINE_RED, None], ids=["lost runner", "no log"])
def test_a_machine_s_red_is_rerun_once_without_the_worker(
    hub: Hub, tmp_path: Path, log: str | None
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=log)
    if log is None:  # no log: waited for, then a machine's red (#1919)
        said = step(hub, tmp_path)
        assert moves(hub, "rerun-failed") == []
        assert any("log not given by GitHub yet" in line for line in said)
        hub.clock += machine_red.LOG_WAIT_S
    said = step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == [("rerun-failed", 7, tested)]
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.machine_rerun_at is not None
    assert "merge-queue-testing" in hub.pulls[7].labels, "still in test"
    assert merge_queue.resume_note(settings_of(tmp_path), 7) == ""
    assert any("machine" in line and "rerun once" in line for line in said)
    hub.finish(tested)  # green after the rerun: merged
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, tested)]


def test_a_machine_s_red_waits_for_memory_still_in_test(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2096: under the memory's alert threshold, the queue's one re-run
    waits — neither done nor spent — and goes once the memory is back."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=MACHINE_RED)
    monkeypatch.setattr(low_memory, "short", lambda meminfo: 1005)
    said = step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.machine_rerun_at is None
    assert "merge-queue-testing" in hub.pulls[7].labels, "still in test"
    assert any(
        "machine red, rerun waits for memory (1005 MB available)" in line
        for line in said
    )
    monkeypatch.undo()
    step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == [("rerun-failed", 7, tested)]


def test_a_test_s_red_whose_log_comes_late_is_never_rerun(
    hub: Hub, tmp_path: Path
) -> None:
    """#1886: the full suite red at 05:06:56 on a test (`ERROR …`), read
    before GitHub gave the log, rerun at 05:08 as a machine's. Now: waited
    for, read at the next pass, a test's red — out of the queue."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=TEST_RED, log_after=90)
    said = step(hub, tmp_path)  # 30 s after: no log yet
    assert moves(hub, "rerun-failed") == []
    assert "merge-queue-testing" in hub.pulls[7].labels, "still in test"
    assert any("log not given by GitHub yet" in line for line in said)
    hub.clock += 60
    step(hub, tmp_path)  # given now
    assert moves(hub, "rerun-failed") == [], "a test's red is never rerun blind"
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "sent_back")


def test_a_coloured_test_s_red_goes_back_to_its_worker_at_once(
    hub: Hub, tmp_path: Path
) -> None:
    """#1973 (28/10): `gh` 2.101 refused the coloured log, the 5 min passed,
    and the queue reran a test that named itself red. Read with the flag: out
    of the queue at the first pass, never rerun."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=COLORED_TEST_RED)
    said = step(hub, tmp_path)
    assert not any("log not given by GitHub yet" in line for line in said)
    assert moves(hub, "rerun-failed") == [], "a test's red is never rerun blind"
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "sent_back")


def test_a_machine_s_red_twice_goes_back_as_a_test_s_red(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=MACHINE_RED)
    step(hub, tmp_path)
    step(hub, tmp_path)  # the rerun runs
    hub.finish(tested, "failure", log=MACHINE_RED)
    step(hub, tmp_path)
    assert len(moves(hub, "rerun-failed")) == 1, "one rerun, never two"
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"
    assert last.reason is not None and "again after one rerun" in last.reason
    assert "merge-queue-testing" not in hub.pulls[7].labels


def test_a_conflict_at_the_update_leaves_with_nothing_pushed(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    hub.open(8, GREEN)
    hub.push_to_main("m1", merging=3)
    step(hub, tmp_path)
    hub.pulls[8].conflict_on_update = True  # main moves into #8's lines
    hub.pulls[8].stale_mergeable = True  # GitHub has not seen it yet
    hub.finish(hub.pulls[7].head)
    step(hub, tmp_path)  # #7 merged; #8 taken: its update refused
    assert ("update-refused", 8) in hub.moves
    assert hub.pulls[8].head == "h8", "nothing pushed"
    assert "merge-queue-testing" not in hub.pulls[8].labels
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (8, "sent_back")
    assert "conflict" in merge_queue.resume_note(settings_of(tmp_path), 8)


def test_a_conflict_after_the_test_leaves_nothing_merged(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.push_to_main("m2", merging=4)
    hub.rollups[4] = GREEN  # code: a new update is needed…
    hub.pulls[7].conflict_on_update = True  # …and it conflicts
    hub.pulls[7].stale_mergeable = True
    hub.finish(tested)
    step(hub, tmp_path)
    assert moves(hub, "merge") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.reason == "a conflict with the base branch"


# --- a refused merge never freezes the queue (#2156, spec #2155) ------------ #


def green_in_test_main_moved_by_nothing(hub: Hub, tmp_path: Path) -> None:
    """The 30/10 (#2154): #7 in test on the base, #3 — factory only, its CI
    ran nothing — merges meanwhile into #7's file; #7's suite ends green."""
    hub.open(7, GREEN)
    step(hub, tmp_path)  # up to date: its head run rerun
    hub.push_to_main("m1", merging=3)
    hub.rollups[3] = DOCS
    hub.pulls[7].unmergeable.add("h7")
    hub.finish("h7")


def test_a_refused_merge_takes_the_update_path_still_in_test(
    hub: Hub, tmp_path: Path
) -> None:
    green_in_test_main_moved_by_nothing(hub, tmp_path)
    said = step(hub, tmp_path)
    assert moves(hub, "merge-refused") == [("merge-refused", 7)]
    assert moves(hub, "update") == [("update", 7, "m1")]
    assert any("not mergeable" in line for line in said)
    step(hub, tmp_path)  # the update pushed: in test on the new head
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7
    assert found.testing.tested_head == "h7+m1"
    assert "merge-queue-testing" in hub.pulls[7].labels
    step(hub, tmp_path)
    assert len(moves(hub, "merge-refused")) == 1, "never the same call again"
    hub.finish("h7+m1")
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, "h7+m1")]


def test_a_refused_merge_whose_update_conflicts_goes_back_and_the_next_starts(
    hub: Hub, tmp_path: Path
) -> None:
    green_in_test_main_moved_by_nothing(hub, tmp_path)
    hub.open(8, GREEN, on="m1")
    hub.pulls[7].conflict_on_update = True
    hub.pulls[7].stale_mergeable = True  # GitHub listed it MERGEABLE
    step(hub, tmp_path)
    assert ("update-refused", 7) in hub.moves
    assert moves(hub, "merge") == []
    assert "merge-queue-testing" not in hub.pulls[7].labels
    found = view(settings_of(tmp_path))
    assert found.last_out is not None
    assert (found.last_out.pull, found.last_out.outcome) == (7, "sent_back")
    assert "conflict" in merge_queue.resume_note(settings_of(tmp_path), 7)
    assert found.testing is not None and found.testing.pull == 8, "the next one"


def test_a_merge_listed_conflicting_takes_the_update_path_without_merging(
    hub: Hub, tmp_path: Path
) -> None:
    """GitHub already says CONFLICTING: no merge is even tried."""
    green_in_test_main_moved_by_nothing(hub, tmp_path)
    hub.pulls[7].conflict_on_update = True  # listed CONFLICTING
    step(hub, tmp_path)
    assert moves(hub, "merge-refused") == []
    assert ("update-refused", 7) in hub.moves
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "sent_back")


def test_another_merge_error_leaves_after_its_passes_with_its_reason(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.finish("h7")
    hub.merge_error = "gh: Resource not accessible by integration (HTTP 403)"
    for _ in range(2):
        said = step(hub, tmp_path)
        assert view(settings_of(tmp_path)).testing is not None, "still in test"
        assert any("HTTP 403" in line for line in said)
    said = step(hub, tmp_path)
    assert len(moves(hub, "merge-failed")) == 3
    assert view(settings_of(tmp_path)).testing is None
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"
    assert last.reason is not None and "HTTP 403" in last.reason
    assert "merge-queue-testing" not in hub.pulls[7].labels
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        failed = merge_queue.merge_failures(
            db, settings_of(tmp_path).github_repo, hub.clock
        )
    assert [(pull, "HTTP 403" in why) for pull, why in failed] == [(7, True)]


def test_a_branch_policy_refusal_is_a_merge_error_never_an_update(
    hub: Hub, tmp_path: Path
) -> None:
    """« Not mergeable » for a policy: an update would not help it."""
    green_in_test_main_moved_by_nothing(hub, tmp_path)
    hub.pulls[7].unmergeable.clear()
    hub.merge_error = (
        "X Pull request owner/repo#7 is not mergeable: the base branch policy"
        " prohibits the merge."
    )
    said = step(hub, tmp_path)
    assert moves(hub, "update") == []
    assert any("merge failed (1/3)" in line and "policy" in line for line in said)


def test_a_new_head_counts_its_merge_errors_from_zero(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.finish("h7")
    hub.merge_error = "gh: HTTP 502"
    step(hub, tmp_path)
    step(hub, tmp_path)  # 2/3 on h7
    hub.push(7, "h7b")
    step(hub, tmp_path)  # its run under the testing label: the test
    hub.finish("h7b")
    said = step(hub, tmp_path)
    assert any("merge failed (1/3)" in line for line in said)
    assert view(settings_of(tmp_path)).testing is not None


def test_the_merge_error_passes_are_read_from_the_project_file(
    hub: Hub, tmp_path: Path
) -> None:
    settings = settings_of(tmp_path)
    once = dataclasses.replace(
        settings,
        merge_queue=dataclasses.replace(settings.merge_queue, merge_error_passes=1),
    )
    hub.open(7, GREEN)
    advance(GitHub(wire(hub), once), once, hub.clock)
    hub.finish("h7")
    hub.merge_error = "gh: HTTP 502"
    hub.clock += 30
    advance(GitHub(wire(hub), once), once, hub.clock)
    last = view(once).last_out
    assert last is not None and last.outcome == "sent_back"


def test_a_merge_error_that_passes_merges_and_alerts_nobody(
    hub: Hub, tmp_path: Path
) -> None:
    """The counter-proof: one error, then GitHub accepts — merged as today."""
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.finish("h7")
    hub.merge_error = "gh: HTTP 502"
    step(hub, tmp_path)
    hub.merge_error = None
    step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, "h7")]
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "merged"
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        assert (
            merge_queue.merge_failures(db, settings_of(tmp_path).github_repo, hub.clock)
            == []
        )


def test_auto_merge_on_a_pull_request_of_the_queue_is_taken_off(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, RUNNING).auto_merge = True
    said = step(hub, tmp_path)
    assert [m[:2] for m in hub.moves] == [
        ("auto-off", 7),
        ("auto-read", 7),
        ("comment", 7),
    ]
    assert any("auto-merge taken off" in line for line in said)
    step(hub, tmp_path)
    assert moves(hub, "auto-off") == [("auto-off", 7)], "said once: it is off"


def test_back_in_the_queue_after_a_green_light_lane(hub: Hub, tmp_path: Path) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure")
    step(hub, tmp_path)
    pr = hub.pulls[7]
    pr.head, pr.rollup = "h7-fix", GREEN  # the worker's fix, its light lane green
    hub.parents["h7-fix"] = [tested]
    hub.runs["h7-fix"] = [hub._run("h7-fix", "completed", "success")]
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7
    assert len(merge_queue.stays(settings_of(tmp_path), open_only=False)) == 2


CHAIN_RED = (
    "2026-10-24T10:00:00Z FAILED tests/test_migration_chain.py::"
    "test_each_revision_is_held_by_one_file - AssertionError: 145 is in 2 files:"
    " 145_a.py, 145_b.py — renuméroter celle de la PR en 146 sur 145\n"
    "2026-10-24T10:00:01Z ##[error]Process completed with exit code 1.\n"
)


def test_a_migration_taken_twice_goes_back_to_its_worker_nothing_merged(
    hub: Hub, tmp_path: Path
) -> None:
    """Registre 23/10: 145 in #1690, #1698 and #1682. Brought up to date with
    `main`, the second has two files of one revision: the chain's unit test is
    its red — a test's red, never rerun as a machine's — and its words, in the
    log the resume hands over (`test_parking`), the number to take."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=CHAIN_RED)
    step(hub, tmp_path)
    assert moves(hub, "merge") == [] and moves(hub, "rerun-failed") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"


# --- the workers go through the queue (#1716) -------------------------------- #


def test_the_workshop_shows_the_queue_never_a_label_of_it() -> None:
    page = (PACKAGE / "dashboard.html").read_text()
    assert "MERGE QUEUE" in page  # the queue's state is shown
    assert "merge-queue" not in page and "hors-file" not in page


def test_no_run_on_its_head_skips_it_without_the_label(
    hub: Hub, tmp_path: Path
) -> None:
    """Review: a head with no run raised, label on, on every pass, and held
    every pull request behind it."""
    hub.open(7, GREEN)
    hub.runs["h7"] = []
    hub.open(8, GREEN)
    said = step(hub, tmp_path)
    assert ("label", 7, True) not in hub.moves
    assert any("#7 skipped" in line for line in said)
    assert hub.moves == [("label", 8, True), ("rerun", 8, "h8")]


def test_a_failed_start_takes_the_label_off(hub: Hub, tmp_path: Path) -> None:
    from tracerworks.process import CommandError

    hub.open(7, GREEN)
    hub.push_to_main("m1", merging=3)
    fake = wire(hub)
    fake.answer("gh", "api", "-X", "PUT", prefix=True, returncode=1, stdout="HTTP 502")
    settings = settings_of(tmp_path)
    with pytest.raises(CommandError):
        advance(GitHub(fake, settings), settings, hub.clock)
    assert hub.moves == [("label", 7, True), ("label", 7, False)]
    assert view(settings).testing is None


def test_an_update_github_never_pushes_leaves_after_a_while(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    hub.push_to_main("m1", merging=3)
    fake = wire(hub)
    fake.answer("gh", "api", "-X", "PUT", prefix=True, stdout="{}")  # 202, no push
    settings = settings_of(tmp_path)
    advance(GitHub(fake, settings), settings, hub.clock)
    assert view(settings).testing is not None
    hub.clock += merge_queue.UPDATE_TIMEOUT_S + 60
    advance(GitHub(fake, settings), settings, hub.clock)
    last = view(settings).last_out
    assert last is not None and last.reason == "GitHub never pushed the branch's update"
    assert "merge-queue-testing" not in hub.pulls[7].labels


def test_in_test_says_which_pull_request_the_queue_reads_first(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    assert merge_queue.in_test(settings_of(tmp_path), 7)
    assert not merge_queue.in_test(settings_of(tmp_path), 8)


# --- the queue is the only path: every auto-merge off (#1783, #1801) ------- #


def auto_moves(hub: Hub) -> list[Move]:
    kinds = {"auto-off", "auto-read", "comment"}
    return [m[:2] for m in hub.moves if m[0] in kinds]


@pytest.mark.parametrize(
    "world",
    [{}, {"draft": True}, {"author": "stranger"}],
    ids=["ready", "draft", "untrusted-author"],
)
def test_an_auto_merge_comes_off_read_back_and_said_once(
    hub: Hub, tmp_path: Path, world: dict[str, object]
) -> None:
    """Whoever opened it, a green light lane must never be enough to merge."""
    pr = hub.open(7, RUNNING)
    pr.auto_merge = True
    for name, value in world.items():
        setattr(pr, name, value)
    said = step(hub, tmp_path)
    assert auto_moves(hub) == [("auto-off", 7), ("auto-read", 7), ("comment", 7)]
    assert not hub.pulls[7].auto_merge and hub.pulls[7].labels == set()
    [comment] = [m[2] for m in hub.moves if m[0] == "comment"]
    assert "file de fusion" in str(comment) and "`hors-file`" in str(comment)
    assert any("#7 — auto-merge taken off" in line for line in said)
    # Set again later: off again, never a second comment.
    hub.pulls[7].auto_merge = True
    step(hub, tmp_path)
    assert [m[0] for m in auto_moves(hub)].count("comment") == 1
    assert not hub.pulls[7].auto_merge


def test_without_auto_merge_nothing_is_said(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, RUNNING)
    step(hub, tmp_path)
    assert auto_moves(hub) == []


def test_hors_file_keeps_its_auto_merge_and_the_path_of_before(
    hub: Hub, tmp_path: Path
) -> None:
    """The one exception (#1801): an emergency merges on its own, once green."""
    pr = hub.open(7, GREEN)
    pr.auto_merge, pr.labels = True, {"hors-file"}
    step(hub, tmp_path)
    assert hub.moves == [] and hub.pulls[7].auto_merge
    assert view(settings_of(tmp_path)).waiting == []


@pytest.mark.parametrize(
    "world",
    [{"auto_merge_sticks": True}, {"auto_merge_off_fails": True}],
    ids=["read-back-still-set", "removal-fails"],
)
def test_an_auto_merge_that_does_not_come_off_keeps_it_out_and_status_says_it(
    hub: Hub, tmp_path: Path, world: dict[str, bool]
) -> None:
    pr = hub.open(7, GREEN)
    pr.auto_merge = True
    for name, value in world.items():
        setattr(pr, name, value)
    said = step(hub, tmp_path)
    assert not moves(hub, "comment") and not moves(hub, "label")
    assert view(settings_of(tmp_path)).waiting == [], "never tested: kept out"
    assert any("#7 — its auto-merge would not come off" in line for line in said)
    found = view(settings_of(tmp_path))
    assert [pull for pull, _, _ in found.refused] == [7]
    lines = merge_queue.describe(found, hub.clock)
    assert any("#7 out of the queue" in line for line in lines)
    assert merge_queue.as_json(found)["refused"][0]["pull"] == 7  # type: ignore[index]
    # Said every pass, written once; once it comes off, no longer said, in.
    step(hub, tmp_path)
    hub.pulls[7].auto_merge_sticks = hub.pulls[7].auto_merge_off_fails = False
    step(hub, tmp_path)
    assert view(settings_of(tmp_path)).refused == ()
    assert view(settings_of(tmp_path)).testing is not None


def test_the_label_of_before_changes_nothing(hub: Hub, tmp_path: Path) -> None:
    """A pull request still labelled `merge-queue` (#1801, story 9) goes the
    same way as any other, without a gesture."""
    hub.open(7, GREEN).labels = {"merge-queue"}
    hub.open(8, GREEN)
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7
    assert [s.pull for s in found.waiting] == [8]


def test_a_label_of_entry_in_the_project_file_is_refused() -> None:
    """`label` and `adopt` left the file (#1801): a stale one is said, loud."""
    import tomllib

    from tracerworks.settings import ConfigError, parse

    shipped = FIXTURES / "project.toml"
    raw = tomllib.loads(shipped.read_text())
    raw["merge_queue"]["label"] = "merge-queue"
    with pytest.raises(ConfigError, match="label"):
        parse(raw, shipped, {})


@pytest.mark.parametrize("leaves", ["draft", "hors-file"])
def test_a_pull_request_in_test_that_leaves_the_queue_loses_the_testing_label(
    hub: Hub, tmp_path: Path, leaves: str
) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    pr = hub.pulls[7]
    assert "merge-queue-testing" in pr.labels
    if leaves == "draft":
        pr.draft = True
    else:
        pr.labels.add("hors-file")
    step(hub, tmp_path)
    assert "merge-queue-testing" not in pr.labels
    assert view(settings_of(tmp_path)).testing is None


def test_a_closed_pull_request_in_test_is_not_relabelled(
    hub: Hub, tmp_path: Path
) -> None:
    """The counter-proof: closed, nothing more is asked of GitHub."""
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.pulls[7].open = False
    step(hub, tmp_path)
    assert moves(hub, "label") == [("label", 7, True)]


# --- a light lane's green reused for the same code diff (#1844) ------------ #


def reuse_world(hub: Hub, tmp_path: Path) -> tuple[Repos, str, str]:
    """#7's code at `before`, its light lane green; then a doc-only commit
    `head`, its light lane still running — real commits, read by git."""
    repos = Repos(tmp_path / "git")
    repos.branch("feature")
    before = repos.commit("feature", branch="feature", files={"src/app.py": "x = 2\n"})
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    pr = hub.open(7, GREEN)
    pr.branch = "feature"
    hub.push(7, before, "success")
    hub.push(7, head)
    return repos, before, head


def reuse_step(
    hub: Hub, tmp_path: Path, repos: Repos, *, reuse: bool = True
) -> list[str]:
    """`step`, tracerworks's main clone being the fixture's, git really run."""
    settings = settings_of(tmp_path)
    settings = dataclasses.replace(
        settings,
        repo_dir=repos.clone,
        merge_queue=dataclasses.replace(settings.merge_queue, reuse=reuse),
    )
    fake = FakeRunner()
    fake.on("git", prefix=True)(lambda argv, stdin: run_process(argv, stdin=stdin))
    hub.clock += 30
    return advance(GitHub(wire(hub, fake), settings), settings, hub.clock)


def test_a_head_with_the_same_code_diff_enters_on_the_earlier_green(
    hub: Hub, tmp_path: Path
) -> None:
    repos, before, head = reuse_world(hub, tmp_path)
    said = reuse_step(hub, tmp_path, repos)
    assert f"merge queue: #7 entered on the light lane of {before[:8]}" in "\n".join(
        said
    )
    [comment] = moves(hub, "comment")
    assert comment[1] == 7
    assert f"voie légère reprise du commit `{before[:8]}`" in str(comment[2])
    # Its turn at once: up to date, its light lane is cancelled for the full
    # suite — never at the entry alone, a test always replaces it.
    assert moves(hub, "cancel") == [("cancel", 7, head)]
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7
    lines = merge_queue.describe(found, hub.clock)
    assert (
        f"merge queue: #7 entered on the light lane of {before[:8]} (same code diff)"
        in lines
    )
    shown = merge_queue.as_json(found)
    assert shown is not None and shown["testing"]["reused_from"] == before
    # The cancel over, the same run reruns under the testing label.
    reuse_step(hub, tmp_path, repos)
    assert moves(hub, "rerun") == [("rerun", 7, head)]
    assert "merge-queue-testing" in hub.pulls[7].labels
    reuse_step(hub, tmp_path, repos)
    assert moves(hub, "merge") == []
    hub.finish(head)
    reuse_step(hub, tmp_path, repos)
    assert moves(hub, "merge") == [("merge", 7, head)]
    assert len(moves(hub, "comment")) == 1, "said once"


def test_a_head_with_another_code_diff_waits_for_its_own_light_lane(
    hub: Hub, tmp_path: Path
) -> None:
    """The counter-proof: a code change after the green."""
    repos, _, _ = reuse_world(hub, tmp_path)
    code = repos.commit("code", branch="feature", files={"src/app.py": "x = 3\n"})
    hub.push(7, code)
    reuse_step(hub, tmp_path, repos)
    reuse_step(hub, tmp_path, repos)
    found = view(settings_of(tmp_path))
    assert (found.testing, found.waiting) == (None, [])
    assert hub.moves == []


def test_reuse_switched_off_waits_for_the_light_lane(hub: Hub, tmp_path: Path) -> None:
    repos, _, head = reuse_world(hub, tmp_path)
    reuse_step(hub, tmp_path, repos, reuse=False)
    found = view(settings_of(tmp_path))
    assert (found.testing, found.waiting) == (None, [])
    assert hub.moves == []
    hub.finish(head)  # its own light lane green: the path of before
    reuse_step(hub, tmp_path, repos, reuse=False)
    assert moves(hub, "label") == [("label", 7, True)]


def test_a_reused_head_waiting_has_its_light_lane_cancelled(
    hub: Hub, tmp_path: Path
) -> None:
    """Behind another in test, a reused head's light lane would hold a heavy
    runner for nothing: the queue redoes it all at its turn (#2109)."""
    hub.open(9, GREEN)
    step(hub, tmp_path)  # #9 in test
    repos, _, head = reuse_world(hub, tmp_path)
    reuse_step(hub, tmp_path, repos)
    assert [s.pull for s in view(settings_of(tmp_path)).waiting] == [7]
    assert moves(hub, "force-cancel") == [("force-cancel", 7, head)]
    said = reuse_step(hub, tmp_path, repos)
    assert [s.pull for s in view(settings_of(tmp_path)).waiting] == [7], said


def test_a_reuse_whose_comment_failed_is_said_on_the_next_pass(
    hub: Hub, tmp_path: Path
) -> None:
    repos, before, _ = reuse_world(hub, tmp_path)
    hub.comment_fails = True
    with pytest.raises(CommandError):
        reuse_step(hub, tmp_path, repos)
    assert view(settings_of(tmp_path)).waiting == []
    hub.comment_fails = False
    reuse_step(hub, tmp_path, repos)
    [comment] = moves(hub, "comment")
    assert f"`{before[:8]}`" in str(comment[2])
    reuse_step(hub, tmp_path, repos)
    assert len(moves(hub, "comment")) == 1


def test_a_reuse_of_an_earlier_stay_is_not_shown_on_a_later_one(
    hub: Hub, tmp_path: Path
) -> None:
    """Sent back, then in again on its own green: no « entered on » line."""
    hub.open(9, GREEN)
    step(hub, tmp_path)  # #9 in test: #7 will wait
    repos, before, head = reuse_world(hub, tmp_path)
    reuse_step(hub, tmp_path, repos)
    assert view(settings_of(tmp_path)).reused == {7: before}
    hub.finish(head, "failure")
    reuse_step(hub, tmp_path, repos)  # sent back
    hub.clock += 600
    hub.push(7, "h7-fix", "success")
    reuse_step(hub, tmp_path, repos)
    found = view(settings_of(tmp_path))
    assert [s.pull for s in found.waiting] == [7]
    assert found.reused == {}


def test_reuse_is_on_by_default_and_its_switch_must_be_a_boolean() -> None:
    import tomllib

    from tracerworks.settings import ConfigError, parse

    shipped = FIXTURES / "project.toml"
    raw = tomllib.loads(shipped.read_text())
    settings = parse(raw, shipped, {})
    assert (settings.merge_queue.reuse, settings.merge_queue.reuse_ignore) == (True, ())
    raw["merge_queue"]["reuse_ignore"] = ["tools/notes/**"]
    raw["merge_queue"].pop("reuse", None)
    settings = parse(raw, shipped, {})
    assert settings.merge_queue.reuse is True, "left out: on"
    assert settings.merge_queue.reuse_ignore == ("tools/notes/**",)
    raw["merge_queue"]["reuse"] = "no"
    with pytest.raises(ConfigError, match="reuse must be true or false"):
        parse(raw, shipped, {})


# --- a pull request waiting runs nothing the queue will redo (#2109) -------- #


def waiting_behind(hub: Hub, tmp_path: Path) -> None:
    """#9 in test, its run going; #7 green, waiting behind it."""
    hub.open(9, GREEN)
    step(hub, tmp_path)
    hub.open(7, GREEN)
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 9
    assert [s.pull for s in found.waiting] == [7]


def test_a_waiting_pull_requests_rerun_is_cancelled_once(
    hub: Hub, tmp_path: Path
) -> None:
    """30/10: #2086's and #2091's reruns held the heavy runners while the
    queue's run for #2065 waited 70 min for one."""
    waiting_behind(hub, tmp_path)
    run = hub.rerun_by_hand(7)
    said = step(hub, tmp_path)
    assert moves(hub, "force-cancel") == [("force-cancel", 7, "h7")]
    assert f"merge queue: #7 waiting — its run {run} cancelled" in "\n".join(said)
    [comment] = moves(hub, "comment")
    assert comment[1] == 7 and f"actions/runs/{run}" in str(comment[2])
    step(hub, tmp_path)
    step(hub, tmp_path)
    assert len(moves(hub, "force-cancel")) == 1, "cancelled once"
    assert len(moves(hub, "comment")) == 1, "said once"
    # Rerun again: that attempt is cancelled too, once.
    hub.rerun_by_hand(7)
    step(hub, tmp_path)
    step(hub, tmp_path)
    assert len(moves(hub, "force-cancel")) == 2


def test_the_queues_run_in_test_is_never_cancelled(hub: Hub, tmp_path: Path) -> None:
    waiting_behind(hub, tmp_path)
    head = hub.pulls[9].head
    assert hub.runs[head][-1]["status"] == "in_progress"
    hub.rerun_by_hand(7)
    for _ in range(3):
        step(hub, tmp_path)
    assert [m[1] for m in moves(hub, "force-cancel") + moves(hub, "cancel")] == [7]
    assert hub.runs[head][-1]["status"] == "in_progress"


def test_a_cancelled_run_does_not_send_the_pull_request_back(
    hub: Hub, tmp_path: Path
) -> None:
    """Its aggregate reads FAILURE once cancelled (#2091, 30/10): the run
    tracerworks cancelled is no red. At its turn, the queue tests it."""
    waiting_behind(hub, tmp_path)
    hub.rerun_by_hand(7)
    step(hub, tmp_path)
    said = step(hub, tmp_path)
    assert not any("#7 left" in line for line in said), said
    assert [s.pull for s in view(settings_of(tmp_path)).waiting] == [7]
    hub.finish(hub.pulls[9].head)
    step(hub, tmp_path)  # #9 merged; #7's turn
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7
    assert merge_queue.queue_cancelled(settings_of(tmp_path), 7) == frozenset()


def test_a_fresh_push_of_a_waiting_pull_request_keeps_its_light_lane(
    hub: Hub, tmp_path: Path
) -> None:
    """The counter-proof: its new head's own light lane says whether it is
    green — never cancelled."""
    waiting_behind(hub, tmp_path)
    hub.push(7, "h7b")
    step(hub, tmp_path)
    step(hub, tmp_path)
    assert moves(hub, "force-cancel") == []


def test_status_and_the_card_say_the_run_the_queue_cancelled(
    hub: Hub, tmp_path: Path
) -> None:
    waiting_behind(hub, tmp_path)
    run = hub.rerun_by_hand(7)
    step(hub, tmp_path)
    found = view(settings_of(tmp_path))
    url = f"https://github.com/owner/repo/actions/runs/{run}"
    assert found.cancelled == {7: (url, int(hub.clock))}
    lines = merge_queue.describe(found, hub.clock + 120)
    assert (
        f"merge queue: #7 waiting — its run {url} cancelled 2 min ago:"
        " the queue reruns everything at its turn"
    ) in lines
    shown = merge_queue.as_json(found)
    assert shown is not None
    assert shown["waiting"][0]["cancelled"] == {"url": url, "at": int(hub.clock)}
    assert shown["testing"]["cancelled"] is None


def test_a_cancel_that_fails_is_said_and_tried_again(hub: Hub, tmp_path: Path) -> None:
    """The pass goes on, its other lines kept; nothing recorded: the run
    stays what it is, and the next pass cancels it."""
    waiting_behind(hub, tmp_path)
    hub.rerun_by_hand(7)
    hub.cancel_fails = True
    said = step(hub, tmp_path)
    assert "merge queue: #7 — its run NOT cancelled (next pass)" in "\n".join(said)
    assert any("#9 in test" in line for line in said), said
    assert merge_queue.queue_cancelled(settings_of(tmp_path), 7) == frozenset()
    hub.cancel_fails = False
    step(hub, tmp_path)
    assert moves(hub, "force-cancel") == [("force-cancel", 7, "h7")]


# --- a full suite's red judged unrelated: back at the head (#2078) ------------ #


def test_a_red_judged_unrelated_enters_again_first_and_is_tested_on_its_turn(
    hub: Hub, tmp_path: Path
) -> None:
    """#2063 (29/10): sent back by a flaky test, it came back last, behind
    #2065. Now its worker's mark puts it back at the head of those waiting;
    its red does not send it back again, and the queue tests it next."""
    from tracerworks import chantier_state

    tested = in_full_test(hub, tmp_path)
    hub.open(8, GREEN, on=hub.main)
    hub.open(9, GREEN, on=hub.main)
    step(hub, tmp_path)  # #8 and #9 enter, wait
    hub.finish(tested, "failure")
    step(hub, tmp_path)  # #7 sent back, #8 in test
    settings = settings_of(tmp_path)

    said = merge_queue.readmit(
        GitHub(wire(hub), settings), settings, 7, tested, hub.clock
    )
    assert said is not None and "#7 back at the head of the queue" in said
    assert "tests/integration/test_widgets.py::test_reply" in said
    again = merge_queue.readmit(GitHub(wire(hub), settings), settings, 7, tested, 0)
    assert again is None, "once per head"
    [comment] = moves(hub, "comment")
    assert comment[1] == 7 and "repart en tête de la file" in str(comment[2])
    found = view(settings)
    assert [s.pull for s in found.waiting] == [7, 9]
    assert chantier_state.queue_states(found)[7] == "waiting"  # its card: MQ

    step(hub, tmp_path)  # still red, waiting: it stays
    assert [s.pull for s in view(settings).waiting] == [7, 9]
    hub.finish(hub.pulls[8].head)
    step(hub, tmp_path)  # #8 merged; #7 next, red as it is — before #9
    found = view(settings)
    assert found.testing is not None and found.testing.pull == 7
    assert "merge-queue-testing" in hub.pulls[7].labels
    assert moves(hub, "rerun-failed") == []


def test_a_light_lane_s_red_or_a_conflict_is_never_sent_back_to_the_head(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN)
    step(hub, tmp_path)
    hub.open(8, GREEN, on=hub.main)
    step(hub, tmp_path)  # #8 waits
    hub.push(8, "h8b", "failure")
    step(hub, tmp_path)  # its light lane red: it left
    settings = settings_of(tmp_path)
    readmitted = merge_queue.readmit(
        GitHub(wire(hub), settings), settings, 8, "h8b", hub.clock
    )
    assert readmitted is None
    assert [s.pull for s in view(settings).waiting] == []


def test_a_head_pushed_after_the_head_of_the_queue_leaves_on_its_own_red(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure")
    step(hub, tmp_path)
    settings = settings_of(tmp_path)
    assert merge_queue.readmit(
        GitHub(wire(hub), settings), settings, 7, tested, hub.clock
    )
    hub.push(7, "h7b", "failure")  # a new head, red on its own
    step(hub, tmp_path)
    assert view(settings).waiting == []


# --- a code red is never a machine's; one machine's re-run per head (#2080) --- #

CODE_STEPS = ("ruff check", "mypy", "front: build")


def coded_step(hub: Hub, tmp_path: Path) -> list[str]:
    """A pass with the project's code steps."""
    settings = settings_of(tmp_path)
    settings = dataclasses.replace(
        settings,
        merge_queue=dataclasses.replace(settings.merge_queue, code_steps=CODE_STEPS),
    )
    hub.clock += 30
    return advance(GitHub(wire(hub), settings), settings, hub.clock)


def test_a_mypy_red_is_a_code_red_sent_back_at_once_never_rerun(
    hub: Hub, tmp_path: Path
) -> None:
    """#2065 (29/10): `checks` red on a mypy error, no failed test named —
    read as a machine's and rerun twice. Its failed step says it: code."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=MACHINE_RED.replace("runner", "x"), step="mypy")
    said = coded_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"
    assert last.reason is not None and "a code red" in last.reason
    assert any("#7 left" in line for line in said)
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        assert machine_red.decided(db, "owner/repo", 7, tested) == machine_red.TEST_RED


def test_a_job_with_no_code_step_failed_stays_a_machine_s_red(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=MACHINE_RED, step="Set up job")
    coded_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == [("rerun-failed", 7, tested)]


def test_a_head_its_light_lane_reran_for_a_machine_is_not_rerun_by_the_queue(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        machine_red.record(db, "owner/repo", 7, tested, 0, machine_red.PARKED)
    hub.finish(tested, "failure", log=MACHINE_RED)
    step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"


def test_a_failed_step_github_does_not_give_is_never_read_as_no_step(
    hub: Hub, tmp_path: Path
) -> None:
    """A 502 on the jobs API: not « no code step », so not re-run — the
    pass is said unreadable, the next one asks again."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=MACHINE_RED.replace("runner", "x"), step="mypy")
    hub.steps_fail = True
    with pytest.raises(CommandError):
        coded_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    hub.steps_fail = False
    coded_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.reason is not None and "code red" in last.reason


# --- a short test's red is rerun once, like a machine's (#2130, spec #2127) -- #

#: #2125 on 30/10: one test read 3601 instead of 3600 in #2099's full suite.
FLAKY = "tests/tracerworks/test_alerts.py::test_the_eighth_failure_is_final"


def red_log(*tests: str) -> str:
    """A tranche's log naming these tests failed."""
    return "".join(
        f"2026-10-30T10:00:00Z FAILED {test} - AssertionError\n" for test in tests
    ) + ("2026-10-30T10:00:01Z ##[error]Process completed with exit code 1.\n")


def flaky_step(hub: Hub, tmp_path: Path, most: int = 3) -> list[str]:
    """A pass with the project's threshold (`tracerworks.toml`: 3)."""
    settings = settings_of(tmp_path)
    settings = dataclasses.replace(
        settings,
        merge_queue=dataclasses.replace(settings.merge_queue, flaky_max_tests=most),
    )
    hub.clock += 30
    return advance(GitHub(wire(hub), settings), settings, hub.clock)


def flaky_in_test(hub: Hub, tmp_path: Path, log: str = red_log(FLAKY)) -> str:
    """#7 in test, its full suite red on `log`, and the pass that reads it."""
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=log)
    return tested


def test_one_test_s_red_is_rerun_once_in_the_queue(hub: Hub, tmp_path: Path) -> None:
    tested = flaky_in_test(hub, tmp_path)
    said = flaky_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == [("rerun-failed", 7, tested)]
    found = view(settings_of(tmp_path))
    assert found.testing is not None and found.testing.pull == 7, "still in test"
    assert found.testing.machine_rerun_at is not None
    assert "merge-queue-testing" in hub.pulls[7].labels
    assert merge_queue.resume_note(settings_of(tmp_path), 7) == "", "no worker"
    assert any("rerun once: 1 flaky test(s)" in line for line in said)
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        assert machine_red.decided(db, "owner/repo", 7, tested) == machine_red.FLAKY
    assert moves(hub, "bug") == [], "a bug only once the rerun is green"


def test_a_green_rerun_merges_and_opens_the_flaky_test_s_bug(
    hub: Hub, tmp_path: Path
) -> None:
    tested = flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    hub.finish(tested)
    said = flaky_step(hub, tmp_path)
    assert moves(hub, "merge") == [("merge", 7, tested)]
    [(_, number, title, body)] = moves(hub, "bug")
    assert isinstance(title, str) and isinstance(body, str)
    assert FLAKY in title.split()
    assert "#7" in body and "https://github.com/owner/repo/actions/runs/" in body
    assert any(f"#{number}" in line and FLAKY in line for line in said)
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "merged")


def test_a_red_rerun_sends_the_pull_request_to_its_worker(
    hub: Hub, tmp_path: Path
) -> None:
    """The counter-proof: a test red each time stays red at its rerun."""
    tested = flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    flaky_step(hub, tmp_path)  # the rerun runs
    hub.finish(tested, "failure", log=red_log(FLAKY))
    flaky_step(hub, tmp_path)
    assert len(moves(hub, "rerun-failed")) == 1, "one rerun, never two"
    assert moves(hub, "merge") == [] and moves(hub, "bug") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"
    assert last.reason is not None and "again after one rerun" in last.reason
    assert "merge-queue-testing" not in hub.pulls[7].labels
    assert "sortie de la file de fusion" in merge_queue.resume_note(
        settings_of(tmp_path), 7
    )


def test_four_tests_red_are_never_rerun(hub: Hub, tmp_path: Path) -> None:
    flaky_in_test(hub, tmp_path, red_log(*(f"{FLAKY}_{n}" for n in range(4))))
    flaky_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "sent_back")


def test_the_threshold_is_the_project_s(hub: Hub, tmp_path: Path) -> None:
    """Three tests rerun at 3, not at 2."""
    flaky_in_test(hub, tmp_path, red_log(*(f"{FLAKY}_{n}" for n in range(3))))
    flaky_step(hub, tmp_path, most=2)
    assert moves(hub, "rerun-failed") == []


def test_a_test_of_a_file_the_pull_request_modifies_is_never_rerun(
    hub: Hub, tmp_path: Path
) -> None:
    hub.open(7, GREEN).files = ["docs/x.md", FLAKY.split("::")[0]]
    hub.push_to_main("m-7", merging=3)
    step(hub, tmp_path)
    hub.finish(hub.pulls[7].head, "failure", log=red_log(FLAKY))
    flaky_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and (last.pull, last.outcome) == (7, "sent_back")


def test_another_file_modified_keeps_the_rerun(hub: Hub, tmp_path: Path) -> None:
    hub.open(7, GREEN).files = ["tools/tracerworks/alerts.py"]
    hub.push_to_main("m-7", merging=3)
    step(hub, tmp_path)
    hub.finish(hub.pulls[7].head, "failure", log=red_log(FLAKY))
    flaky_step(hub, tmp_path)
    assert len(moves(hub, "rerun-failed")) == 1


def test_a_machine_s_red_then_a_test_s_red_on_one_head_is_one_rerun_in_all(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    hub.finish(tested, "failure", log=MACHINE_RED)
    flaky_step(hub, tmp_path)
    flaky_step(hub, tmp_path)  # the rerun runs
    hub.finish(tested, "failure", log=red_log(FLAKY))
    flaky_step(hub, tmp_path)
    assert len(moves(hub, "rerun-failed")) == 1, "one rerun per head, shared"
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.outcome == "sent_back"


def test_a_head_its_light_lane_reran_has_no_flaky_rerun(
    hub: Hub, tmp_path: Path
) -> None:
    tested = in_full_test(hub, tmp_path)
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        machine_red.record(db, "owner/repo", 7, tested, 0, machine_red.NO_WORKER)
    hub.finish(tested, "failure", log=red_log(FLAKY))
    flaky_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []


def test_an_open_bug_of_the_test_is_commented_never_doubled(
    hub: Hub, tmp_path: Path
) -> None:
    """#2125 was opened by hand, the test's name in its title."""
    hub.bugs[2125] = "Flaky: `test_the_eighth_failure_is_final` reads 3601"
    hub.bugs[2126] = "Another bug"
    tested = flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    hub.finish(tested)
    flaky_step(hub, tmp_path)
    assert moves(hub, "bug") == []
    [(_, number, body)] = moves(hub, "bug-comment")
    assert isinstance(body, str)
    assert number == 2125 and "#7" in body and "/actions/runs/" in body


def test_a_bug_github_refuses_never_holds_the_merge(hub: Hub, tmp_path: Path) -> None:
    tested = flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    hub.finish(tested)
    fake = wire(hub)
    fake.answer("gh", "issue", "list", stdout="gh: HTTP 502", returncode=1, prefix=True)
    settings = settings_of(tmp_path)
    settings = dataclasses.replace(
        settings,
        merge_queue=dataclasses.replace(settings.merge_queue, flaky_max_tests=3),
    )
    hub.clock += 30
    said = advance(GitHub(fake, settings), settings, hub.clock)
    assert moves(hub, "merge") == [("merge", 7, tested)]
    assert any("NOT" in line and FLAKY in line for line in said)


def test_without_a_threshold_a_test_s_red_goes_back_as_before(
    hub: Hub, tmp_path: Path
) -> None:
    """The key absent from the project file: 0, the rule off."""
    assert settings_of(tmp_path).merge_queue.flaky_max_tests == 0
    flaky_in_test(hub, tmp_path)
    step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []


def test_status_and_the_card_say_the_flaky_rerun(hub: Hub, tmp_path: Path) -> None:
    flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    with closing(store.connect(settings_of(tmp_path).history_db)) as db:
        shown = machine_red.recent(db, "owner/repo", hub.clock, lambda head, at: 2)
    assert [s.pull for s in shown] == [7]
    assert "rerun once: 1 flaky test(s)" in shown[0].text


def test_a_red_rerun_says_it_was_for_flaky_tests_not_the_machine(
    hub: Hub, tmp_path: Path
) -> None:
    tested = flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    flaky_step(hub, tmp_path)
    hub.finish(tested, "failure", log=red_log(FLAKY))
    flaky_step(hub, tmp_path)
    last = view(settings_of(tmp_path)).last_out
    assert last is not None and last.reason is not None
    assert "flaky" in last.reason and "machine" not in last.reason


def test_a_bug_of_another_file_s_test_of_the_same_name_is_not_commented(
    hub: Hub, tmp_path: Path
) -> None:
    name = FLAKY.split("::")[1]
    hub.bugs[2200] = f"Flaky: tests/integration/test_other.py::{name}"
    tested = flaky_in_test(hub, tmp_path)
    flaky_step(hub, tmp_path)
    hub.finish(tested)
    flaky_step(hub, tmp_path)
    assert moves(hub, "bug-comment") == []
    assert len(moves(hub, "bug")) == 1


def test_a_collection_error_is_never_rerun(hub: Hub, tmp_path: Path) -> None:
    """`ERROR path`: the file could not even be collected — no flaky test."""
    log = "2026-10-30T10:00:00Z ERROR tests/tracerworks/test_alerts.py\n"
    flaky_in_test(hub, tmp_path, log)
    flaky_step(hub, tmp_path)
    assert moves(hub, "rerun-failed") == []
