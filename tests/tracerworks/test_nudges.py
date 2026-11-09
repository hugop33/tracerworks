"""A worker that asks a permission it need not ask is nudged (#1789).

Its turn's end, as its `Stop` hook kept it, is a fixture (`turn_ends/`):
a demand of leave after a red of the machine is nudged once it has waited
20 min; a report of work, a decision of the operator's, a worker held by
anything else than its rest are not. One nudge an hour at most, and the
third without a commit nor a push puts the human label instead. The clock
is the test's.
"""

from __future__ import annotations

import io
import json
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FIXTURES, ROOT, recorded_settings, recorded_world
from tracerworks import chantier_state, nudges, store
from tracerworks.chantier_state import Phase, State
from tracerworks.cli import Env
from tracerworks.hook_state import MEMORY_MARKER, hook_file, turn_end
from tracerworks.nudges import Gesture, Seen, Sent
from tracerworks.process import Result
from tracerworks.settings import Waits, load, parse

REPO = "owner/repo"
T0 = 1_790_000_000  # when the worker came to rest
MIN = 60


#: The patterns proved here (`fixtures/tracerworks/waits.toml`): a project file's
#: own are pinned to them in `tests/tracerworks_project`.
def _proved_waits() -> Waits:
    raw = tomllib.loads((FIXTURES / "project.toml").read_text(encoding="utf-8"))
    patterns = tomllib.loads((FIXTURES / "waits.toml").read_text(encoding="utf-8"))
    raw["waits"].update(patterns["waits"])
    return parse(raw, FIXTURES / "project.toml", {}).waits


WAITS = _proved_waits()
ASKS, DECISIONS = WAITS.permission_ask, WAITS.decision


#: The pass's world: its env, what it said, its `gh issue` calls, its clock.
World = tuple[Env, io.StringIO, list[str], list[float]]


def turn(name: str) -> str:
    return (FIXTURES / "turn_ends" / f"{name}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name", ["push-after-memory-red", "rerun-after-lost-runner", "english-may-i"]
)
def test_a_demand_of_leave_asks_permission(name: str) -> None:
    assert nudges.asks_permission(turn(name), ASKS, DECISIONS)


@pytest.mark.parametrize(
    "name", ["report-of-work", "decision-production", "decision-secret"]
)
def test_a_report_or_a_decision_of_the_operator_s_does_not(name: str) -> None:
    assert not nudges.asks_permission(turn(name), ASKS, DECISIONS)


def test_a_decision_word_holds_back_a_demand_that_would_be_nudged() -> None:
    """Counter-proof of the decision rule: the same demand, without its
    decision words, is nudged."""
    words = turn("decision-production")
    assert nudges.asks_permission(words, ASKS, ())
    assert not nudges.asks_permission(words, ASKS, DECISIONS)


def seen(
    *, cause: str = nudges.AT_REST, words: str | None = None, progress: str = "a:a"
) -> Seen:
    return Seen(
        spec=510,
        cause=cause,
        rested_at=T0,
        last_words=turn("push-after-memory-red") if words is None else words,
        progress=progress,
    )


def decide(s: Seen, before: list[Sent], now: int) -> Gesture:
    return nudges.decide(
        s, before, now, after_minutes=20, asks=ASKS, decisions=DECISIONS
    )


def test_the_nudge_leaves_at_20_min_not_before() -> None:
    assert decide(seen(), [], T0 + 19 * MIN) is Gesture.NONE
    assert decide(seen(), [], T0 + 20 * MIN) is Gesture.NUDGE


@pytest.mark.parametrize(
    "cause",
    [
        "human label",  # needs-human on its spec
        "worker at rest, its CI runs",
        "worker: waits for memory",
        "merge queue: waiting",
        "worker: working",
    ],
)
def test_only_a_worker_at_rest_in_development_is_nudged(cause: str) -> None:
    assert decide(seen(cause=cause), [], T0 + 30 * MIN) is Gesture.NONE


def test_a_report_of_work_is_never_nudged() -> None:
    words = turn("report-of-work")
    assert decide(seen(words=words), [], T0 + 3 * 60 * MIN) is Gesture.NONE


def test_one_nudge_an_hour_at_most() -> None:
    first = Sent(T0 + 20 * MIN, "a:a", "sent")
    assert decide(seen(), [first], T0 + 79 * MIN) is Gesture.NONE
    assert decide(seen(), [first], T0 + 80 * MIN) is Gesture.NUDGE


def test_the_third_without_progress_puts_the_label_instead() -> None:
    before = [Sent(T0, "a:a", "sent"), Sent(T0 + 60 * MIN, "a:a", "sent")]
    assert decide(seen(), before, T0 + 2 * 60 * MIN) is Gesture.LABEL


def test_a_commit_or_a_push_since_starts_the_count_again() -> None:
    """Counter-proof of the count: the same two nudges, the worktree moved."""
    before = [Sent(T0, "a:a", "sent"), Sent(T0 + 60 * MIN, "a:a", "sent")]
    pushed = seen(progress="a:b")  # its upstream has the commit now
    assert decide(pushed, before, T0 + 2 * 60 * MIN) is Gesture.NUDGE


def test_an_unsaved_report_in_the_merge_queue_is_nudged_without_permission_words() -> (
    None
):
    report = Seen(
        2102, "merge queue: waiting", T0, "Ma PR est prête.", "a:a", unsaved=True
    )
    assert decide(report, [], T0 + 19 * MIN) is Gesture.NONE
    assert decide(report, [], T0 + 20 * MIN) is Gesture.NUDGE
    before = [Sent(T0 + 20 * MIN, "a:a", "sent")]
    assert decide(report, before, T0 + 30 * MIN) is Gesture.NONE


def test_unsaved_work_does_not_override_a_real_decision() -> None:
    report = Seen(
        2102,
        "merge queue: waiting",
        T0,
        turn("decision-production"),
        "a:a",
        unsaved=True,
    )
    assert decide(report, [], T0 + 3 * 60 * MIN) is Gesture.NONE


# --- the worker's turn end ----------------------------------------------------------


def write_events(path: Path, *events: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def test_the_turn_end_stays_its_word_under_waiting_for_input(tmp_path: Path) -> None:
    """Claude Code says « waiting for your input » a minute after a turn: the
    turn's last message is still what the worker said."""
    path = tmp_path / "h.jsonl"
    write_events(
        path,
        {"event": "Stop", "text": "Dis-moi de relancer le push.", "at": T0},
        {"event": "Notification", "kind": "idle_prompt", "text": "…", "at": T0 + 60},
    )
    end = turn_end(path)
    assert end is not None and end.at == T0 and "relancer" in (end.text or "")


def test_a_turn_end_followed_by_work_is_none(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    write_events(
        path,
        {"event": "Stop", "text": "Dis-moi de relancer le push.", "at": T0},
        {"event": "UserPromptSubmit", "text": "continue", "at": T0 + 60},
    )
    assert turn_end(path) is None


# --- the relauncher's pass ----------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """Worker #510 at rest in its window, its turn ended at T0 on a demand."""
    settings = recorded_settings(tmp_path)
    fake = recorded_world()
    fake.answer("git", "-C", str(ROOT / "510"), "rev-parse", prefix=True, stdout="c1\n")
    gh: list[str] = []

    def answer_gh(call: Sequence[str], stdin: bytes | None) -> Result:
        gh.append(" ".join(call[1:4]))
        return Result(0, "", "")

    fake.on("gh", "issue", prefix=True)(answer_gh)
    write_events(
        hook_file(settings.cache_dir, 510),
        {"event": "Stop", "text": turn("push-after-memory-red"), "at": T0},
    )
    with closing(store.connect(settings.history_db)) as db:
        at_rest = State(Phase.DEVELOPMENT, "at rest", nudges.AT_REST)
        chantier_state.record(db, settings.github_repo, 510, at_rest, 1, T0)
    out = io.StringIO()
    clock = [float(T0 + 20 * MIN)]
    env = Env(settings, runner=fake, out=out, err=io.StringIO(), now=lambda: clock[0])
    yield env, out, gh, clock


def run_pass(env: Env, monkeypatch: pytest.MonkeyPatch, told: list[int]) -> list[int]:
    from tracerworks import cli
    from tracerworks.observe import observe_workers

    monkeypatch.setattr(cli, "deliver", lambda env, n, text: told.append(n))
    return cli._nudge_askers(env, observe_workers(env.runner, env.settings, env.tmux))


def nudge_rows(env: Env) -> list[tuple[int, str]]:
    with closing(store.connect(env.settings.history_db)) as db:
        return [
            (int(at), str(outcome))
            for at, outcome in db.execute("SELECT at, outcome FROM nudge ORDER BY at")
        ]


def test_the_pass_nudges_once_then_labels_the_third(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, out, gh, clock = world
    told: list[int] = []
    assert run_pass(env, monkeypatch, told) == [510]
    assert told == [510]
    assert "#510: asked a permission it need not ask — nudged (1/2)" in out.getvalue()

    clock[0] += 30 * MIN  # within the hour: nothing
    assert run_pass(env, monkeypatch, told) == []

    clock[0] += 30 * MIN  # an hour since: the second
    assert run_pass(env, monkeypatch, told) == [510]
    clock[0] += 60 * MIN  # the third, no commit nor push: the label
    assert run_pass(env, monkeypatch, told) == [510]
    assert told == [510, 510]
    assert gh == ["issue edit 510", "issue comment 510"]
    assert [o for _, o in nudge_rows(env)] == ["sent", "sent", "needs_human"]


def test_status_says_the_last_nudge(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracerworks import cli

    env, _, _, _ = world
    run_pass(env, monkeypatch, [])
    with closing(store.connect(env.settings.history_db)) as db:
        said = cli.nudge_words(db, env.settings.github_repo, [510, 501])
    assert list(said) == [510]  # #501 never nudged: nothing said
    assert said[510].startswith("nudged ") and "(1/2 without progress)" in said[510]


def test_a_worker_whose_turn_ended_on_a_report_is_not_nudged(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _, _, _ = world
    write_events(
        hook_file(env.settings.cache_dir, 510),
        {"event": "Stop", "text": turn("report-of-work"), "at": T0},
    )
    told: list[int] = []
    assert run_pass(env, monkeypatch, told) == [] and told == []


@pytest.fixture
def unfinished_world(world: World) -> World:
    from tests.tracerworks.fakes import FakeRunner

    env, _, _, _ = world
    assert isinstance(env.runner, FakeRunner)
    env.runner.answer(
        "gh",
        "pr",
        "list",
        "--repo",
        REPO,
        "--head",
        prefix=True,
        stdout='[{"number": 710}]',
    )
    env.runner.answer("gh", "issue", "list", prefix=True, stdout="[]")
    env.runner.answer(
        "tmux",
        "capture-pane",
        "-p",
        "-t",
        f"={env.settings.tmux_session}:=worker-510",
        stdout="\u276f\n────────────\n",
    )
    write_events(
        hook_file(env.settings.cache_dir, 510),
        {"event": "Stop", "text": "Ma PR est prête.", "at": T0},
    )
    with closing(store.connect(env.settings.history_db)) as db:
        chantier_state.record(
            db,
            REPO,
            510,
            State(Phase.MERGE_QUEUE, None, "merge queue: waiting"),
            None,
            T0 + 1,
        )
    return world


@pytest.mark.parametrize("words", ["Ma PR est prête.", turn("push-after-memory-red")])
def test_the_pass_recovers_unsaved_work_hidden_in_the_merge_queue(
    unfinished_world: World, monkeypatch: pytest.MonkeyPatch, words: str
) -> None:
    from tracerworks import cli
    from tracerworks.observe import observe_workers

    env, _, _, _ = unfinished_world
    write_events(
        hook_file(env.settings.cache_dir, 510),
        {"event": "Stop", "text": words, "at": T0},
    )
    messages: list[str] = []
    monkeypatch.setattr(cli, "deliver", lambda env, n, text: messages.append(text))
    workers = observe_workers(env.runner, env.settings, env.tmux)
    assert cli._nudge_askers(env, workers) == [510]
    assert len(messages) == 1 and "non commité" in messages[0]
    assert cli._nudge_askers(env, workers) == []


@pytest.mark.parametrize(
    "hold",
    [
        "working",
        "asking",
        "dead",
        "no window",
        "unknown Git",
        "all pushed",
        "field taken",
        "field unknown",
        "memory",
        "needs human",
        "no open PR",
    ],
)
def test_unsaved_work_never_overrides_a_hold(
    unfinished_world: World, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    from dataclasses import replace

    from tests.tracerworks.fakes import FakeRunner
    from tracerworks import cli
    from tracerworks.git import Saved
    from tracerworks.observe import WorkerState, observe_workers

    env, _, _, _ = unfinished_world
    assert isinstance(env.runner, FakeRunner)
    worker = next(
        w for w in observe_workers(env.runner, env.settings, env.tmux) if w.spec == 510
    )
    states = {
        "working": WorkerState.WORKING,
        "asking": WorkerState.ASKING,
        "dead": WorkerState.DEAD,
    }
    if hold in states:
        worker = replace(worker, state=states[hold])
    elif hold == "no window":
        worker = replace(worker, window=None)
    elif hold == "unknown Git":
        worker = replace(worker, saved=None)
    elif hold == "all pushed":
        worker = replace(worker, saved=Saved("origin/branch", 0, 0))
    elif hold == "field taken":
        worker = replace(worker, pending="texte de Sam")
    elif hold == "field unknown":
        env.runner.answer("tmux", "capture-pane", prefix=True, returncode=1)
    elif hold == "memory":
        worker = replace(worker, waits_for_memory=True)
    elif hold == "needs human":
        payload = [
            {
                "number": 511,
                "title": "Ticket",
                "body": "Parent : #510",
                "labels": [{"name": env.settings.human_label}],
                "assignees": [],
            }
        ]
        env.runner.answer(
            "gh", "issue", "list", prefix=True, stdout=json.dumps(payload)
        )
    elif hold == "no open PR":
        env.runner.answer("gh", "pr", "list", prefix=True, stdout="[]")
    messages: list[int] = []
    monkeypatch.setattr(cli, "deliver", lambda env, n, text: messages.append(n))
    assert cli._nudge_askers(env, [worker]) == [] and messages == []


def test_a_worker_waiting_for_memory_is_seen_under_waiting_for_input(
    tmp_path: Path,
) -> None:
    """The memory marker (#1730) survives Claude Code's « waiting for your
    input », a minute after the turn: read from the turn's end too."""
    from tracerworks.observe import observe_workers

    settings = recorded_settings(tmp_path)
    write_events(
        hook_file(settings.cache_dir, 510),
        {"event": "Stop", "text": f"…\n{MEMORY_MARKER}", "at": T0},
        {"event": "Notification", "kind": "idle_prompt", "text": "…", "at": T0 + 60},
    )
    env = Env(settings, runner=recorded_world(), out=io.StringIO(), err=io.StringIO())
    workers = observe_workers(env.runner, env.settings, env.tmux)
    assert [w.spec for w in workers if w.waits_for_memory] == [510]


def test_a_pattern_that_does_not_compile_is_refused_at_load(tmp_path: Path) -> None:
    from tracerworks.settings import ConfigError

    source = FIXTURES / "project.toml"
    text = source.read_text(encoding="utf-8").replace(
        "permission_ask_minutes = 20", "permission_ask_minutes = 20\n#", 1
    )
    broken = tmp_path / "p.toml"
    broken.write_text(
        text.replace("permission_ask = [", 'permission_ask = [\n    "(",', 1)
    )
    with pytest.raises(ConfigError, match="no regular expression"):
        load(str(broken))


def test_the_workshop_card_says_the_last_nudge(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracerworks.cli import recorded_states

    env, _, _, _ = world
    run_pass(env, monkeypatch, [])
    shown = {
        "workers": [
            {"spec": 510, "phase": "development", "phase_reason": "at rest",
             "phase_cause": nudges.AT_REST, "lane": 1}
        ]
    }  # fmt: skip
    [card] = recorded_states(env, shown)["workers"]
    assert "(1/2 without progress)" in card["nudge"]


def test_a_comment_that_fails_after_the_label_never_labels_twice(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label on is recorded at once: once the operator takes it off, the
    next pass nudges again — not a second label for the same three."""
    from tracerworks.process import Result

    env, out, _, clock = world
    env.runner.on("gh", "issue", "comment", prefix=True)(  # type: ignore[attr-defined]
        lambda call, stdin: Result(1, "", "HTTP 502")
    )
    told: list[int] = []
    for _ in range(3):
        run_pass(env, monkeypatch, told)
        clock[0] += 60 * MIN
    assert [o for _, o in nudge_rows(env)] == ["sent", "sent", "needs_human"]
    assert "its nudge NOT sent" in out.getvalue()  # the comment's failure, said
    run_pass(env, monkeypatch, told)  # the label off: a nudge, not a label
    assert [o for _, o in nudge_rows(env)][-1] == "sent"
