"""One state per chantier, the same words everywhere, and its history (#1729).

A test gives the facts the world gives — a hook's last word, a parked
record, a pull request, the merge queue — and reads the state and the words
that come out; never the order of the calls. The scenarios are the factory's
register's lines, replayed.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from tests.tracerworks.fakes import ROOT, fixture, recorded_settings, recorded_world
from tests.tracerworks.test_snapshot import github_world, snapshot, workers_of
from tests.tracerworks.test_status import NOW, env_for
from tests.tracerworks.test_usage import _worktree_logs
from tracerworks import chantier_state, parking, store
from tracerworks.chantier_state import ALLOWED, Facts, Phase, State, allowed, derive
from tracerworks.cli import EXIT_OK, Env, recorded_states, run
from tracerworks.hook_state import HookEvent
from tracerworks.observe import WorkerState, read_worker
from tracerworks.runtime.tmux import Window

P = Phase
REPO = "owner/repo"
CLAUDE = Window("worker-1235", "claude", 0, "claude")
IDLE = fixture("pane-claude-idle.txt")


# --- the table ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (P.QUEUED, P.DEVELOPMENT),
        (P.DEVELOPMENT, P.LIGHT_CHECK),
        (P.LIGHT_CHECK, P.MERGE_QUEUE),
        (P.MERGE_QUEUE, P.DONE),
        (P.MERGE_QUEUE, P.DEVELOPMENT),  # sent back to its worker
        (P.DEVELOPMENT, P.HUMAN),
        (P.HUMAN, P.DEVELOPMENT),
        (P.PARKED, P.DEVELOPMENT),
        (P.DONE, P.QUEUED),  # reopened
        (P.BLOCKED, P.BLOCKED),  # itself, another reason
    ],
)
def test_a_transition_of_the_table_is_allowed(before: Phase, after: Phase) -> None:
    assert allowed(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (P.QUEUED, P.LIGHT_CHECK),  # a light check needs a pull request
        (P.QUEUED, P.MERGE_QUEUE),
        (P.QUEUED, P.DONE),
        (P.MERGE_QUEUE, P.QUEUED),
        (P.DONE, P.MERGE_QUEUE),
        (P.DONE, P.LIGHT_CHECK),
    ],
)
def test_a_transition_outside_the_table_is_refused(before: Phase, after: Phase) -> None:
    assert not allowed(before, after)


def test_the_table_names_every_state_and_each_has_a_way_out() -> None:
    assert set(ALLOWED) == set(Phase)
    assert all(ALLOWED[phase] for phase in Phase)


# --- the register's scenarios, replayed ---------------------------------------


def from_hooks(event: HookEvent, pane: str = IDLE) -> State:
    reading = read_worker(CLAUDE, pane, event)
    return derive(
        Facts(worker=reading.state, asking=reading.waits, asking_kind=reading.kind)
    )


def test_the_prompt_of_12_10_waits_for_a_human_with_its_text() -> None:
    """worker-1235 « waiting » behind « Teach auto mode… »: a prompt."""
    hook = HookEvent("Notification", "permission_prompt", "Teach auto mode?", NOW)
    assert (
        from_hooks(hook).describe()
        == "waits for a human (permission: Teach auto mode?)"
    )


def test_the_login_of_22_10_is_blocked_with_the_gesture() -> None:
    """#1625 launched logged out, shown « waiting — in development »."""
    state = from_hooks(HookEvent("StopFailure", "authentication_failed", None, NOW))
    assert state.phase is P.BLOCKED
    assert state.reason == "login expired — /login in the worker's window"


def test_the_question_of_19_10_waits_for_a_human_with_the_question() -> None:
    """#1529 asked in its window, and nobody knew."""
    hook = HookEvent("Notification", "elicitation_dialog", "Which token?", NOW)
    assert from_hooks(hook).describe() == "waits for a human (question: Which token?)"


def test_a_question_asked_on_github_waits_for_a_human_too() -> None:
    state = derive(Facts(worker=WorkerState.WAITING, needs_human=1530))
    assert state.describe() == "waits for a human (asked on #1530)"


def test_the_memory_of_18_10_keeps_a_spec_queued_and_says_why(tmp_path: Path) -> None:
    """#1493 held all night for memory: the snapshot's queue says why."""
    from tracerworks.guards import Admission
    from tracerworks.snapshot import _queued_phase

    posts = {"waiting": {}, "queue": [{"number": 1493}]}
    low = Admission(None, "no", ("available memory 900 MB < threshold 2500 MB",))
    [entry] = _queued_phase(posts, low)["queue"]
    assert (entry["phase"], entry["phase_text"]) == (
        "queued",
        "queued (waits for memory)",
    )
    [free] = _queued_phase(posts, Admission(None, "no", ()))["queue"]
    assert free["phase_text"] == "queued"


def test_a_worker_whose_ci_ended_is_not_in_a_check_19_10() -> None:
    """A lane shown « waiting for CI » although free: only a CI underway is
    a light check; a CI ended leaves its worker in development, at rest."""
    rest = Facts(worker=WorkerState.WAITING, pull=720, ci_underway=False)
    assert derive(rest).describe() == "in development (at rest)"
    running = Facts(worker=WorkerState.WAITING, pull=720, ci_underway=True)
    assert derive(running).describe() == "light check (PR #720)"


W = WorkerState
LIMIT = "limit reached: rate_limit"


@pytest.mark.parametrize(
    ("facts", "words"),
    [
        (Facts(worker=W.WORKING), "in development"),
        (Facts(parked_pull=720), "light check (PR #720, its session closed)"),
        # 30/10, #2094: in conflict, no run — never « light check »
        (
            Facts(parked_pull=720, conflicting=True),
            "parked (PR #720 in conflict, not resumed yet)",
        ),
        (
            Facts(parked_pull=720, conflicting=True, ci_underway=True),
            "light check (PR #720, its session closed)",
        ),
        (Facts(worker=W.STOPPED, set_aside_for=520), "parked (set aside for #520)"),
        (Facts(worker=W.STOPPED, set_aside_for=0), "parked (set aside by hand)"),
        (Facts(worker=W.STOPPED), "parked (stopped)"),
        (Facts(worker=W.DEAD), "blocked (its provider is gone from its window)"),
        (
            Facts(pull=7, queue_state="testing"),
            "in the merge queue (its full suite runs)",
        ),
        (
            Facts(worker=W.WAITING, pull=7, queue_state="waiting"),
            "in the merge queue (waiting its turn)",
        ),
        (Facts(worker=W.WAITING, merged=7), "done (PR #7 merged)"),
        (Facts(), "queued"),
        # a merge outranks all: a worker still at rest after it is done
        (
            Facts(worker=W.ASKING, asking="q", asking_kind="question", merged=7),
            "done (PR #7 merged)",
        ),
        # a limit is a gesture, not an answer
        (
            Facts(worker=W.ASKING, asking=LIMIT, asking_kind="limit"),
            f"blocked ({LIMIT})",
        ),
    ],
)
def test_each_state_is_said_in_one_set_of_words(facts: Facts, words: str) -> None:
    assert derive(facts).describe() == words


def test_the_0_of_5_of_09_10_counts_the_pushed_ticket_commits(tmp_path: Path) -> None:
    """Five tickets coded, « 0/5 »: a ticket open until its pull request
    merges is done once its commit `(#n)` is pushed."""
    fake = github_world(recorded_world())
    fake.answer(
        "git", "-C", str(ROOT / "501"), "log", "--format=%s",
        "origin/main..@{upstream}",
        stdout="feat(console): the screen (#503)\nfix: a typo in #504's title\n",
    )  # fmt: skip
    _worktree_logs(tmp_path / "projects", ROOT / "501")
    worker = workers_of(snapshot(fake, tmp_path))[501]
    assert [(t["number"], t["status"]) for t in worker["tickets"]] == [
        (502, "done"),
        (503, "done"),
        (504, "todo"),  # named, not its commit
    ]
    assert (worker["done"], worker["total"]) == (2, 3)


# --- the same words in status and the workshop --------------------------------


def test_status_and_the_workshop_say_the_same_words(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    _worktree_logs(tmp_path / "projects", ROOT / "501")
    shown = workers_of(snapshot(fake, tmp_path))
    env, out, _ = env_for(recorded_world(), tmp_path)
    assert run(env, ["status", "--json"]) == EXIT_OK
    said = {w["spec"]: w["phase_text"] for w in json.loads(out.getvalue())["workers"]}
    page = {spec: w["phase_text"] for spec, w in shown.items()}
    # 510's ticket carries needs-human: a GitHub fact only the snapshot reads
    assert page.pop(510) == "waits for a human (asked on #511)"
    assert said.pop(510) == "in development (at rest)"
    assert said == page
    assert said[501] == "in development"


# --- the history ---------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(tmp_path / "h.sqlite3")) as base:
        yield base


def state(phase: Phase, reason: str | None = None, cause: str = "test") -> State:
    return State(phase, reason, cause)


def test_each_change_is_written_once_with_its_cause(db: sqlite3.Connection) -> None:
    record = chantier_state.record
    assert record(db, REPO, 7, state(P.QUEUED), None, 100) is not None
    assert record(db, REPO, 7, state(P.QUEUED), None, 160) is None  # unchanged
    assert record(db, REPO, 7, state(P.DEVELOPMENT, None, "worker: working"), 1, 220)
    assert record(db, REPO, 7, state(P.DEVELOPMENT, "at rest"), 1, 280)  # its reason
    history = chantier_state.steps(db, REPO, 7)
    assert [(s.at, s.phase, s.reason, s.lane) for s in history] == [
        (100, P.QUEUED, None, None),
        (220, P.DEVELOPMENT, None, 1),
        (280, P.DEVELOPMENT, "at rest", 1),
    ]
    assert history[1].cause == "worker: working"
    assert chantier_state.since(db, REPO, [7, 8]) == {7: 220}


def test_a_refused_change_is_journaled_once_and_the_state_kept(
    db: sqlite3.Connection,
) -> None:
    record = chantier_state.record
    record(db, REPO, 7, state(P.QUEUED), None, 100)
    refused = record(db, REPO, 7, state(P.MERGE_QUEUE, "waiting its turn"), None, 160)
    assert refused is not None and refused.refused
    assert (
        record(db, REPO, 7, state(P.MERGE_QUEUE, "waiting its turn"), None, 220) is None
    )
    current = chantier_state.current(db, REPO, 7)
    assert current is not None and current.phase is P.QUEUED
    # the world agrees with the table again
    assert record(db, REPO, 7, state(P.DEVELOPMENT), 1, 280) is not None
    assert [s.refused for s in chantier_state.steps(db, REPO, 7)] == [
        False,
        True,
        False,
    ]


def test_a_refused_state_that_lasts_is_real_and_the_history_moves_on(
    db: sqlite3.Connection,
) -> None:
    """Review of #1727: `serve` down while a chantier went from queued to
    merged; the snapshot that comes back sees « done », the table refuses it
    — a race lasts a snapshot, this lasts: accepted 10 min later, said so."""
    record = chantier_state.record
    record(db, REPO, 7, state(P.QUEUED), None, 100)
    assert record(db, REPO, 7, state(P.DONE, "PR #9 merged", "pull merged"), None, 160)
    assert record(db, REPO, 7, state(P.DONE, "PR #9 merged"), None, 160 + 599) is None
    confirmed = record(
        db, REPO, 7, state(P.DONE, "PR #9 merged", "pull merged"), None, 760
    )
    assert confirmed is not None and not confirmed.refused
    assert confirmed.cause == "pull merged, confirmed: refused, then lasting"
    current = chantier_state.current(db, REPO, 7)
    assert current is not None and current.phase is P.DONE


def test_the_workshop_card_carries_since_its_story_and_its_alert(
    tmp_path: Path,
) -> None:
    """What `serve` adds to each worker after recording (#1729, #1730)."""
    settings = recorded_settings(tmp_path)
    red = state(P.PARKED, "PR #720 red, not resumed yet", "parked: red")
    with closing(store.connect(settings.history_db)) as base:
        chantier_state.record(
            base, REPO, 520, state(P.DEVELOPMENT), 1, int(NOW) - 4 * 3600
        )
        chantier_state.record(base, REPO, 520, red, 1, int(NOW) - 3 * 3600)
    env, _, _ = env_for(recorded_world(), tmp_path)
    shown = {
        "workers": [
            {"spec": 520, "phase": "parked", "phase_reason": red.reason,
             "phase_cause": red.cause, "lane": 1}
        ]
    }  # fmt: skip
    [card] = recorded_states(env, shown)["workers"]
    assert card["phase_since"] == int(NOW) - 3 * 3600
    assert [line.split(" — ", 1)[1] for line in card["history"]] == [
        "in development — lane 1 — test",
        "parked (PR #720 red, not resumed yet) — lane 1 — parked: red",
    ]
    assert card["alert"] == "parked for 180 min — PR #720 red, not resumed yet"


def test_serve_records_the_snapshot_and_journals_a_refusal(tmp_path: Path) -> None:
    env, _, err = env_for(recorded_world(), tmp_path)
    queued = {
        "workers": [],
        "queue": [{"number": 9, "phase": "queued", "phase_cause": "no worker"}],
    }
    jumped = {
        "workers": [
            {
                "spec": 9,
                "phase": "merge_queue",
                "phase_reason": "waiting its turn",
                "phase_cause": "merge queue: waiting",
                "lane": 2,
            }
        ],
        "queue": [],
    }
    assert recorded_states(env, queued) is queued  # shown as it is
    recorded_states(env, jumped)
    assert "#9: transition to 'in the merge queue (waiting its turn)' refused" in (
        err.getvalue()
    )


def test_history_unrolls_a_chantier_with_its_causes(tmp_path: Path) -> None:
    settings = recorded_settings(tmp_path)
    with closing(store.connect(settings.history_db)) as base:
        chantier_state.record(
            base, REPO, 9, state(P.QUEUED, None, "no worker"), None, 100
        )
        chantier_state.record(base, REPO, 9, state(P.DONE), None, 150)  # refused
        chantier_state.record(
            base,
            REPO,
            9,
            state(P.HUMAN, "question: which?", "worker: question"),
            1,
            200,
        )
    out = io.StringIO()
    env = Env(
        settings, runner=recorded_world(), now=lambda: NOW, out=out, err=io.StringIO()
    )
    assert run(env, ["history", "9"]) == EXIT_OK
    lines = out.getvalue().splitlines()
    assert lines[0].endswith(" — queued — no worker")
    assert " — REFUSED: done — test" in lines[1]
    assert lines[2].endswith(
        " — waits for a human (question: which?) — lane 1 — worker: question"
    )
    out.truncate(0), out.seek(0)
    assert run(env, ["history", "10"]) == EXIT_OK
    assert out.getvalue().startswith("#10: no state recorded yet")


def test_status_says_since_when_a_chantier_is_in_its_state(tmp_path: Path) -> None:
    settings = recorded_settings(tmp_path)
    with closing(store.connect(settings.history_db)) as base:
        chantier_state.record(
            base, REPO, 501, state(P.DEVELOPMENT, None), 1, int(NOW) - 3 * 3600
        )
    env, out, _ = env_for(recorded_world(), tmp_path)
    assert run(env, ["status"]) == EXIT_OK
    [line] = [x for x in out.getvalue().splitlines() if x.startswith("#501")]
    assert " — in development since 3 h 00 ago — " in line
    out.truncate(0), out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    [w] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 501]
    assert w["phase_since"] == int(NOW) - 3 * 3600


def test_status_says_a_parked_pull_request_in_conflict(tmp_path: Path) -> None:
    """30/10, #2094: parked in conflict, `status` said « light check » too —
    it reads the open conflict episodes of the base, no GitHub call."""
    settings = recorded_settings(tmp_path)
    parking.park(settings, 520, None, 815, 0)
    with closing(store.connect(settings.history_db)) as base:
        base.execute(
            "INSERT INTO pull_conflict (project, pull, seen_at) VALUES (?, ?, ?)",
            (REPO, 815, int(NOW) - 600),
        )
    env, out, _ = env_for(recorded_world(), tmp_path)
    assert run(env, ["status", "--json"]) == EXIT_OK
    [w] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 520]
    assert w["phase_text"] == "parked (PR #815 in conflict, not resumed yet)"


def test_the_order_reads_one_line_a_chantier_in_its_words() -> None:
    from tracerworks.order import Factory, Role

    found = Factory(
        {1693: Role.DEVELOPMENT, 1701: Role.SET_ASIDE}, {1701: 1, 1693: 2}, {}, 6, 2
    )
    assert found.describe() == [
        "1st #1701 parked (set aside)",
        "2nd #1693 in development",
    ]
    said = {1693: "waits for a human (question: x)"}
    assert found.describe(said)[1] == "2nd #1693 waits for a human (question: x)"


def test_a_text_left_in_a_field_is_said_and_leaves_a_running_ci_s_phase() -> None:
    """#2003: said in the reason; the phase is the CI's while it runs."""
    rest = Facts(worker=WorkerState.WAITING, pending="ouvre la PR de #1587")
    assert derive(rest).phase is Phase.DEVELOPMENT
    assert 'text waiting in its field: "ouvre la PR de #1587"' in str(
        derive(rest).reason
    )
    ci = Facts(
        worker=WorkerState.WAITING, pending="ouvre la PR", ci_underway=True, pull=7
    )
    assert derive(ci).phase is Phase.LIGHT_CHECK
    assert "text waiting in its field" in str(derive(ci).reason)


def test_a_pull_request_back_at_the_head_of_the_queue_is_in_the_merge_queue() -> None:
    """#2078: its worker parked on a red judged unrelated, the pull request
    waits at the head of the queue — its card says Merge queue, not « parked:
    red » nor LIGHT CHECK."""
    state = derive(Facts(parked_pull=720, pull=720, ci_red=True, queue_state="waiting"))
    assert state.phase is Phase.MERGE_QUEUE
