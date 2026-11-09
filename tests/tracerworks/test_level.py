"""The model and the effort a worker starts with (#2251, spec #2250).

Chosen on the spec by labels — the test project's `llm-big|small` and
`thinking-<effort>`, its default `big` · `high` — and passed as `--model` and
`--effort` at EVERY start: a launch, the resume of a branch, the resume of a
chantier parked on a red or set aside, the labels read again at that moment.
A label that cannot be followed leaves its dimension at the default, said
with the launch: the factory never stops for a label at night.
"""

from __future__ import annotations

import json
import shlex
from contextlib import closing
from pathlib import Path

import pytest

from tests.tracerworks.fakes import ROOT, TMUX, FakeRunner, recorded_world
from tests.tracerworks.test_clean import RED, answer_state
from tests.tracerworks.test_start_stop import (
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
    world,
)
from tracerworks.cleanup import usage_comment
from tracerworks.cli import EXIT_OK, run
from tracerworks.level import (
    DEFAULT,
    LABEL,
    Choice,
    LevelRules,
    choose,
    describe_view,
    record,
    recorded,
    view,
)
from tracerworks.recorder import comment_row
from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime
from tracerworks.usage import ChantierUsage, chantier_usage, project_dir

RULES = LevelRules(
    model="big",
    effort="high",
    models=("big", "small"),
    model_prefix="llm-",
    effort_prefix="thinking-",
)


# --- the choice, against the function ------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "model", "effort"),
    [
        ([], Choice("big", DEFAULT), Choice("high", DEFAULT)),
        (
            ["spec", "ci-github"],
            Choice("big", DEFAULT),
            Choice("high", DEFAULT),
        ),
        (
            ["thinking-xhigh", "llm-small"],
            Choice("small", LABEL, label="llm-small"),
            Choice("xhigh", LABEL, label="thinking-xhigh"),
        ),
        (
            ["thinking-low"],
            Choice("big", DEFAULT),
            Choice("low", LABEL, label="thinking-low"),
        ),
        (
            ["thinking-max", "thinking-max"],
            Choice("big", DEFAULT),
            Choice("max", LABEL, label="thinking-max"),
        ),
        (
            ["thinking-ultra", "llm-small"],
            Choice("small", LABEL, label="llm-small"),
            Choice("high", DEFAULT, anomaly="thinking-ultra unknown"),
        ),
        (
            ["thinking-xhigh", "thinking-medium"],
            Choice("big", DEFAULT),
            Choice(
                "high",
                DEFAULT,
                anomaly="thinking-medium and thinking-xhigh contradict each other",
            ),
        ),
        (
            ["llm-huge", "llm-small", "thinking-medium"],
            Choice("big", DEFAULT, anomaly="llm-huge unknown"),
            Choice("medium", LABEL, label="thinking-medium"),
        ),
    ],
)
def test_each_dimension_is_its_label_or_its_default_and_why(
    labels: list[str], model: Choice, effort: Choice
) -> None:
    level = choose(labels, RULES)
    assert (level.model, level.effort) == (model, effort)


def test_the_choice_is_said_with_where_it_comes_from() -> None:
    assert choose([], RULES).describe() == "model big (default), effort high (default)"
    assert choose(["thinking-xhigh", "thinking-ultra"], RULES).describe() == (
        "model big (default), effort high (default — thinking-ultra unknown)"
    )
    assert choose(["llm-small"], RULES).args() == [
        "--model", "small", "--effort", "high",
    ]  # fmt: skip


# --- a launch: the worker's command line --------------------------------------------


@pytest.fixture
def startable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real spawn, its token and its session guard answering."""
    monkeypatch.setattr(TmuxWorktreeRuntime, "launch_blocker", lambda self, p: None)
    monkeypatch.setattr(TmuxWorktreeRuntime, "guard_blocker", lambda self, s: None)


def labelled(fake: FakeRunner, spec: int, *labels: str) -> None:
    fake.answer(
        "gh", "issue", "view", str(spec), "--repo", "owner/repo", "--json", "labels",
        stdout=json.dumps({"labels": [{"name": n} for n in ("spec", *labels)]}),
    )  # fmt: skip


def command(fake: FakeRunner, window: str) -> list[str]:
    """The words the worker's window `exec`s."""
    [argv] = [
        c.argv
        for c in fake.ran(*TMUX, "new-window")
        if c.argv[c.argv.index("-n") + 1] == window
    ]
    return shlex.split(argv[-1].split("; exec ", 1)[1])


def level_of(words: list[str]) -> tuple[str, str]:
    assert words.count("--model") == 1 and words.count("--effort") == 1
    return words[words.index("--model") + 1], words[words.index("--effort") + 1]


def started(tmp_path: Path, *labels: str, remote: str = "") -> tuple[FakeRunner, str]:
    fake = world(tmp_path)
    issue_answer(fake, 600, "Spec — Console opérateur")
    git_repo_answers(fake, remote=remote)
    fake.answer(*TMUX, "new-window", prefix=True)
    labelled(fake, 600, *labels)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    assert run(env, ["start", "600"]) == EXIT_OK, err.getvalue()
    return fake, out.getvalue()


@pytest.mark.usefixtures("startable")
def test_a_spec_without_label_starts_at_the_project_s_default(tmp_path: Path) -> None:
    fake, out = started(tmp_path)

    assert level_of(command(fake, "worker-600")) == ("big", "high")
    assert (
        "worker-600 launched (claude; model big (default), effort high (default))"
        in out
    )


@pytest.mark.usefixtures("startable")
def test_a_spec_s_labels_choose_its_model_and_its_effort(tmp_path: Path) -> None:
    fake, out = started(tmp_path, "thinking-xhigh", "llm-small")

    assert level_of(command(fake, "worker-600")) == ("small", "xhigh")
    assert "model small (label llm-small), effort xhigh (label thinking-xhigh)" in out


@pytest.mark.usefixtures("startable")
@pytest.mark.parametrize(
    ("labels", "why"),
    [
        (("thinking-ultra",), "thinking-ultra unknown"),
        (
            ("thinking-medium", "thinking-xhigh"),
            "thinking-medium and thinking-xhigh contradict each other",
        ),
    ],
)
def test_a_label_that_cannot_be_followed_starts_at_the_default_and_says_why(
    tmp_path: Path, labels: tuple[str, ...], why: str
) -> None:
    fake, out = started(tmp_path, *labels)

    assert level_of(command(fake, "worker-600")) == ("big", "high")
    assert f"effort high (default — {why})" in out


@pytest.mark.usefixtures("startable")
def test_the_resume_of_an_existing_branch_starts_at_the_spec_s_level(
    tmp_path: Path,
) -> None:
    fake, _ = started(
        tmp_path,
        "thinking-medium",
        remote="abc123\trefs/heads/agent/600-console-operateur\n",
    )

    assert level_of(command(fake, "worker-600")) == ("big", "medium")


# --- a resume: the labels of now ---------------------------------------------------


def test_a_parked_red_resumes_with_the_label_put_during_its_pause(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#520 parked on PR #720 at the default; the operator puts
    `thinking-xhigh` on its spec; its red resumes it in xhigh."""
    from tests.tracerworks.test_parking import resume_world

    fake, env, out, err = resume_world(tmp_path)
    labelled(fake, 520, "thinking-xhigh")
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()

    words = command(fake, "worker-520")
    assert "--resume" in words
    assert level_of(words) == ("big", "xhigh")
    said = "#520: resumed with model big (default), effort xhigh (label thinking-xhigh)"
    assert said in out.getvalue()


def test_a_chantier_set_aside_resumes_with_the_labels_of_now(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    from tests.tracerworks.test_set_aside import set_aside_for_520

    fake, env, out, err = set_aside_for_520(tmp_path)
    labelled(fake, 501, "llm-small", "thinking-low")

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()

    words = command(fake, "worker-501")
    assert "--resume" in words
    assert level_of(words) == ("small", "low")
    assert "#501: resumed with model small (label llm-small)" in out.getvalue()


def test_a_parked_red_without_label_resumes_at_the_default(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """No label: the default — what an older session ran with is not kept."""
    from tests.tracerworks.test_parking import resume_world

    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert level_of(command(fake, "worker-520")) == ("big", "high")


# --- seen: what the session's log says it sent (#2252) -------------------------------
#
# The format measured on 03/11 in the VPS's workers' logs, anonymous: each
# request is an `api-request-blob` line, its effort under
# `message.output_config` (the first request of a session has none); each
# answer an `assistant` line naming its `message.model`.


def _blob(effort: str | None) -> str:
    message: dict[str, object] = {"role": "system", "content": [{"type": "text"}]}
    if effort is not None:
        message["output_config"] = {"effort": effort}
    return json.dumps(
        {"type": "api-request-blob", "sessionId": "s", "hash": "h", "message": message}
    )


def _answer(model: str, stamp: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": stamp,
            "message": {"id": stamp, "model": model, "usage": {"output_tokens": 1}},
        }
    )


def session_logs(
    root: Path,
    worktree: Path,
    lines: list[str],
    subagent: list[str] | None = None,
    name: str = "0f1e2d3c-4b5a-4968-8776-655443322110",
) -> None:
    folder = project_dir(root, worktree)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if subagent is not None:
        agents = folder / name / "subagents"
        agents.mkdir(parents=True)
        (agents / "agent-a1.jsonl").write_text("\n".join(subagent) + "\n")


WORKTREE = Path("/home/dev/.agent-worktrees/601")
MAIN = [
    json.dumps({"type": "user", "timestamp": "2026-11-03T08:00:00Z"}),
    _blob(None),
    _blob("medium"),
    _answer("claude-opus-5-5", "2026-11-03T08:00:05Z"),
    _blob("high"),
    _answer("claude-opus-5-5", "2026-11-03T08:01:00Z"),
    _answer("<synthetic>", "2026-11-03T08:01:30Z"),
]
#: a subagent, newer than the worker's last line: never the worker's level
SUBAGENT = [_blob("low"), _answer("claude-haiku-5-5", "2026-11-03T09:00:00Z")]


def test_the_model_and_the_effort_seen_are_the_session_s_last(tmp_path: Path) -> None:
    session_logs(tmp_path, WORKTREE, MAIN, subagent=SUBAGENT)

    usage = chantier_usage(tmp_path, WORKTREE)

    assert usage is not None and usage.seen() == ("claude-opus-5-5", "high")


def test_the_newest_session_is_the_one_seen(tmp_path: Path) -> None:
    session_logs(tmp_path, WORKTREE, MAIN)
    later = [_blob("xhigh"), _answer("claude-sonnet-5-5", "2026-11-04T08:00:00Z")]
    session_logs(tmp_path, WORKTREE, later, name="11111111-2222-4333-8444-5555")

    usage = chantier_usage(tmp_path, WORKTREE)

    assert usage is not None and usage.seen() == ("claude-sonnet-5-5", "xhigh")


def test_a_log_without_request_has_nothing_seen(tmp_path: Path) -> None:
    session_logs(
        tmp_path, WORKTREE, [json.dumps({"type": "user", "timestamp": "2026-11-03Z"})]
    )
    usage = chantier_usage(tmp_path, WORKTREE)
    assert usage is not None and usage.seen() == (None, None)


ASKED = choose([], RULES)  # big · high, the default


@pytest.mark.parametrize(
    ("seen", "model_differs", "effort_differs", "said"),
    [
        (("claude-big-5-5", "high"), False, False, "big · high"),
        (
            ("claude-big-5-5", "medium"), False, True,
            "big · high asked — MISMATCH: claude-big-5-5 · medium sent",
        ),
        (
            ("claude-small-5-5", "high"), True, False,
            "big · high asked — MISMATCH: claude-small-5-5 · high sent",
        ),
        ((None, None), None, None, "big · high (sent: ? · ?)"),
    ],
)  # fmt: skip
def test_asked_and_seen_differ_only_when_both_are_known(
    seen: tuple[str | None, str | None],
    model_differs: bool | None,
    effort_differs: bool | None,
    said: str,
) -> None:
    shown = view(ASKED, seen)
    assert shown["model"]["differs"] is model_differs
    assert shown["effort"]["differs"] is effort_differs
    assert describe_view(shown) == said


def test_nothing_asked_nor_seen_says_nothing_and_an_anomaly_is_said() -> None:
    assert describe_view(view(None, (None, None))) is None
    assert describe_view(view(None, ("claude-big-5-5", "medium"))) == (
        "claude-big-5-5 · medium sent (asked unknown)"
    )
    odd = view(choose(["thinking-ultra"], RULES), ("claude-big-5-5", "high"))
    assert odd["effort"]["anomaly"] == "thinking-ultra unknown"
    assert describe_view(odd) == "big · high — thinking-ultra unknown"


@pytest.mark.usefixtures("startable")
def test_a_launch_records_what_it_asked(tmp_path: Path) -> None:
    started(tmp_path, "thinking-xhigh")
    assert recorded(tmp_path / "cache", 600) == choose(["thinking-xhigh"], RULES)


def test_a_resume_records_what_it_asked(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    from tests.tracerworks.test_parking import resume_world

    fake, env, _, err = resume_world(tmp_path)
    record(env.settings.cache_dir, 520, choose([], RULES))
    labelled(fake, 520, "thinking-low")
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert recorded(env.settings.cache_dir, 520) == choose(["thinking-low"], RULES)


# --- status and the dashboard ------------------------------------------------------


def seen_501(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str) -> Path:
    """#501 started at the default; its log says `effort` was sent — where
    `status` reads it (`CLAUDE_CONFIG_DIR`) and the snapshot test's root."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    lines = [_blob(effort), _answer("claude-big-5-5", "2026-11-03T08:00:05Z")]
    session_logs(tmp_path / "projects", ROOT / "501", lines, subagent=SUBAGENT)
    record(tmp_path / "cache", 501, choose([], RULES))
    return tmp_path / "projects"


@pytest.mark.parametrize(("effort", "differs"), [("high", False), ("medium", True)])
def test_status_json_says_each_worker_s_level_asked_and_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str, differs: bool
) -> None:
    from tests.tracerworks.test_status import env_for

    seen_501(tmp_path, monkeypatch, effort)
    env, out, _ = env_for(recorded_world(), tmp_path)

    assert run(env, ["status", "--json"]) == EXIT_OK

    [worker] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 501]
    assert worker["model"] == {
        "asked": "big", "source": "default", "label": None, "anomaly": None,
        "seen": "claude-big-5-5", "differs": False,
    }  # fmt: skip
    assert worker["effort"] == {
        "asked": "high", "source": "default", "label": None, "anomaly": None,
        "seen": effort, "differs": differs,
    }  # fmt: skip


def test_status_text_marks_the_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.tracerworks.test_status import env_for

    seen_501(tmp_path, monkeypatch, "medium")
    env, out, _ = env_for(recorded_world(), tmp_path)

    assert run(env, ["status"]) == EXIT_OK

    first = out.getvalue().splitlines()[0]
    assert first.startswith(
        "#501 console-operateur — claude — big · high asked"
        " — MISMATCH: claude-big-5-5 · medium sent — in development"
    )


def test_the_dashboard_s_worker_says_its_level_asked_and_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.tracerworks.test_snapshot import github_world, snapshot, workers_of

    seen_501(tmp_path, monkeypatch, "medium")
    workers = workers_of(snapshot(github_world(recorded_world()), tmp_path))

    worker = workers[501]
    assert (worker["model"]["asked"], worker["model"]["seen"]) == (
        "big",
        "claude-big-5-5",
    )
    assert worker["effort"]["asked"] == "high" and worker["effort"]["seen"] == "medium"
    assert worker["effort"]["differs"] is True
    assert workers[510]["effort"]["seen"] is None  # never ran a Claude session


# --- a finished chantier keeps its level (#2253) ------------------------------------


def _finished(tmp_path: Path) -> ChantierUsage:
    session_logs(tmp_path / "logs", WORKTREE, MAIN, subagent=SUBAGENT)
    usage = chantier_usage(tmp_path / "logs", WORKTREE)
    assert usage is not None
    return usage


def test_the_usage_comment_names_the_level_seen_and_is_read_back(
    tmp_path: Path,
) -> None:
    body = usage_comment(601, _finished(tmp_path), 1024)

    assert (
        "\nModèle · effort : claude-opus-5-5 · high (constatés dans le journal de la"
        " dernière session du worker).\n" in body
    )
    row = comment_row("owner/repo", 601, body, 1790413800)
    assert row is not None and (row["model"], row["effort"]) == (
        "claude-opus-5-5",
        "high",
    )
    unknown = body.replace("claude-opus-5-5 · high", "inconnu · inconnu")
    row = comment_row("owner/repo", 601, unknown, 1790413800)
    assert row is not None and (row["model"], row["effort"]) == (None, None)


def test_a_base_of_before_gains_the_columns_and_a_finished_chantier_fills_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from tests.tracerworks.fakes import recorded_settings
    from tests.tracerworks.test_store import at_version
    from tracerworks import store
    from tracerworks.recorder import record_chantier

    path = tmp_path / "factory.sqlite3"
    old = {
        "project": "owner/repo", "spec": 500, "finished_at": 1, "duration_s": 2,
        "tokens_total": 3, "tokens_input": 0, "tokens_cache_write": 0,
        "tokens_cache_read": 0, "tokens_output": 3, "messages": 1, "source": "clean",
    }  # fmt: skip
    with closing(at_version(path, 30, monkeypatch)) as db:
        store.insert(db, "chantier", [old])
    settings = replace(recorded_settings(tmp_path), history_db=path)

    assert record_chantier(settings, 601, _finished(tmp_path), 1790413800)

    with closing(store.connect(path)) as db:
        assert store.version(db) == store.SCHEMA_VERSION >= 31
        assert db.execute(
            "SELECT spec, tokens_total, model, effort FROM chantier ORDER BY spec"
        ).fetchall() == [(500, 3, None, None), (601, 4, "claude-opus-5-5", "high")]


# --- after the review ----------------------------------------------------------------


def test_the_relauncher_s_history_still_reads_a_launch_naming_its_level(
    tmp_path: Path,
) -> None:
    """The journal's « launched » line names the level since #2251: the
    dashboard's history must still see the launch."""
    from tracerworks import texts
    from tracerworks.history import launches

    journal = tmp_path / "next.log"
    line = texts.LAUNCHED.format(
        window="worker-620", provider="claude", level=choose([], RULES).describe()
    )
    journal.write_text(f"--- 2026-09-26T03:05:00+00:00\n{line}\n", encoding="utf-8")

    found = launches(journal, lambda w: int(w.split("-")[1]))

    assert found is not None and [spec for spec, _ in found] == [620]


def test_a_resume_whose_labels_cannot_be_read_starts_at_the_default_said(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    from tests.tracerworks.test_parking import resume_world

    fake, env, out, err = resume_world(tmp_path)
    fake.answer(
        "gh", "issue", "view", "520", "--repo", "owner/repo", "--json", "labels",
        returncode=1, stdout="HTTP 502",
    )  # fmt: skip
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()

    assert level_of(command(fake, "worker-520")) == ("big", "high")
    assert "labels unreadable" in out.getvalue()


def test_an_alias_with_a_context_suffix_is_its_model() -> None:
    from tracerworks.level import same_model

    assert same_model("opus[1m]", "claude-opus-5-5")
    assert not same_model("sonnet", "claude-opus-5-5")
