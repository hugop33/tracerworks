"""Each wait has a deadline (#1730).

The clock is injected (`now`): a wait is a run of rows in the chantiers'
history, and a test writes the rows and moves the clock — the human label
goes on at 30 min, not before, once per episode, and comes off when the
worker works again; a worker that waits for memory goes on when it is back;
a chantier parked red too long alerts.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FIXTURES, recorded_settings, recorded_world
from tracerworks import chantier_state, store, waits
from tracerworks.alerts import conditions
from tracerworks.chantier_state import Phase, State
from tracerworks.cli import Env
from tracerworks.hook_state import (
    MEMORY_MARKER,
    SCRIPT,
    HookEvent,
    hook_file,
    last_word,
    turn_end,
)
from tracerworks.process import CommandError

REPO = "owner/repo"
T0 = 1_790_000_000  # the wait's start; the deadline is T0 + 30 min
P = Phase


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(store.connect(tmp_path / "h.sqlite3")) as base:
        yield base


def enter(db: sqlite3.Connection, spec: int, state: State, at: int) -> None:
    chantier_state.record(db, REPO, spec, state, 1, at)


PROMPT = State(P.HUMAN, "permission: Bash command: rm -rf x", "worker: permission")
LOGIN = State(
    P.BLOCKED, "login expired — /login in the worker's window", "worker: login"
)
WORKING = State(P.DEVELOPMENT, None, "worker: working")


class Gh:
    """`gh` as a list of what it was asked; `fail` makes the next call fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = False

    def __call__(self, argv: list[str], body: str | None) -> None:
        if self.fail:
            raise CommandError("gh: could not resolve host")
        self.calls.append((argv, body))


def tend(db: sqlite3.Connection, gh: Gh, now: int, *specs: int) -> list[str]:
    return waits.tend(
        db,
        gh,
        project=REPO,
        label="needs-human",
        specs=specs,
        now=now,
        after_minutes=30,
        window=lambda n: f"worker-{n}",
        session="widgets",
    )


def labels(gh: Gh) -> list[tuple[str, str]]:
    return [(a[2], a[a.index("--add-label") + 1] if "--add-label" in a else "-")
            for a, _ in gh.calls if a[:2] == ["issue", "edit"]]  # fmt: skip


def test_the_label_goes_on_at_30_min_not_before(db: sqlite3.Connection) -> None:
    enter(db, 7, PROMPT, T0)
    gh = Gh()
    assert tend(db, gh, T0 + 29 * 60 + 59, 7) == []
    assert gh.calls == []
    assert tend(db, gh, T0 + 30 * 60, 7) == [
        "#7 labeled needs-human: permission: Bash command: rm -rf x"
    ]
    assert gh.calls[0][0] == [
        "issue", "edit", "7", "-R", REPO, "--add-label", "needs-human"
    ]  # fmt: skip
    argv, body = gh.calls[1]
    assert argv == ["issue", "comment", "7", "-R", REPO, "--body-file", "-"]
    assert body is not None
    assert "attend un humain depuis 30 min" in body
    assert "permission: Bash command: rm -rf x" in body
    assert "répondre à l'invite dans la fenêtre `worker-7`" in body


def test_one_comment_per_episode_never_two(db: sqlite3.Connection) -> None:
    enter(db, 7, PROMPT, T0)
    gh = Gh()
    tend(db, gh, T0 + 31 * 60, 7)
    tend(db, gh, T0 + 32 * 60, 7)
    tend(db, gh, T0 + 90 * 60, 7)
    assert len([a for a, _ in gh.calls if a[:2] == ["issue", "comment"]]) == 1


def test_the_label_comes_off_when_the_worker_works_again(
    db: sqlite3.Connection,
) -> None:
    enter(db, 7, PROMPT, T0)
    gh = Gh()
    tend(db, gh, T0 + 31 * 60, 7)
    enter(db, 7, WORKING, T0 + 40 * 60)
    assert tend(db, gh, T0 + 41 * 60, 7) == [
        "#7 unlabeled needs-human: its worker no longer waits"
    ]
    assert gh.calls[-1][0] == [
        "issue", "edit", "7", "-R", REPO, "--remove-label", "needs-human"
    ]  # fmt: skip
    calls = len(gh.calls)
    assert tend(db, gh, T0 + 42 * 60, 7) == []  # once
    assert len(gh.calls) == calls
    # a new wait is a new episode: told again, after its own 30 min
    enter(db, 7, LOGIN, T0 + 50 * 60)
    assert tend(db, gh, T0 + 79 * 60, 7) == []
    [line] = tend(db, gh, T0 + 80 * 60, 7)
    assert line.startswith("#7 labeled needs-human: login expired")
    assert "`/login` dans la fenêtre `worker-7`" in str(gh.calls[-1][1])


def test_a_label_already_there_is_not_tracerworks_s_to_take_off(
    db: sqlite3.Connection,
) -> None:
    """A worker that asked on GitHub itself (`human label` cause): neither
    labeled again nor unlabeled by tracerworks."""
    asked = State(P.HUMAN, "asked on #8", "human label")
    enter(db, 7, asked, T0)
    gh = Gh()
    tend(db, gh, T0 + 60 * 60, 7)
    enter(db, 7, WORKING, T0 + 61 * 60)
    tend(db, gh, T0 + 62 * 60, 7)
    assert gh.calls == []


def test_github_unreachable_is_tried_again_never_doubled(
    db: sqlite3.Connection,
) -> None:
    enter(db, 7, PROMPT, T0)
    gh = Gh()
    gh.fail = True
    [said] = tend(db, gh, T0 + 31 * 60, 7)
    assert said.startswith("#7: its wait NOT told on GitHub (next pass)")
    gh.fail = False
    tend(db, gh, T0 + 32 * 60, 7)
    tend(db, gh, T0 + 33 * 60, 7)
    assert len([a for a, _ in gh.calls if a[:2] == ["issue", "comment"]]) == 1


def test_tracerworks_s_own_label_never_holds_the_wait(db: sqlite3.Connection) -> None:
    """Review of #1727: the label tracerworks put, read back by the snapshot,
    made the chantier « waits for a human (asked on #7) ». The worker
    answered and came back to rest between two snapshots: no « in
    development » row ever — the label must come off all the same."""
    enter(db, 7, PROMPT, T0)
    gh = Gh()
    tend(db, gh, T0 + 31 * 60, 7)
    asked = State(P.HUMAN, "asked on #7", "human label")  # its own label, read back
    enter(db, 7, asked, T0 + 40 * 60)
    assert tend(db, gh, T0 + 41 * 60, 7) == [
        "#7 unlabeled needs-human: its worker no longer waits"
    ]
    assert gh.calls[-1][0][-2:] == ["--remove-label", "needs-human"]


@pytest.mark.parametrize(
    ("cause", "gesture"),
    [
        ("worker: question", "lui répondre par `tracerworks message 7`."),
        ("worker: limit", "rien à répondre"),
        ("worker: login", "`/login` dans la fenêtre `worker-7`"),
        ("worker: asking", "regarder la fenêtre `worker-7`"),
    ],
)
def test_each_wait_says_the_gesture_of_its_kind(cause: str, gesture: str) -> None:
    """Read from its cause — the kind — never from its words."""
    assert waits.gesture(cause, 7, "worker-7", "widgets").startswith(gesture)


def test_serve_s_cycle_labels_through_gh(tmp_path: Path) -> None:
    """The real adapter: `gh issue edit` then `gh issue comment` on stdin."""
    from tracerworks.cli import Env, recorded_states

    settings = recorded_settings(tmp_path)
    with closing(store.connect(settings.history_db)) as base:
        enter(base, 510, PROMPT, T0)
    fake = recorded_world()
    fake.answer("gh", "issue", "edit", prefix=True)
    fake.answer("gh", "issue", "comment", prefix=True)
    err = io.StringIO()
    env = Env(settings, runner=fake, now=lambda: T0 + 31 * 60, err=err)
    shown = {
        "workers": [
            {
                "spec": 510,
                "phase": "human",
                "phase_reason": PROMPT.reason,
                "phase_cause": PROMPT.cause,
                "lane": 1,
            }
        ]
    }
    recorded_states(env, shown)
    [comment] = fake.ran("gh", "issue", "comment")
    assert comment.stdin is not None and b"attend un humain" in comment.stdin
    assert "#510 labeled needs-human" in err.getvalue()


# --- memory ------------------------------------------------------------------------


def test_the_stop_hook_keeps_the_end_of_the_last_message(tmp_path: Path) -> None:
    state = tmp_path / "7.jsonl"
    last = "x" * 3000 + f"\n{MEMORY_MARKER}"
    payload = {"hook_event_name": "Stop", "last_assistant_message": last}
    subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(state)],
        input=json.dumps(payload),
        text=True,
        check=True,
    )
    [line] = [json.loads(x) for x in state.read_text().splitlines()]
    event = HookEvent(line["event"], line["kind"], line["text"], line["at"])
    assert event.waits_for_memory


def test_the_primer_s_rule_names_the_marker_the_hook_reads() -> None:
    """One marker, the code's: the project file says `{memory_marker}`."""
    from tracerworks.primer import primer

    rule = "s'il manque de mémoire, termine ton tour par {memory_marker}"
    said = primer("o/r", 7, "t", "agent/7-x", "claude", [rule], resuming=False)
    assert f"termine ton tour par {MEMORY_MARKER}" in said


def test_the_state_file_starts_again_past_its_size(tmp_path: Path) -> None:
    from tracerworks import worker_hook

    state = tmp_path / "7.jsonl"
    state.write_text("x" * (worker_hook.ROTATE_BYTES + 1))
    worker_hook.record(state, '{"hook_event_name": "Stop"}', None, 1.0)
    [line] = state.read_text().splitlines()
    assert json.loads(line)["event"] == "Stop"
    assert (tmp_path / "7.jsonl.1").stat().st_size == worker_hook.ROTATE_BYTES + 1


def memory_world(tmp_path: Path, available_kb: int) -> tuple[Env, io.StringIO]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = recorded_settings(tmp_path)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: 8000000 kB\nMemAvailable: {available_kb} kB\n")
    settings = replace(settings, meminfo=meminfo)
    stop = {"event": "Stop", "text": f"…\n{MEMORY_MARKER}", "at": T0}
    path = hook_file(settings.cache_dir, 510)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(stop) + "\n")
    out = io.StringIO()
    return Env(settings, runner=recorded_world(), out=out, err=io.StringIO()), out


def test_a_worker_waiting_for_memory_goes_on_when_it_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracerworks import cli
    from tracerworks.observe import observe_workers

    told: list[tuple[int, str]] = []
    monkeypatch.setattr(cli, "deliver", lambda env, n, text: told.append((n, text)))
    env, out = memory_world(tmp_path, 1400 * 1024)  # 1.4 GB: short
    workers = observe_workers(env.runner, env.settings, env.tmux)
    assert [w.spec for w in workers if w.waits_for_memory] == [510]
    assert cli._memory_back(env, workers) is None
    assert told == []

    env, out = memory_world(tmp_path / "back", 1600 * 1024)  # 1.6 GB: back
    assert cli._memory_back(env, workers) == 510
    [(spec, text)] = told
    assert spec == 510 and "La mémoire est revenue : 1600 Mo" in text
    assert "#510: memory back (1600 MB available)" in out.getvalue()


#: worker-1771's file, 24-25/10 (#1810): its turn ends on the marker, Claude
#: Code says a minute later that it waits for input, then compacts it at rest.
COMPACTED = (FIXTURES / "hook_events" / "memory_wait_compacted.jsonl").read_text()
WORK_AGAIN = (
    {"event": "UserPromptSubmit", "text": "La mémoire est revenue", "at": T0},
    {"event": "PostToolUse", "at": T0},
)


def test_a_wait_for_memory_outlives_rest_and_compaction(tmp_path: Path) -> None:
    path = tmp_path / "1771.jsonl"
    path.write_text(COMPACTED)
    end = turn_end(path)
    assert end is not None and end.waits_for_memory
    word = last_word(path)
    assert word is not None and word.waits_for_memory  # what status reads


@pytest.mark.parametrize("again", WORK_AGAIN, ids=lambda e: str(e["event"]))
def test_a_prompt_or_a_tool_after_the_turn_ends_the_wait(
    tmp_path: Path, again: dict[str, object]
) -> None:
    path = tmp_path / "1771.jsonl"
    path.write_text(COMPACTED + json.dumps(again) + "\n")
    assert turn_end(path) is None
    word = last_word(path)
    assert word is not None and word.event == again["event"]


def test_a_compacted_turn_without_the_marker_does_not_wait(tmp_path: Path) -> None:
    path = tmp_path / "1771.jsonl"
    path.write_text(COMPACTED.replace(MEMORY_MARKER, "Fini."))
    end = turn_end(path)
    assert end is not None and not end.waits_for_memory


def test_a_session_start_with_no_turn_before_says_it_works(tmp_path: Path) -> None:
    """A resumed session's start, nothing ended since: its own word."""
    path = tmp_path / "1771.jsonl"
    start = {"event": "SessionStart", "kind": "resume", "at": T0 + 1}
    after = "".join(json.dumps(e) + "\n" for e in (WORK_AGAIN[1], start))
    path.write_text(COMPACTED + after)
    word = last_word(path)
    assert word is not None and word.event == "SessionStart"


def test_a_worker_compacted_while_it_waits_goes_on_when_memory_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker-1771: ten hours lost, the relauncher passing each minute."""
    from tracerworks import cli
    from tracerworks.observe import observe_workers

    told: list[tuple[int, str]] = []
    monkeypatch.setattr(cli, "deliver", lambda env, n, text: told.append((n, text)))
    env, _ = memory_world(tmp_path, 7000 * 1024)
    hook_file(env.settings.cache_dir, 510).write_text(COMPACTED)
    workers = observe_workers(env.runner, env.settings, env.tmux)
    assert [w.spec for w in workers if w.waits_for_memory] == [510]
    assert cli._memory_back(env, workers) == 510
    assert [spec for spec, _ in told] == [510]


def test_a_worker_waiting_for_memory_is_in_development_and_says_why() -> None:
    from tracerworks.chantier_state import Facts, derive
    from tracerworks.observe import WorkerState

    facts = Facts(worker=WorkerState.WAITING, waits_for_memory=True)
    assert derive(facts).describe() == "in development (waits for memory)"


# --- parked red ----------------------------------------------------------------------


RED = State(P.PARKED, "PR #720 red, not resumed yet", "parked: red")


def test_parked_red_past_its_deadline_alerts_once_then_restores(
    db: sqlite3.Connection,
) -> None:
    enter(db, 520, RED, T0)
    assert waits.stale_parked(db, REPO, [520], T0 + 119 * 60, 120) == {}
    stale = waits.stale_parked(db, REPO, [520], T0 + 120 * 60, 120)
    assert stale == {520: (float(T0), "PR #720 red, not resumed yet")}
    alarming, known = conditions(
        db, REPO, "main", "ci.yml", T0 + 125 * 60, operator="Sam",
        human_label="needs-human", blocked={}, stale_parked=stale,
    )  # fmt: skip
    assert "parked-red owner/repo" in known
    assert [c.text for c in alarming if c.key.startswith("parked-red")] == [
        "#520 parked for 125 min — PR #720 red, not resumed yet — owner/repo"
    ]
    # resumed: in development again, no more alert
    enter(db, 520, WORKING, T0 + 130 * 60)
    assert waits.stale_parked(db, REPO, [520], T0 + 131 * 60, 120) == {}


def test_a_parked_chantier_whose_ci_is_red_is_parked_not_in_a_check() -> None:
    from tracerworks.chantier_state import Facts, derive

    assert derive(Facts(parked_pull=720, ci_red=True)).describe() == (
        "parked (PR #720 red, not resumed yet)"
    )
