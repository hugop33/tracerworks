"""A red CI is resumed before anything new; with every session busy it
preempts the most recent chantier in dev ranked after it (#1263, #1823).

The world of `test_order`: #520, first in the order, parked on its PR #720;
#510 then #501 in development (#501 has a Claude session of its worktree),
every session busy. When #720 goes red, #501's session is closed, its id
kept, and #520's session reopened — even with the machine short of memory,
since one session replaces another. The quota still says.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest

from tests.tracerworks.fakes import FIXTURES, TMUX, FakeRunner, fixture, target
from tests.tracerworks.test_clean import GREEN, RED, answer_state
from tests.tracerworks.test_order import (
    SESSION,
    in_order,
    in_state_since,
    line_of,
    shop,
    status_lines,
)
from tracerworks import order, parking
from tracerworks.cli import EXIT_BROKEN, EXIT_OK, EXIT_REFUSED, Env, run
from tracerworks.usage import project_dir

#: The session of #501's worktree, the one set aside.
DEV_SESSION = "11111111-2222-4333-8444-555555555555"

Shop = tuple[FakeRunner, Env, io.StringIO, io.StringIO]


@pytest.fixture
def transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Claude Code's log of #501's worktree: one session."""
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    folder = project_dir(config / "projects", tmp_path / "wt" / "501")
    folder.mkdir(parents=True)
    (folder / f"{DEV_SESSION}.jsonl").write_text("{}\n")
    return config


def tight(fake: FakeRunner, env: Env) -> Env:
    """2,2 Go free while the runner runs a CI: no new session may start."""
    fake.answer(
        "gh", "api", "repos/owner/repo/actions/runners", stdout=fixture("runners.json")
    )
    settings = dataclasses.replace(env.settings, meminfo=FIXTURES / "meminfo.txt")
    return dataclasses.replace(env, settings=settings)


def red_first(tmp_path: Path, windows: tuple[int, ...] = (501, 510)) -> Shop:
    """Every session busy, #520's PR red for long, the machine short of memory."""
    fake, env, out, err = shop(tmp_path, windows=windows)
    env = tight(fake, env)
    parking.park(env.settings, 520, SESSION, 720, 0)
    answer_state(fake, 720, RED)
    in_order(env, {520: 1, 510: 2, 501: 3})
    in_state_since(env, 520, 0)  # its red waited past a step: a safe point
    return fake, env, out, err


def opened(fake: FakeRunner) -> list[tuple[str, ...]]:
    return [c.argv for c in fake.ran(*TMUX, "new-window")]


def window_of(argv: tuple[str, ...]) -> str:
    return argv[argv.index("-n") + 1]


# --- a red with every session busy -------------------------------------------------


def test_a_red_sets_aside_the_most_recent_in_development_and_is_resumed(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    """12/10: #1252 red waited an hour behind « 2 workers already running »,
    then « < 3500 MB, runner busy ». It replaces a session: neither stops it."""
    fake, env, out, err = red_first(tmp_path)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()

    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    [argv] = opened(fake)
    assert window_of(argv) == "worker-520" and f"--resume {SESSION}" in argv[-1]
    said = out.getvalue()
    assert "#501 set aside (session 11111111) for #520's red, better ranked" in said
    assert "#520: PR #720 needs its worker — worker-520 resumed" in said
    assert said.index("#501 set aside") < said.index("#520: PR #720")
    assert launched == []
    [record] = order.set_aside_records(env.settings).values()
    assert (record.spec, record.session, record.for_spec) == (501, DEV_SESSION, 520)
    assert not fake.ran(*TMUX, "kill-window", "-t", target("worker-510"))


def test_a_red_refused_by_the_quota_sets_nothing_aside(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = red_first(tmp_path)
    env.settings.state_file.write_text("claude:\n  weekly_used: 95\n")

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "cap reached" in err.getvalue()
    assert not fake.ran(*TMUX, "kill-window") and opened(fake) == []
    assert order.set_aside_records(env.settings) == {}


def test_a_red_with_a_free_session_passes_the_memory_guard(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """It adds a session: the memory guard says — and the red still goes first."""
    fake, env, _, err = shop(tmp_path, windows=[501])
    env = tight(fake, env)
    parking.park(env.settings, 520, SESSION, 720, 0)
    answer_state(fake, 720, RED)
    in_order(env, {520: 1, 501: 2})

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert (
        "#520: PR #720 needs its worker — waits, before any new spec:"
        " available memory 2252 MB < threshold 3500 MB" in err.getvalue()
    )
    assert opened(fake) == [] and launched == []
    assert not fake.ran("gh", "issue", "list")


def test_a_red_never_preempts_a_chantier_resumed_less_than_20_min_ago(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    """No back and forth (#1823): #501 and #510 just came back in dev."""
    fake, env, out, _ = red_first(tmp_path)
    in_state_since(env, 501, int(env.now()) - 60, "development")
    in_state_since(env, 510, int(env.now()) - 60, "development")

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert (
        "#520: PR #720 needs its worker — waits: #510 (2nd) · #501 (3rd), none"
        " ranked after it at a safe point yet" in out.getvalue()
    )
    assert not fake.ran(*TMUX, "kill-window") and opened(fake) == []


def test_a_red_whose_chantier_in_development_has_no_session_waits_said(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """No transcript for #501's worktree: closing it would lose it."""
    fake, env, out, _ = red_first(tmp_path)

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert (
        "#501 NOT set aside: no Claude session found for its worktree"
        " — #520's red waits" in out.getvalue()
    )
    assert not fake.ran(*TMUX, "kill-window") and opened(fake) == []
    assert order.set_aside_records(env.settings) == {}


# --- the chantier set aside -----------------------------------------------------------


def set_aside_for_520(tmp_path: Path, windows: tuple[int, ...] = ()) -> Shop:
    """#501 set aside for #520, which is back in CI (green)."""
    fake, env, out, err = shop(tmp_path, windows=windows)
    parking.park(env.settings, 520, SESSION, 720, 0)
    answer_state(fake, 720, GREEN)
    order.set_aside(env.settings, 501, DEV_SESSION, 520, 1790413000)
    in_order(env, {520: 1, 501: 2})
    return fake, env, out, err


def test_a_set_aside_chantier_resumes_its_session_before_anything_new(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = set_aside_for_520(tmp_path)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    [argv] = opened(fake)
    assert window_of(argv) == "worker-501" and f"--resume {DEV_SESSION}" in argv[-1]
    reason = (tmp_path / "wt" / ".primers" / "501-resume.md").read_text()
    assert "mise de côté" in reason and "#520" in reason
    assert "git status" in reason and "avant de continuer" in reason
    assert "#501: a session free — worker-501 resumed" in out.getvalue()
    assert launched == []
    assert order.set_aside_records(env.settings) == {}


def test_a_set_aside_chantier_waits_while_every_session_is_busy(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#510 holds the one session: #501 waits, and nothing new starts."""
    fake, env, out, err = set_aside_for_520(tmp_path, windows=(510,))
    env.settings.state_file.write_text("max_workers: 1\n")
    in_order(env, {510: 3})

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert opened(fake) == [] and launched == []
    assert "#501 set aside — waits: 1/1 sessions busy: #510 (3rd)" in out.getvalue()
    assert "1/1 sessions busy" in err.getvalue()
    assert 501 in order.set_aside_records(env.settings)


def test_a_set_aside_chantier_with_a_window_is_never_resumed_again(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Never two sessions on one chantier: #501 was reopened by hand."""
    fake, env, out, _ = set_aside_for_520(tmp_path, windows=(501,))

    run(env, ["start-next"])
    assert [a for a in opened(fake) if window_of(a) == "worker-501"] == []
    assert "#501 has its window again: no longer set aside" in out.getvalue()
    assert order.set_aside_records(env.settings) == {}


# --- by hand, and what status says ---------------------------------------------


def test_set_aside_by_hand_closes_the_window_and_keeps_the_session(
    tmp_path: Path, transcripts: Path
) -> None:
    fake, env, out, err = shop(tmp_path, windows=[501])

    assert run(env, ["set-aside", "501"]) == EXIT_OK, err.getvalue()
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert "#501 set aside (session 11111111) by Sam" in out.getvalue()
    [record] = order.set_aside_records(env.settings).values()
    assert (record.spec, record.session, record.for_spec) == (501, DEV_SESSION, None)


@pytest.mark.parametrize(
    ("spec", "said"),
    [
        # stopped: set aside after all only with its session (#2118)
        (520, "#520 NOT set aside: no Claude session found for its worktree"),
        (501, "#501 NOT set aside: no Claude session found for its worktree"),
    ],
)
def test_set_aside_by_hand_refuses_what_it_could_not_resume(
    tmp_path: Path, spec: int, said: str
) -> None:
    fake, env, _, err = shop(tmp_path, windows=[501])

    assert run(env, ["set-aside", str(spec)]) == EXIT_REFUSED
    assert said in err.getvalue()
    assert not fake.ran(*TMUX, "kill-window")


def test_status_says_set_aside(tmp_path: Path) -> None:
    _, env, out, _ = set_aside_for_520(tmp_path)

    lines = status_lines(env, out)
    assert line_of(lines, 501).endswith(" — 2nd in the order")
    assert "1st #520 light check (PR #720, its session closed)" in lines
    assert "2nd #501 parked (set aside for #520)" in lines

    out.truncate(0)
    out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    [w] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 501]
    assert (w["rank"], w["role"]) == (2, "set_aside")
    assert w["set_aside"] == {
        "session": DEV_SESSION,
        "for": 520,
        "set_aside_at": 1790413000,
    }


def test_a_red_that_fails_to_reopen_after_a_set_aside_ends_the_pass(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    """A session was closed for it: nothing else happens on this pass — not
    the chantier set aside reopened at once, nor a second red."""
    fake, env, _, err = red_first(tmp_path)
    # #540 has its red too, ranked last (#510's session is known)
    folder = project_dir(transcripts / "projects", tmp_path / "wt" / "510")
    folder.mkdir(parents=True)
    (folder / f"{SESSION}.jsonl").write_text("{}\n")
    parking.park(env.settings, 540, SESSION, 740, 0)
    answer_state(fake, 740, RED)
    in_order(env, {540: 4})
    fake.answer(*TMUX, "new-window", prefix=True, returncode=1, stdout="no server")

    assert run(env, ["start-next"]) == EXIT_BROKEN  # a session closed for nothing
    assert "#520: parked worker NOT resumed" in err.getvalue()
    assert 501 in order.set_aside_records(env.settings)
    assert len(opened(fake)) == 1 and launched == []
    assert not fake.ran(*TMUX, "kill-window", "-t", target("worker-510"))


def test_a_set_aside_not_recorded_is_broken_and_gives_its_session(
    tmp_path: Path, transcripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its window is closed: without the record nothing resumes it — the
    exit says broken (3), and the session is given for a resume by hand."""
    import sqlite3

    _, env, _, err = shop(tmp_path, windows=[501])

    def broken(*_: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(order, "set_aside", broken)
    assert run(env, ["set-aside", "501"]) == EXIT_BROKEN
    assert f"claude --resume {DEV_SESSION}" in err.getvalue()
