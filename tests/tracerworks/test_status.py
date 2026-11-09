"""`tracerworks status`: the state of the workers rebuilt without a database (#566).

Everything here reads recorded `gh`/`git`/`tmux` outputs (`tracerworks_fakes`):
the claim under test is that the conventions alone — window `worker-<n>`,
worktree `<root>/<n>`, branch `agent/<n>-<slug>` — are enough to say who is
working, waiting, stopped or dead, and what would be lost.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.tracerworks.fakes import (
    ROOT,
    TMUX,
    FakeRunner,
    fixture,
    recorded_settings,
    recorded_world,
    target,
)
from tracerworks.cli import EXIT_BROKEN, EXIT_OK, Env, run
from tracerworks.conventions import Conventions, slugify
from tracerworks.disk import describe_size, size_kb
from tracerworks.git import parse_status
from tracerworks.observe import (
    WorkerState,
    asking_specs,
    classify,
    input_field,
    is_busy,
    observe_workers,
    pending_field,
    permission_request,
    resting_specs,
)
from tracerworks.providers import CLAUDE
from tracerworks.runtime.tmux import WINDOW_FORMAT, Tmux, Window

NOW = 1790413800.0
UNKNOWN_LEVEL = {
    "asked": None,
    "source": None,
    "label": None,
    "anomaly": None,
    "seen": None,
    "differs": None,
}
C = Conventions("worker-", "agent/", ROOT)
PROMPT = CLAUDE.prompt_glyph


def env_for(fake: FakeRunner, tmp_path: Path) -> tuple[Env, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    env = Env(
        recorded_settings(tmp_path),
        runner=fake,
        now=lambda: NOW,
        sleep=lambda _: None,
        stdin=io.BytesIO(),
        out=out,
        err=err,
    )
    return env, out, err


# --- conventions -----------------------------------------------------------


def test_the_three_names_of_a_chantier_round_trip() -> None:
    assert C.window_name(501) == "worker-501"
    assert C.spec_of_window("worker-501") == 501
    assert C.worktree_path(501) == ROOT / "501"
    assert C.spec_of_worktree(ROOT / "501") == 501
    assert C.branch_name(501, "console-operateur") == "agent/501-console-operateur"
    assert C.parse_branch("agent/501-console-operateur") == (501, "console-operateur")


def test_the_names_follow_the_project_file_not_the_code() -> None:
    other = Conventions("w.", "feat/agents/", Path("/srv/wt"))
    assert other.window_name(7) == "w.7" and other.spec_of_window("w.7") == 7
    assert other.spec_of_window("worker-7") is None  # « . » is not a wildcard
    assert other.spec_of_window("wx7") is None
    assert other.branch_pattern(7) == "feat/agents/7-*"
    assert other.parse_branch("feat/agents/7-x") == (7, "x")
    assert other.parse_branch("agent/7-x") is None


@pytest.mark.parametrize(
    "name",
    ["0", "claude", "factory", "worker-", "worker-0", "worker-501x", "worker-05"],
)
def test_a_window_outside_the_convention_is_not_a_worker(name: str) -> None:
    assert C.spec_of_window(name) is None


def test_a_worktree_outside_the_root_is_not_a_worker() -> None:
    assert (
        C.spec_of_worktree(Path("/home/dev/dev/widgets/.claude/worktrees/501")) is None
    )
    assert C.spec_of_worktree(ROOT / "501" / "nested") is None
    assert C.spec_of_worktree(ROOT / "abc") is None


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Spec — Chantier 10 : la page Widgets", "chantier-10-la-page-widgets"),
        ("Spec : Console opérateur", "console-operateur"),
        (
            "Factory V1 : tracerworks, une CLI sans état pour piloter deux workers",
            "factory-v1-tracerworks-une-cli-sans-etat",
        ),
        ("« !!! »", "spec"),
    ],
)
def test_a_slug_is_short_ascii_and_never_empty(title: str, slug: str) -> None:
    assert slugify(title) == slug
    assert len(slug) <= 40
    assert C.parse_branch(C.branch_name(1, slug)) == (1, slug)


# --- reading a pane and a worktree ----------------------------------------


def test_busy_is_read_from_the_status_line_not_from_the_conversation() -> None:
    assert is_busy(fixture("pane-claude-working.txt"), CLAUDE)
    # The idle pane QUOTES « esc to interrupt » in the conversation above.
    assert "esc to interrupt" in fixture("pane-claude-idle.txt")
    assert not is_busy(fixture("pane-claude-idle.txt"), CLAUDE)


def test_the_input_field_is_read_with_its_non_breaking_space() -> None:
    assert input_field(f"{PROMPT}\xa0\n", CLAUDE) == ""
    assert (
        input_field(f"{PROMPT}\xa0Message de Sam (m-1a2b)\n", CLAUDE)
        == "Message de Sam (m-1a2b)"
    )
    assert input_field("no prompt here\n", CLAUDE) is None


def test_git_status_counts_unpushed_commits_and_every_unsaved_path() -> None:
    assert parse_status(fixture("status-510.txt")) == (
        "origin/agent/510-classification",
        2,
        2,
    )
    assert parse_status(fixture("status-520.txt")) == (None, None, 0)


# --- the rebuilt state ------------------------------------------------------


def test_each_worker_state_is_deduced_from_the_world(tmp_path: Path) -> None:
    fake = recorded_world()
    settings = recorded_settings(tmp_path)
    workers = {
        w.spec: w for w in observe_workers(fake, settings, Tmux(fake, "widgets", None))
    }

    assert sorted(workers) == [501, 510, 520, 530]  # the operator's are not workers
    assert workers[501].state is WorkerState.WORKING
    assert workers[510].state is WorkerState.WAITING
    assert workers[520].state is WorkerState.STOPPED
    assert workers[530].state is WorkerState.DEAD

    assert workers[501].saved is not None and workers[501].saved.everything_pushed
    saved_510 = workers[510].saved
    assert saved_510 is not None and (saved_510.unpushed, saved_510.dirty) == (2, 2)
    saved_520 = workers[520].saved
    assert saved_520 is not None and (saved_520.upstream, saved_520.unpushed) == (
        None,
        3,
    )
    assert workers[520].last_activity == 1790400000  # the last commit, no window
    assert workers[530].worktree is None and workers[530].saved is None
    assert workers[501].slug == "console-operateur"
    assert workers[501].provider == "claude"


def test_the_resting_specs_are_the_workers_waiting_in_their_window(
    tmp_path: Path,
) -> None:
    fake = recorded_world()
    settings = recorded_settings(tmp_path)
    # 501 is working, 520 has no window, 530's agent is gone, the operator's windows
    # are not workers
    assert resting_specs(settings, Tmux(fake, "widgets", None)) == {510}


@pytest.mark.parametrize(
    "missing",
    [
        "no server running on /tmp/tmux-1001/default",
        "can't find session: widgets",
        "error connecting to /tmp/tmux-1001/default (No such file or directory)",
        # A SERVER DYING WHILE READ (#2221): `kill-server` returned, the
        # server accepted the client and went.
        "server exited unexpectedly",
        "server exited",
    ],
)
def test_status_after_a_tmux_restart_shows_every_worker_stopped(
    tmp_path: Path, missing: str
) -> None:
    """No server, no session: nothing to reconcile, the worktrees are stopped."""
    fake = recorded_world()
    fake.answer(
        *TMUX,
        "list-windows",
        "-t",
        "=widgets",
        "-F",
        WINDOW_FORMAT,
        returncode=1,
        stdout=missing,
    )
    for n in (501, 510):
        fake.answer(
            "git",
            "-C",
            str(ROOT / str(n)),
            "log",
            "-1",
            "--format=%ct",
            stdout="1790400000\n",
        )
    env, out, _ = env_for(fake, tmp_path)

    assert run(env, ["status", "--json"]) == EXIT_OK

    report = json.loads(out.getvalue())
    assert [(w["spec"], w["state"]) for w in report["workers"]] == [
        (501, "stopped"),
        (510, "stopped"),
        (520, "stopped"),
    ]
    assert not fake.ran(*TMUX, "capture-pane")


@pytest.mark.parametrize(
    "broken",
    [
        "error connecting to /tmp/tmux-1001/default (Permission denied)",
        "server exited unexpectedly, and more",
        "protocol version mismatch (client 8, server 7)",
    ],
)
def test_any_other_tmux_error_is_broken_and_named(tmp_path: Path, broken: str) -> None:
    """The counter-proof of #2221: only a server gone or dying reads stopped;
    an unreadable socket stops admission, and says why."""
    fake = recorded_world()
    fake.answer(
        *TMUX,
        "list-windows",
        "-t",
        "=widgets",
        "-F",
        WINDOW_FORMAT,
        returncode=1,
        stdout=broken,
    )
    env, _, err = env_for(fake, tmp_path)

    assert run(env, ["status"]) == EXIT_BROKEN
    assert broken in err.getvalue()


def test_status_says_everything_operator_asked_for(tmp_path: Path) -> None:
    env, out, _ = env_for(recorded_world(), tmp_path)

    assert run(env, ["status"]) == EXIT_OK

    lines = out.getvalue().splitlines()
    assert lines[0].startswith(
        "#501 console-operateur — claude — in development — agent/501-console-operateur"
    )
    assert "everything is pushed" in lines[0]
    assert "disk 0.9 GB" in lines[0] and "disk 312 MB" in lines[1]
    assert "disk ?" in lines[2]  # du failed: unknown, never an error
    assert "2 unpushed commit(s), 2 uncommitted file(s)" in lines[1]
    assert "stopped" in lines[2] and "branch never pushed" in lines[2]
    assert "#530" in lines[3]
    assert "blocked (its provider is gone from its window)" in lines[3]
    assert lines[-1] == (
        "runner vps-1 online, busy · memory 2.2 GB free"
        " · provider claude · Claude quota ?/80%"
    )


def test_status_json_is_complete_for_the_cockpit(tmp_path: Path) -> None:
    env, out, _ = env_for(recorded_world(), tmp_path)

    assert run(env, ["status", "--json"]) == EXIT_OK

    report = json.loads(out.getvalue())
    assert report["runner"] == {"name": "vps-1", "status": "online", "busy": True}
    assert report["available_memory_mb"] == 2252
    assert report["workers"][1] == {
        "spec": 510,
        "state": "waiting",
        "phase": "development",
        "phase_text": "in development (at rest)",
        "phase_reason": "at rest",
        "phase_since": None,
        "parked": None,
        "asking": None,
        "pending_field": None,
        "nudge": None,
        "provider": "claude",
        # #2252: started before its level was recorded, no session log
        "model": UNKNOWN_LEVEL,
        "effort": UNKNOWN_LEVEL,
        "branch": "agent/510-classification",
        "worktree": str(ROOT / "510"),
        "last_activity": 1790412000,
        "unpushed_commits": 2,
        "uncommitted_paths": 2,
        "disk_kb": 320000,
        "rank": 2,
        "role": "development",
        "set_aside": None,
        "place_wait": None,
    }
    assert report["workers"][2]["disk_kb"] is None


def test_an_unreadable_github_is_unknown_never_free(tmp_path: Path) -> None:
    fake = recorded_world()
    fake.answer(
        "gh",
        "api",
        "repos/owner/repo/actions/runners",
        returncode=1,
        stdout="HTTP 401",
    )
    env, out, _ = env_for(fake, tmp_path)

    assert run(env, ["status", "--json"]) == EXIT_OK
    assert json.loads(out.getvalue())["runner"]["status"] == "unknown"


def test_status_says_the_queue_paused_first(tmp_path: Path) -> None:
    """#2192: a factory starting nothing is not taken for broken."""
    env, out, _ = env_for(recorded_world(), tmp_path)
    env.settings.state_file.write_text("queue:\n  paused: true\n")

    assert run(env, ["status"]) == EXIT_OK
    assert out.getvalue().splitlines()[0] == (
        "QUEUE PAUSED (settings): start-next starts nothing"
    )
    out.truncate(0), out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    assert json.loads(out.getvalue())["queue_paused"] is True


def test_a_broken_state_file_is_refused_loudly(tmp_path: Path) -> None:
    env, _, err = env_for(recorded_world(), tmp_path)
    env.settings.state_file.write_text("claude:\n  weekly_cap: eighty\n")

    assert run(env, ["status"]) == EXIT_BROKEN
    assert "weekly_cap" in err.getvalue()


def test_a_stopped_worker_keeps_the_provider_named_in_its_primer(
    tmp_path: Path,
) -> None:
    env, out, _ = env_for(recorded_world(), tmp_path)
    env.settings.cache_dir.mkdir()
    (env.settings.cache_dir / "primer-520.md").write_text(
        "Repository: o/r\nChantier: #520 — Dashboard\n"
        "Branche: agent/520-dashboard — provider: codex\n"
    )

    assert run(env, ["status", "--json"]) == EXIT_OK
    [w520] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 520]
    assert w520["provider"] == "codex"


def test_a_dimmed_suggestion_is_not_text_in_the_field() -> None:
    """Measured on 26/09 with a real worker: Claude Code suggests a next
    message in the empty field, drawn dim (ESC[2m). Only typed text counts."""
    suggestion = f"\x1b[39m{PROMPT}\xa0\x1b[2mfais un commit wip: et pousse\x1b[0m\n"
    assert input_field(suggestion, CLAUDE) == ""
    typed = f"\x1b[39m{PROMPT}\xa0brouillon\x1b[2m suite suggérée\x1b[22m fin\n"
    assert input_field(typed, CLAUDE) == "brouillon fin"
    assert (
        input_field(f"{PROMPT}\xa0sans échappements\n", CLAUDE) == "sans échappements"
    )


def test_busy_survives_a_status_bar_cut_to_the_window_width() -> None:
    """Measured on 26/09 (worker-577 running subagents): at 80 columns the
    status bar ended in « esc to int… », and the worker read as waiting."""
    assert is_busy(fixture("pane-claude-working-truncated.txt"), CLAUDE)
    spinner_only = fixture("pane-claude-working-truncated.txt").replace(
        " · esc to int…", ""
    )
    assert is_busy(spinner_only, CLAUDE)  # the spinner line alone suffices
    assert not is_busy(fixture("pane-claude-idle-after-work.txt"), CLAUDE)


@pytest.mark.parametrize(
    ("kb", "text"),
    [
        (None, "?"),
        (0, "0 MB"),
        (320000, "312 MB"),
        (943718, "0.9 GB"),
        (5 << 20, "5.0 GB"),
    ],
)
def test_a_size_is_said_in_mo_or_go(kb: int | None, text: str) -> None:
    assert describe_size(kb) == text


def test_du_output_that_is_not_a_size_is_unknown() -> None:
    fake = FakeRunner()
    fake.answer("du", "-sk", "/a", stdout="12\t/a\n")
    fake.answer("du", "-sk", "/b", stdout="")
    fake.answer("du", "-sk", "/c", stdout="7\t/c\n", returncode=1)
    assert size_kb(fake, Path("/a")) == 12
    assert size_kb(fake, Path("/b")) is None
    assert size_kb(fake, Path("/c")) is None


# --- a worker stopped on a permission prompt (#984) --------------------------


def test_a_permission_prompt_is_read_with_what_it_asks() -> None:
    """Seen on 07/10 (worker-976): « Do you want to proceed? » and its
    numbered options. `status` said « waiting »: the first option, behind the
    input glyph, looks like an input field at rest."""
    pane = fixture("pane-claude-permission.txt")
    assert permission_request(pane) == "Bash command: rm node_modules"
    window = Window("worker-510", "claude", 0, "claude")
    assert classify(window, pane) is WorkerState.ASKING


@pytest.mark.parametrize(
    "name",
    [
        "pane-claude-idle.txt",
        "pane-claude-working.txt",
        "pane-claude-quoting-a-permission.txt",  # the conversation quotes one
    ],
)
def test_no_permission_prompt_on_an_ordinary_screen(name: str) -> None:
    assert permission_request(fixture(name)) is None


def test_a_prompt_of_claude_code_2_1_310_says_its_command_not_its_tip() -> None:
    """Captured on the VPS on 24/10 (#1728): a tip, a description, then the
    command between dashed rules."""
    pane = fixture("pane-claude-permission-tip.txt")
    assert permission_request(pane) == "Bash command: touch .tmp/trial/made-by-claude"


def test_a_prompt_without_a_frame_says_its_nearest_line() -> None:
    pane = f"  Read(/srv/x)\n\n Do you want to proceed?\n {PROMPT} 1. Yes\n   2. No\n"
    assert permission_request(pane) == "Read(/srv/x)"


def test_a_rule_far_above_the_question_is_not_its_frame() -> None:
    old = "─" * 40
    pane = "\n".join(
        [old, "ancienne ligne", "autre", *[f"fil {i}" for i in range(20)],
         "  Read(/srv/x)", "", " Do you want to proceed?", f" {PROMPT} 1. Yes",
         "   2. No"]
    )  # fmt: skip
    assert permission_request(pane) == "Read(/srv/x)"


def test_only_a_claude_window_is_read_for_a_permission_prompt() -> None:
    pane = fixture("pane-claude-permission.txt")
    codex = Window("worker-510", "codex", 0, "codex")
    assert classify(codex, pane) is not WorkerState.ASKING


def asking_world() -> FakeRunner:
    fake = recorded_world()
    fake.answer(
        *TMUX,
        "capture-pane",
        "-p",
        "-t",
        target("worker-510"),
        stdout=fixture("pane-claude-permission.txt"),
    )
    return fake


def test_status_says_a_worker_is_blocked_on_a_permission_and_what_it_asks(
    tmp_path: Path,
) -> None:
    env, out, _ = env_for(asking_world(), tmp_path)

    assert run(env, ["status"]) == EXIT_OK
    [line] = [x for x in out.getvalue().splitlines() if x.startswith("#510")]
    assert "waits for a human (permission: Bash command: rm node_modules)" in line

    out.truncate(0), out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    [w] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 510]
    assert w["state"] == "asking"
    assert w["asking"] == "permission: Bash command: rm node_modules"


def test_a_worker_on_a_permission_prompt_is_halted_and_asking(tmp_path: Path) -> None:
    """Halted, as a resting worker: a `needs-human` episode open on its
    chantier stays open (no false « resolved »); and its prompt is told."""
    settings = recorded_settings(tmp_path)
    tmux = Tmux(asking_world(), "widgets", None)
    assert resting_specs(settings, tmux) == {510}
    assert asking_specs(settings, tmux) == {
        510: "permission: Bash command: rm node_modules"
    }


# --- a text left in the field of a worker at rest (#2003) -------------------


@pytest.mark.parametrize(
    ("screen", "pending"),
    [
        ("pane-claude-idle.txt", ""),
        ("pane-claude-field-dimmed.txt", ""),
        ("pane-claude-field-taken.txt", "ouvre la PR de #1587 et #1588 maintenant"),
        ("pane-claude-field-taken-multi.txt", "ligne une\nligne deux\nligne trois"),
        ("pane-claude-field-folded.txt", "[Pasted text #2 +30 lines]"),
    ],
    ids=["empty", "suggestion", "taken", "taken-multi", "folded"],
)
def test_the_text_left_in_a_field_is_read_whole_and_a_suggestion_is_none(
    screen: str, pending: str
) -> None:
    """Captured on Claude Code 2.1.312 (28/10), colours kept (`capture -e`):
    the suggestion is dimmed; a pasted text spans lines under the glyph; a
    long one is folded, its words not on the screen."""
    assert pending_field(fixture(screen), CLAUDE) == pending


def test_the_input_field_reads_every_line_of_a_text_left() -> None:
    taken = fixture("pane-claude-field-taken-multi.txt")
    assert input_field(taken, CLAUDE) == "ligne une ligne deux ligne trois"


def taken_world(screen: str = "pane-claude-field-taken.txt") -> FakeRunner:
    fake = recorded_world()
    fake.answer(
        *TMUX, "capture-pane", "-p", "-e", "-t", target("worker-510"),
        stdout=fixture(screen),
    )  # fmt: skip
    return fake


def test_status_says_a_worker_at_rest_has_a_text_waiting_in_its_field(
    tmp_path: Path,
) -> None:
    """26/10: worker-1586 « at rest » for hours, a text typed in its field and
    never submitted; nothing said it."""
    env, out, _ = env_for(taken_world(), tmp_path)
    assert run(env, ["status"]) == EXIT_OK
    line = out.getvalue().splitlines()[1]
    assert line.startswith("#510")
    assert 'text waiting in its field: "ouvre la PR de #1587 et #1588' in line

    env, out, _ = env_for(taken_world(), tmp_path)
    assert run(env, ["status", "--json"]) == EXIT_OK
    worker = json.loads(out.getvalue())["workers"][1]
    assert worker["pending_field"] == "ouvre la PR de #1587 et #1588 maintenant"
    assert "text waiting in its field" in worker["phase_text"]


def test_a_suggestion_or_a_busy_worker_s_field_says_nothing(tmp_path: Path) -> None:
    env, out, _ = env_for(taken_world("pane-claude-field-dimmed.txt"), tmp_path)
    assert run(env, ["status", "--json"]) == EXIT_OK
    workers = json.loads(out.getvalue())["workers"]
    assert [w["pending_field"] for w in workers] == [None, None, None, None]
