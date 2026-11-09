"""A worker waiting on its CI is parked, then resumed if it goes red (#1025).

`clean` parks: #501 of the recorded world, its PR #619 open and its CI running,
has its Claude session closed and the session's id written in the history
base. `start-next` resumes: a parked #520 (worktree, no window) whose PR went
red or into conflict is reopened with `claude --resume <id>`, before any new
chantier and under the same guards. No real Claude: the tmux calls are
recorded, the transcripts are files of the test.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.tracerworks.fakes import TMUX, FakeRunner, answer_job_log, fixture, target
from tests.tracerworks.test_clean import (
    GREEN,
    HEAD,
    RED,
    RUNNING,
    answer_comments,
    answer_state,
    clean_world,
    cleaned,
    finished_501,
    removed,
    waiting_501,
)
from tests.tracerworks.test_start_next import FACTORY, gh_lists
from tests.tracerworks.test_start_stop import (
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
    world,
)
from tracerworks import parking
from tracerworks.cleanup import RED_MARKER, Checks
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, Env, run
from tracerworks.process import Result
from tracerworks.runtime.tmux import WINDOW_FORMAT
from tracerworks.usage import project_dir

LIST_WINDOWS = (*TMUX, "list-windows", "-t", "=widgets", "-F", WINDOW_FORMAT)
SESSION = "0f1e2d3c-4b5a-4968-8776-655443322110"
OLDER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RED_WITH_JOB = [
    *GREEN,
    {"__typename": "CheckRun", "name": "browser", "status": "COMPLETED",
     "conclusion": "FAILURE",
     "detailsUrl": "https://github.com/owner/repo/actions/runs/77/job/4242"},
]  # fmt: skip


@pytest.fixture
def transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Claude Code's logs: two sessions of #501's worktree, the newest last."""
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    folder = project_dir(config / "projects", tmp_path / "wt" / "501")
    folder.mkdir(parents=True)
    (folder / f"{OLDER}.jsonl").write_text("{}\n")
    newest = folder / f"{SESSION}.jsonl"
    newest.write_text("{}\n")
    stamp = (folder / f"{OLDER}.jsonl").stat().st_mtime
    os.utime(newest, (stamp + 60, stamp + 60))
    return config


def records(env: Env) -> dict[int, parking.Parked]:
    return parking.parked(env.settings)


def shells(pane: str) -> str:
    busy = pane.replace("· ← for agents", "· 1 shell · ← for agents")
    assert busy != pane
    return busy


# --- parking, in clean ----------------------------------------------------------------


def test_a_worker_watching_its_running_ci_in_a_shell_is_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    """08/10: #1016 kept 36 times — « a shell is still running » was its
    `gh run watch`. While the CI runs, that shell is only waiting."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=shells(fixture("pane-claude-idle.txt")))
    answer_state(fake, 619, RUNNING)
    code, env, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert str(tmp_path / "wt" / "501") not in removed(fake)
    assert (
        f"#501: PR #619 open, worker-501 closed — parked (session {SESSION[:8]})" in out
    )
    [record] = records(env).values()
    assert (record.spec, record.session, record.pull) == (501, SESSION, 619)
    assert record.resumed_for is None


def test_a_worker_watching_its_ci_in_the_foreground_is_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    """Working — but on `gh run watch`: everything is pushed and the CI runs."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=fixture("pane-claude-working.txt"))
    answer_state(fake, 619, RUNNING)
    code, env, _, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert 501 in records(env)


def test_a_working_worker_whose_ci_is_over_keeps_its_window(
    tmp_path: Path, transcripts: Path
) -> None:
    """Red or green, a working worker is doing something: it stays."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=fixture("pane-claude-working.txt"))
    answer_state(fake, 619, RED)
    code, env, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — window worker-501 open" in out
    assert records(env) == {}


def test_a_shell_running_after_the_ci_still_keeps_the_worker(
    tmp_path: Path, transcripts: Path
) -> None:
    """CI red: the shell is the worker's fix being tested — not a wait."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=shells(fixture("pane-claude-idle.txt")))
    answer_state(fake, 619, RED)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — PR #619 open, but a shell is still running" in out


def test_a_worker_with_an_unpushed_commit_is_never_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=shells(fixture("pane-claude-idle.txt")))
    answer_state(fake, 619, RUNNING)
    fake.answer(
        "git", "-C", str(tmp_path / "wt" / "501"), "status", "--porcelain=v2",
        "--branch",
        stdout="# branch.oid 1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "# branch.head agent/501-console-operateur\n"
        "# branch.upstream origin/agent/501-console-operateur\n"
        "# branch.ab +1 -0\n",
    )  # fmt: skip
    code, env, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — window worker-501 open" in out
    assert records(env) == {}


def test_a_merged_pr_whose_checks_still_run_gives_the_slot_back(
    tmp_path: Path, transcripts: Path
) -> None:
    """08/10: #1021 done, merged, counted in the cap while its browser job ran."""
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601, checks=RUNNING)
    code, env, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert str(tmp_path / "wt" / "501") not in removed(fake)
    assert not fake.ran("gh", "issue", "close")
    assert "#501: PR #601 merged, its checks still run — worker-501 parked" in out
    assert records(env)[501].pull == 601


def test_a_parked_worker_red_after_the_merge_keeps_its_worktree_for_the_resume(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    env, _, _ = make_env(fake, tmp_path)
    parking.park(env.settings, 540, SESSION, 640, 0)
    fake.answer(
        "gh", "pr", "list", "--repo", "owner/repo", "--head", "agent/540-fini",
        "--state", "merged", "--json", "number", "--limit", "1",
        stdout=json.dumps([{"number": 640}]),
    )  # fmt: skip
    fake.answer(
        "gh", "pr", "view", "640", "--repo", "owner/repo", "--json",
        "statusCheckRollup", stdout=json.dumps({"statusCheckRollup": RED}),
    )  # fmt: skip
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert removed(fake) == []
    assert not fake.ran("gh", "issue", "comment")
    assert "#540 kept — PR #640 red after the merge, its parked worker resumes" in out


def test_a_removed_worktree_forgets_its_parked_session(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    env, _, _ = make_env(fake, tmp_path)
    parking.park(env.settings, 540, SESSION, 640, 0)
    code, _, _, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert removed(fake) == [str(tmp_path / "wt" / "540")]
    assert records(env) == {}


# --- a window that closes badly (23/10, #1682) ---------------------------------------


def closing_on_kill(fake: FakeRunner, error: str, closed: bool) -> None:
    """`kill-window` fails with `error`; the window is gone after it if
    `closed` — #1682: `/exit` closed it between the last look and the kill."""

    def windows(_: Sequence[str], __: bytes | None) -> Result:
        text = fixture("list-windows.txt")
        if closed and fake.ran(*TMUX, "kill-window"):
            text = "\n".join(
                line for line in text.splitlines() if not line.startswith("worker-501")
            )
        return Result(0, text)

    fake.on(*LIST_WINDOWS)(windows)
    fake.answer(*TMUX, "kill-window", prefix=True, returncode=1, stdout=error)


def test_a_window_closed_between_exit_and_kill_is_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=fixture("pane-claude-working.txt"))
    answer_state(fake, 619, RUNNING)
    closing_on_kill(fake, "can't find window: worker-501", closed=True)
    code, env, out, err = cleaned(tmp_path, fake)

    assert code == EXIT_OK, err
    parked = f"#501: PR #619 open, worker-501 closed — parked (session {SESSION[:8]})"
    assert parked in out
    assert (records(env)[501].session, records(env)[501].pull) == (SESSION, 619)
    assert "kill-window" not in err


def test_a_close_that_fails_otherwise_still_parks_and_says_why(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=fixture("pane-claude-working.txt"))
    answer_state(fake, 619, RUNNING)
    closing_on_kill(fake, "server exited unexpectedly", closed=False)
    _, env, _, err = cleaned(tmp_path, fake)

    assert (records(env)[501].session, records(env)[501].pull) == (SESSION, 619)
    assert "#501: worker-501 NOT closed, parked all the same" in err
    assert "server exited unexpectedly" in err


def test_a_red_after_a_bad_close_resumes_the_worker(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#1682 replayed: parked through a racy close, red on its head, then
    the next `start-next` reopens its session."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=fixture("pane-claude-working.txt"))
    answer_state(fake, 619, RUNNING)
    closing_on_kill(fake, "can't find window: worker-501", closed=True)
    cleaned(tmp_path, fake)

    gh_lists(fake, FACTORY)
    issue_answer(fake, 565, "Spec — Factory V1 : tracerworks")
    git_repo_answers(fake)
    fake.answer(*TMUX, "new-window", prefix=True)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    answer_state(fake, 619, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    [argv] = resumed_argv(fake)
    assert argv[argv.index("-n") + 1] == "worker-501"
    assert f"--resume {SESSION}" in argv[-1]
    assert "#501: PR #619 needs its worker — worker-501 resumed" in out.getvalue()
    assert launched == []


# --- resuming, in start-next ---------------------------------------------------


def resume_world(tmp_path: Path) -> tuple[FakeRunner, Env, io.StringIO, io.StringIO]:
    """No window; #520 parked on PR #720 (session SESSION); FACTORY queued."""
    fake = world(tmp_path)
    gh_lists(fake, FACTORY)
    issue_answer(fake, 565, "Spec — Factory V1 : tracerworks")
    git_repo_answers(fake)
    fake.answer(*TMUX, "new-window", prefix=True)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    parking.park(env.settings, 520, SESSION, 720, 0)
    return fake, env, out, err


def resumed_argv(fake: FakeRunner) -> list[tuple[str, ...]]:
    return [c.argv for c in fake.ran(*TMUX, "new-window")]


def test_a_red_ci_resumes_the_parked_session_with_the_failed_job(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    # the API's log, given while the run still runs (#1092): the whole job's,
    # stamped, the runner's cleanup after the error
    answer_job_log(
        fake, "owner/repo", 4242,
        stdout="﻿2026-10-08T10:00:00.1234567Z ##[group]Run uv run pytest\n"
        "2026-10-08T10:00:00.2234567Z FAILED test_widgets.py\n"
        "2026-10-08T10:00:01.0000000Z 1 failed, 40 passed\n"
        "2026-10-08T10:00:01.1000000Z ##[error]Process completed with exit code 1.\n"
        "2026-10-08T10:00:02.0000000Z Post job cleanup.\n"
        "2026-10-08T10:00:02.1000000Z [command]/usr/bin/git version\n",
    )  # fmt: skip

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()

    [argv] = resumed_argv(fake)
    assert "-n" in argv and argv[argv.index("-n") + 1] == "worker-520"
    assert argv[argv.index("-c") + 1] == str(tmp_path / "wt" / "520")
    script = argv[-1]
    assert f"--resume {SESSION}" in script
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert str(tmp_path / "wt" / ".primers" / "520-resume.md") in script
    assert "PR #720" in reason and "des checks ont fini rouges : browser" in reason
    assert "\nFAILED test_widgets.py\n1 failed, 40 passed\n##[error]" in reason
    assert "Post job cleanup" not in reason and "2026-10-08T" not in reason
    assert "ne pousse rien" in reason
    # the resume took the slot: no new chantier on this pass
    assert launched == []
    assert "#520: PR #720 needs its worker — worker-520 resumed" in out.getvalue()
    assert records(env)[520].resumed_for == f"{HEAD}:red"


def test_a_coloured_red_log_is_in_the_resume_without_its_colours(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#1999: `gh` 2.101 refuses a log with escape sequences unless told to
    allow them; the resume said « journal non disponible » (#1974, 28/10)."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_job_log(
        fake, "owner/repo", 4242,
        stdout="2026-10-28T17:19:50.1Z \x1b[31mFAILED\x1b[0m tests/test_widgets.py"
        "::test_reply - AssertionError\n"
        "2026-10-28T17:19:51.1Z \x1b[31m1 failed\x1b[0m, 40 passed\n"
        "2026-10-28T17:19:52.1Z ##[error]Process completed with exit code 1.\n",
    )  # fmt: skip

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "journal non disponible" not in reason
    assert (
        "\nFAILED tests/test_widgets.py::test_reply - AssertionError\n"
        "1 failed, 40 passed\n##[error]" in reason
    )
    assert "\x1b" not in reason


def test_the_same_red_head_resumes_once(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """A worker that judged the red unrelated and parked again is not
    reopened for the same commit; the queue goes on. It said so on its PR:
    unanswered, it would be told once more (#2175, `test_unanswered_red`)."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(
        fake, 720, RED, comments=["Sans rapport, voir le run."],
        commented_at="2026-10-31T03:25:00Z",
    )  # fmt: skip
    parking.mark_resumed(env.settings, 520, f"{HEAD}:red", 0)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []
    assert [n for n, _, _ in launched] == [565]


def test_a_new_head_red_again_resumes_again(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED, sha="d" * 40)
    parking.mark_resumed(env.settings, 520, f"{HEAD}:red", 0)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    assert launched == []


def test_a_conflict_resumes_the_parked_session(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RUNNING, mergeable="CONFLICTING")

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "la PR est en conflit avec origin/main" in reason
    # #2013: a head in conflict will not merge — its run is not waited for
    assert "pousse dès que c'est prêt" in reason
    assert "attends sa fin" not in reason


def motive(red: bool, conflict: bool) -> parking.PullState:
    return parking.PullState(
        number=1996,
        state="OPEN",
        head_sha=HEAD,
        conflicting=conflict,
        checks=Checks(running=[], failed=["checks"] if red else []),
    )


@pytest.mark.parametrize(
    ("red", "rerun", "machine"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (True, True, True),
    ],
)
def test_a_resume_on_a_conflict_pushes_the_merge_at_once(
    red: bool, rerun: bool, machine: bool
) -> None:
    """#2013 (#1996, 28/10): a head in conflict will not merge, even green —
    the worker pushes its merge as soon as it is ready and rests; the run of
    the old head is cancelled on purpose. With a red too, and after a rerun."""
    pull = motive(red, conflict=True)
    text = parking.resume_text(FakeRunner(), "owner/repo", "origin/main", pull,
                               rerun, machine=machine)  # fmt: skip
    assert "la PR est en conflit avec origin/main" in text
    assert "pousse dès que c'est prêt" in text
    assert "annulé exprès" in text
    assert "repose-toi" in text
    assert "ne pousse rien" not in text and "attends sa fin" not in text
    if red:
        assert "des checks ont fini rouges : checks" in text


@pytest.mark.parametrize(
    ("rerun", "machine", "lead"),
    [
        (False, False, "tant que le run tourne, ne pousse rien"),
        (True, False, "tu avais jugé sans rapport"),
        (True, True, "ne nommait aucun test"),
    ],
)
def test_a_red_without_a_conflict_keeps_waiting_for_its_run(
    rerun: bool, machine: bool, lead: str
) -> None:
    """A head that may merge: a push would hide the reds not shown yet."""
    text = parking.resume_text(FakeRunner(), "owner/repo", "origin/main",
                               motive(True, False), rerun, machine=machine)  # fmt: skip
    assert lead in text and "ne pousse rien" in text
    assert "en conflit" not in text and "annulé exprès" not in text


def sent_back_by_the_queue(env: Env, pull: int, reason: str) -> None:
    """The merge queue's last stay of `pull`: it left, sent back (#1715)."""
    from contextlib import closing

    from tracerworks import store

    with closing(store.connect(env.settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state, left_at,"
            " outcome, reason) VALUES (?, ?, 0, 'out', 1, 'sent_back', ?)",
            (env.settings.github_repo, pull, reason),
        )


@pytest.mark.parametrize("mergeable", ["MERGEABLE", "CONFLICTING"])
def test_a_pull_request_the_queue_sent_back_resumes_its_worker_told_so(
    tmp_path: Path, launched: list[tuple[int, str, str]], mergeable: str
) -> None:
    """#1715: the queue's full suite red (or a conflict) on PR #720 — the
    parked worker is resumed by today's resume, told the queue sent it back,
    that its branch moved on GitHub, and how it comes back in."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED, mergeable=mergeable)
    sent_back_by_the_queue(env, 720, "the full suite is red: integration (1/2)")

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "des checks ont fini rouges" in reason
    assert "sortie de la file de fusion : the full suite is red: integration" in reason
    assert "`git pull` avant de corriger" in reason
    assert "Ne pose pas `merge-queue-testing`" in reason
    if mergeable == "CONFLICTING":
        assert "la PR est en conflit avec origin/main" in reason


def test_a_migration_taken_twice_resumes_with_the_number_to_take(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#1715: the queue brought the branch up to date, the chain's test is red;
    the resume hands its words to the worker: « renuméroter en N+1 sur N »."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    answer_job_log(
        fake, "owner/repo", 4242,
        stdout="2026-10-24T10:00:00.1Z FAILED tests/test_migration_chain.py::"
        "test_each_revision_is_held_by_one_file - AssertionError: 145 is in 2"
        " files: 145_a.py, 145_b.py — renuméroter celle de la PR en 146 sur 145\n"
        "2026-10-24T10:00:01.1Z ##[error]Process completed with exit code 1.\n",
    )  # fmt: skip
    sent_back_by_the_queue(env, 720, "the full suite is red: checks")

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "renuméroter celle de la PR en 146 sur 145" in reason
    assert "sortie de la file de fusion" in reason


def test_a_red_in_the_queue_s_test_waits_for_the_queue(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Spec #1708: a machine's red is rerun « sans réveiller le worker » — so
    while the queue tests PR #720, its red is the queue's to read first."""
    from contextlib import closing

    from tracerworks import store

    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED)
    with closing(store.connect(env.settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state)"
            " VALUES (?, 720, 0, 'testing')",
            (env.settings.github_repo,),
        )

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []
    assert "the queue decides first" in out.getvalue()


def test_a_run_the_queue_cancelled_while_it_waits_resumes_nothing(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#2109: PR #720 waits in the queue, its run 77 cancelled by the queue —
    its aggregate reads FAILURE (#2091, 30/10). No red: the worker sleeps —
    its stay over without a test too (gone, a draft)."""
    from contextlib import closing

    from tracerworks import store

    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    with closing(store.connect(env.settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state)"
            " VALUES (?, 720, 0, 'waiting')",
            (env.settings.github_repo,),
        )
        db.execute(
            "INSERT INTO merge_queue_cancel (project, pull, head, run, attempt,"
            " at, url) VALUES (?, 720, ?, 77, 2, 1, 'u')",
            (env.settings.github_repo, HEAD),
        )

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []
    assert "needs its worker" not in out.getvalue()
    with closing(store.connect(env.settings.history_db)) as db:
        db.execute("UPDATE merge_queue SET state = 'out', outcome = 'gone'")
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []


def test_a_red_of_the_queue_s_test_after_its_cancel_resumes_the_worker(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """The counter-proof: the queue cancelled run 77, then tested the pull
    request — rerunning that run — and sent it back red: a red."""
    from contextlib import closing

    from tracerworks import store

    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED_WITH_JOB)
    with closing(store.connect(env.settings.history_db)) as db:
        db.execute(
            "INSERT INTO merge_queue_cancel (project, pull, head, run, attempt,"
            " at, url) VALUES (?, 720, ?, 77, 2, 1, 'u')",
            (env.settings.github_repo, HEAD),
        )
        db.execute(
            "INSERT INTO merge_queue (project, pull, entered_at, state, started_at,"
            " left_at, outcome, reason) VALUES (?, 720, 0, 'out', 2, 3,"
            " 'sent_back', 'the full suite is red: browser')",
            (env.settings.github_repo,),
        )
    answer_job_log(
        fake, "owner/repo", 4242,
        stdout="2026-10-30T10:00:00.1Z FAILED tests/integration/test_widgets.py::"
        "test_reply - AssertionError\n"
        "2026-10-30T10:00:01.1Z ##[error]Process completed with exit code 1.\n",
    )  # fmt: skip

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1


def test_a_red_outside_the_queue_says_nothing_of_it(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "file de fusion" not in reason


@pytest.mark.parametrize("rollup", [GREEN, RUNNING], ids=["green", "running"])
def test_a_green_or_running_ci_resumes_nothing_and_the_next_chantier_starts(
    tmp_path: Path, launched: list[tuple[int, str, str]], rollup: list[dict[str, str]]
) -> None:
    """The parked #520 holds no slot: the queue's #565 starts."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, rollup)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []
    assert [n for n, _, _ in launched] == [565]


def test_a_red_ci_with_the_workshop_full_waits_then_goes_first(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = resume_world(tmp_path)
    answer_state(fake, 720, RED)
    full = fixture("list-windows.txt")  # worker-501, 510, 530: over the cap
    from tracerworks.runtime.tmux import WINDOW_FORMAT

    fake.answer(
        *TMUX, "list-windows", "-t", "=widgets", "-F", WINDOW_FORMAT, stdout=full
    )

    assert run(env, ["start-next"]) == EXIT_REFUSED
    # every session busy (#1823): #520, entered with them, ranks after #501
    # and #510, and #530's dead window cannot be closed: the red waits
    assert (
        "#520: PR #720 needs its worker — waits: #501 (1st) · #510 (2nd) ·"
        " #530 (4th), none ranked after it at a safe point yet" in out.getvalue()
    )
    assert "3/2 sessions busy" in err.getvalue()
    assert resumed_argv(fake) == [] and launched == []
    assert records(env)[520].resumed_for is None

    # a slot frees: the resume passes before the queue's #565
    calm(fake, tmp_path, env)
    assert run(env, ["start-next"]) == EXIT_OK
    assert len(resumed_argv(fake)) == 1
    assert launched == []


def test_a_parked_chantier_with_a_window_is_never_resumed(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Never two sessions on one chantier: #501 has its window."""
    fake, env, _, err = resume_world(tmp_path)
    parking.park(env.settings, 501, SESSION, 619, 0)
    answer_state(fake, 720, GREEN)
    one = "\n".join(
        line
        for line in fixture("list-windows.txt").splitlines()
        if not line.startswith(("worker-510", "worker-530"))
    )
    fake.answer(*LIST_WINDOWS, stdout=one)
    fake.answer(
        *TMUX, "capture-pane", "-p", "-t", target("worker-501"),
        stdout=fixture("pane-claude-idle.txt"),
    )  # fmt: skip

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert not fake.ran("gh", "pr", "view", "619")
    assert resumed_argv(fake) == []


def test_a_red_after_the_merge_resumes_with_the_red_message_and_marks_the_spec(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED, state="MERGED")
    answer_comments(fake, 520, [])
    fake.answer("gh", "issue", "comment", "520", prefix=True)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "Ta PR #720 est fusionnée" in reason
    [comment] = fake.ran("gh", "issue", "comment", "520")
    assert comment.stdin is not None
    assert RED_MARKER.format(pull=720) in comment.stdin.decode()


def test_a_red_after_the_merge_already_told_resumes_nothing(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED, state="MERGED")
    answer_comments(fake, 520, [f"{RED_MARKER.format(pull=720)}\n…"])

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []
    assert [n for n, _, _ in launched] == [565]


def test_a_fresh_start_forgets_the_parked_session(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    parking.park(env.settings, 565, SESSION, 900, 0)
    answer_state(fake, 720, GREEN)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert 565 not in records(env) and 520 in records(env)


# --- what is shown ----------------------------------------------------------------


def test_status_shows_a_parked_worker_apart_from_a_stopped_one(tmp_path: Path) -> None:
    fake = world(tmp_path)
    env, out, _ = make_env(fake, tmp_path)
    parking.park(env.settings, 520, SESSION, 720, 0)

    assert run(env, ["status"]) == EXIT_OK
    [line] = [x for x in out.getvalue().splitlines() if x.startswith("#520")]
    assert "light check (PR #720, its session closed)" in line
    assert " stopped " not in line

    out.truncate(0)
    out.seek(0)
    assert run(env, ["status", "--json"]) == EXIT_OK
    [w] = [w for w in json.loads(out.getvalue())["workers"] if w["spec"] == 520]
    assert (w["state"], w["parked"]["session"], w["parked"]["pull"]) == (
        "parked",
        SESSION,
        720,
    )


def test_only_a_uuid_is_a_session(tmp_path: Path) -> None:
    folder = project_dir(tmp_path, tmp_path / "wt")
    folder.mkdir(parents=True)
    (folder / "--dangerous.jsonl").write_text("{}\n")
    assert parking.latest_session(tmp_path, tmp_path / "wt") is None


def test_a_conflict_after_a_red_on_the_same_head_resumes_again(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED, mergeable="CONFLICTING")
    parking.mark_resumed(env.settings, 520, f"{HEAD}:red", 0)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1


def test_a_worker_parked_at_rest_on_a_red_it_saw_is_not_woken_for_it(
    tmp_path: Path, transcripts: Path, launched: list[tuple[int, str, str]]
) -> None:
    """No churn: at rest on its red, parked, then not resumed a minute later."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, RED)
    code, env, _, _ = cleaned(tmp_path, fake)
    assert code == EXIT_OK
    assert records(env)[501].resumed_for == f"{HEAD}:red"

    assert not parking.due_on_open(
        parking.pull_state(fake, "owner/repo", 619, ("operator-login",)),
        records(env)[501],
    )


def resumed_501_for_red(tmp_path: Path, fake: FakeRunner, ago: float) -> Env:
    """#501 parked on PR #619, resumed for its red `ago` seconds before now."""
    env, _, _ = make_env(fake, tmp_path)
    parking.park(env.settings, 501, SESSION, 619, env.now() - ago - 600)
    parking.mark_resumed(env.settings, 501, f"{HEAD}:red", env.now() - ago)
    return env


def test_a_worker_just_resumed_for_its_red_is_not_parked_again(
    tmp_path: Path, transcripts: Path
) -> None:
    """19/10, #1539: resumed at 21:45:25 for its red, read « at rest »
    between two of its commands at 21:46:25, parked with that red « seen »
    — never woken again. A resume has a grace before a park at rest."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, RED)
    resumed_501_for_red(tmp_path, fake, ago=60)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — resumed for PR #619 1 min ago, its worker is on it" in out


def test_a_resumed_worker_at_rest_after_the_grace_is_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, RED)
    resumed_501_for_red(tmp_path, fake, ago=parking.RESUME_GRACE_S)
    code, env, _, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert records(env)[501].resumed_for == f"{HEAD}:red"


def test_a_resumed_worker_at_rest_on_a_new_head_is_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    """The grace covers the red it was resumed for, not its next push."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, RUNNING, sha="d" * 40)
    resumed_501_for_red(tmp_path, fake, ago=60)
    code, _, _, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))


def resumed_501_for_conflict(tmp_path: Path, fake: FakeRunner, ago: float) -> None:
    """#501 parked on PR #619, resumed for its conflict `ago` seconds before."""
    env, _, _ = make_env(fake, tmp_path)
    parking.park(env.settings, 501, SESSION, 619, env.now() - ago - 600)
    parking.mark_resumed(env.settings, 501, f"{HEAD}:conflict,", env.now() - ago)


def test_a_worker_resumed_for_a_conflict_is_kept_while_github_recomputes(
    tmp_path: Path, transcripts: Path
) -> None:
    """30/10, #2094: resumed at 15:54:11 for its conflict, parked at 15:54:26
    in the middle of its `git merge` — a merge on the base had just made
    GitHub say `UNKNOWN`, and the grace asked for the conflict still said.
    Unknown counts as still in conflict: the worker keeps its window."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, [], mergeable="UNKNOWN")
    resumed_501_for_conflict(tmp_path, fake, ago=15)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — resumed for PR #619 0 min ago, its worker is on it" in out


@pytest.mark.parametrize(
    ("sha", "mergeable", "ago"),
    [
        (HEAD, "MERGEABLE", 60),  # resolved: mergeable again
        ("d" * 40, "UNKNOWN", 60),  # its merge pushed: a new head
        (HEAD, "UNKNOWN", parking.RESUME_GRACE_S),  # the cap, as for a red
    ],
)
def test_a_worker_resumed_for_a_conflict_is_parked_once_it_is_over(
    tmp_path: Path, transcripts: Path, sha: str, mergeable: str, ago: float
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, [], sha=sha, mergeable=mergeable)
    resumed_501_for_conflict(tmp_path, fake, ago=ago)
    code, _, _, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))


def test_a_conflict_already_told_is_resumed_again_without_a_run(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """30/10, #2094: parked in its merge, its conflict « told » for its head —
    never resumed again, and the PR stayed in conflict an hour, no one on
    it. A conflict without a session nor a run is always resumed."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, [], mergeable="CONFLICTING")
    parking.mark_resumed(env.settings, 520, f"{HEAD}:conflict,", 0)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    assert launched == []


def test_a_conflict_already_told_with_its_run_going_is_not_resumed_again(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """A run still going: tracerworks cancels it (#2013), then the resume."""
    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RUNNING, mergeable="CONFLICTING")
    parking.mark_resumed(env.settings, 520, f"{HEAD}:conflict,", 0)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []


def test_a_resume_due_while_claude_is_not_the_provider_holds_the_slot(
    tmp_path: Path, launched: list[tuple[int, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    import tracerworks.cli as cli
    from tracerworks.guards import Admission
    from tracerworks.providers import CODEX

    fake, env, _, err = resume_world(tmp_path)
    answer_state(fake, 720, RED)
    monkeypatch.setattr(
        cli, "admission", lambda *a, **k: Admission(CODEX, "provider codex")
    )

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "the resume waits, before any new spec" in err.getvalue()
    assert resumed_argv(fake) == [] and launched == []


def test_a_resume_whose_record_fails_still_opens_one_window_only(
    tmp_path: Path, launched: list[tuple[int, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    import sqlite3

    import tracerworks.cli as cli

    fake, env, _, err = resume_world(tmp_path)
    parking.park(env.settings, 510, SESSION, 710, 0)
    fake.answer(
        "git", "-C", str(tmp_path / "wt" / "510"), "log", "-1", "--format=%ct",
        stdout="1\n",
    )  # fmt: skip
    answer_state(fake, 720, RED)
    answer_state(fake, 710, RED)

    def broken(*_: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cli, "mark_resumed", broken)
    assert run(env, ["start-next"]) == EXIT_OK
    assert len(resumed_argv(fake)) == 1 and launched == []
    assert "resumed, but its resume NOT recorded" in err.getvalue()
    assert "NOT resumed" not in err.getvalue()


def test_a_busy_codex_worker_is_never_parked(tmp_path: Path, transcripts: Path) -> None:
    """It could not be resumed: only the at-rest rule of 27/09 applies."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619, pane=shells(fixture("pane-claude-idle.txt")))
    answer_state(fake, 619, RUNNING)
    listing = fixture("list-windows.txt").replace(
        "worker-501\tclaude\t1790413000\tclaude",
        "worker-501\tcodex\t1790413000\tcodex",
    )
    assert listing != fixture("list-windows.txt")
    fake.answer(*LIST_WINDOWS, stdout=listing)
    code, env, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — PR #619 open, but a shell is still running" in out
    assert records(env) == {}


def test_a_merged_worker_watching_its_checks_in_the_foreground_is_parked(
    tmp_path: Path, transcripts: Path
) -> None:
    """08/10: the merged #1021, working on its watch, held its slot."""
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601, checks=RUNNING)
    fake.answer(
        *TMUX, "capture-pane", "-p", "-t", target("worker-501"),
        stdout=fixture("pane-claude-working.txt"),
    )  # fmt: skip
    code, env, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert "#501: PR #601 merged, its checks still run — worker-501 parked" in out
    assert records(env)[501].pull == 601
