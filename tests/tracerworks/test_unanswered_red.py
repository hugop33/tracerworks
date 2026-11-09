"""A red told but left unanswered is told once more (#2175).

31/10: #2131's worker was resumed at 03:19 UTC for a red of the merge queue,
left a diagnostic line uncommitted, and its session was gone at 03:34. The
red, « told », never woke it again: 4 h 30 without anyone on it, the new
specs first. Now: resumed for the red its PR still shows, with neither a push
nor a comment since, the next free session resumes it once more — its
uncommitted file named — and then never again. `status` says it red.
"""

from __future__ import annotations

import io
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.tracerworks.fakes import TMUX, FakeRunner
from tests.tracerworks.test_clean import (
    HEAD,
    RED,
    answer_state,
    clean_world,
    cleaned,
    waiting_501,
)
from tests.tracerworks.test_parking import (
    SESSION,
    records,
    resume_world,
    resumed_501_for_red,
    resumed_argv,
)
from tests.tracerworks.test_parking import transcripts as transcripts
from tests.tracerworks.test_start_stop import make_env, world
from tracerworks import chantier_state, parking, store
from tracerworks.cleanup import Checks
from tracerworks.cli import EXIT_OK, Env, run
from tracerworks.git import parse_dirty
from tracerworks.github import TRACERWORKS_SIGNATURE
from tracerworks.parking import AGAIN, Parked, PullState

TOLD = f"{HEAD}:red"
#: the first resume of 31/10
RESUMED_AT = 1_793_416_756  # 31/10 03:19:16 UTC


def red(
    *, sha: str = HEAD, conflict: bool = False, at: float | None = None
) -> PullState:
    return PullState(
        number=720,
        state="OPEN",
        head_sha=sha,
        conflicting=conflict,
        checks=Checks(running=[], failed=["integration tranche 2/2"]),
        commented_at=at,
    )


def resumed(told: str | None = TOLD, at: int | None = RESUMED_AT) -> Parked:
    return Parked(520, SESSION, 720, RESUMED_AT - 3600, told, resumed_at=at)


# --- the rule, pure ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pull", "record", "due"),
    [
        (red(), resumed(), True),  # 31/10: no push, no comment
        (red(at=RESUMED_AT - 600), resumed(), True),  # a comment before the resume
        (red(at=RESUMED_AT + 360), resumed(), False),  # answered: a comment since
        (red(at=float("inf")), resumed(), False),  # a comment without a date
        (red(sha="d" * 40), resumed(), False),  # pushed: a new head, its own red
        (red(), resumed(TOLD + AGAIN), False),  # told twice already
        (red(), resumed(at=None), False),  # seen at rest, never resumed for it
        (red(conflict=True), resumed(f"{HEAD}:conflict,red"), False),  # its own rule
    ],
    ids=[
        "unanswered", "comment before", "comment since", "undated comment",
        "pushed", "told twice", "never resumed", "conflict",
    ],
)  # fmt: skip
def test_what_is_told_once_more(pull: PullState, record: Parked, due: bool) -> None:
    assert parking.unanswered(pull, record) is due


def test_told_twice_is_never_due_again() -> None:
    assert parking.due_on_open(red(), resumed())
    assert not parking.due_on_open(red(), resumed(TOLD + AGAIN))
    # pushed after its second telling: the new head's red is news
    assert parking.due_on_open(red(sha="d" * 40), resumed(TOLD + AGAIN))


def test_its_second_resume_has_its_grace() -> None:
    """#1539: a worker just resumed, at rest between two commands, is not
    parked at once — its second resume too."""
    record = resumed(TOLD + AGAIN, at=RESUMED_AT)
    assert parking.in_resume_grace(red(), record, RESUMED_AT + 60)


def test_parked_on_what_it_was_resumed_for_keeps_the_resume(tmp_path: Path) -> None:
    """The 31/10 path as the spec tells it: resumed, at rest past the grace,
    parked on the same red — still unanswered. Parked on its second telling,
    or on a red it saw live: no resume to answer, never told again."""
    env, _, _ = make_env(world(tmp_path), tmp_path)
    parking.park(env.settings, 520, SESSION, 720, 0)
    parking.mark_resumed(env.settings, 520, TOLD, RESUMED_AT)
    parking.park(env.settings, 520, SESSION, 720, RESUMED_AT + 900, seen=TOLD)
    assert parking.parked(env.settings)[520].resumed_at == RESUMED_AT

    parking.mark_resumed(env.settings, 520, TOLD + AGAIN, RESUMED_AT + 1000)
    parking.park(env.settings, 520, SESSION, 720, RESUMED_AT + 2000, seen=TOLD)
    record = parking.parked(env.settings)[520]
    assert (record.resumed_for, record.resumed_at) == (TOLD, None)
    assert not parking.due_on_open(red(), record)


def test_the_text_names_its_first_resume_and_its_uncommitted_files() -> None:
    text = parking.again_text(
        720, resumed(), ["tests/integration/test_log_codes.py", "notes.md"]
    )
    assert text.startswith("Seconde reprise pour ce rouge")
    assert "31/10 03:19 (UTC)" in text and "PR #720" in text
    assert "`tests/integration/test_log_codes.py`, `notes.md`" in text
    assert "dernière reprise" in text and "Rouge sans rapport — " in text

    many = parking.again_text(720, resumed(), [f"f{i}" for i in range(13)])
    assert "`f9` (+3)" in many and "f10" not in many
    assert "Non commité" not in parking.again_text(720, resumed(), [])
    assert "`git status` illisible" in parking.again_text(720, resumed(), None)


@pytest.mark.parametrize(
    ("porcelain", "paths"),
    [
        ("", []),
        (" M tests/a.py\0?? notes.md\0", ["tests/a.py", "notes.md"]),
        ("R  new name.py\0old.py\0A  b.py\0", ["new name.py", "b.py"]),
    ],
    ids=["clean", "changed and untracked", "a rename"],
)
def test_what_is_uncommitted(porcelain: str, paths: list[str]) -> None:
    assert parse_dirty(porcelain) == paths


# --- the 31/10 scenario, in start-next ----------------------------------------------


def told_once(tmp_path: Path) -> tuple[FakeRunner, Env, io.StringIO, io.StringIO]:
    """#520 parked on PR #720, resumed for its red an hour ago; its session
    gone since, a diagnostic line left uncommitted."""
    fake, env, out, err = resume_world(tmp_path)
    parking.mark_resumed(env.settings, 520, TOLD, env.now() - 3600)
    fake.answer(
        "git", "-C", str(tmp_path / "wt" / "520"), "status", "--porcelain=v1", "-z",
        stdout=" M tests/integration/test_log_codes.py\0",
    )  # fmt: skip
    return fake, env, out, err


def test_an_unanswered_red_is_told_once_more_then_never(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake, env, out, err = told_once(tmp_path)
    answer_state(fake, 720, RED)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    [argv] = resumed_argv(fake)
    assert f"--resume {SESSION}" in argv[-1]
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert reason.startswith("Seconde reprise pour ce rouge")
    assert "`tests/integration/test_log_codes.py`" in reason
    assert "des checks ont fini rouges" in reason  # the red, told as the first time
    assert launched == []  # before any new spec
    assert (
        "#520: PR #720 red, its resume left unanswered (no push, no comment) —"
        " worker-520 resumed once more" in out.getvalue()
    )
    assert records(env)[520].resumed_for == TOLD + AGAIN

    # its session gone again, still no answer: never a third time
    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    assert [n for n, _, _ in launched] == [565]


def test_a_worker_that_commented_is_not_resumed(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Counter-proof: it answered on its PR — the parked-red alert is the
    operator's, the queue goes on."""
    fake, env, _, err = told_once(tmp_path)
    stamp = datetime.fromtimestamp(env.now() - 1800, UTC)
    answer_state(
        fake, 720, RED, comments=["Je regarde."],
        commented_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )  # fmt: skip

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert resumed_argv(fake) == []
    assert [n for n, _, _ in launched] == [565]


def test_tracerworks_s_own_comment_is_no_answer(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """tracerworks comments under the operator's login, as a worker does: its
    signed comments (the queue's, a conflict's) are never the worker's."""
    fake, env, _, err = told_once(tmp_path)
    stamp = datetime.fromtimestamp(env.now() - 1800, UTC)
    answer_state(
        fake, 720, RED,
        comments=["La PR sort de la file." + TRACERWORKS_SIGNATURE],
        commented_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )  # fmt: skip

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    assert records(env)[520].resumed_for == TOLD + AGAIN


def test_a_worker_that_pushed_is_resumed_for_its_new_red_as_before(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """Counter-proof: a push is an answer — its new head's red is told as
    any first red, never as a second telling."""
    fake, env, _, err = told_once(tmp_path)
    answer_state(fake, 720, RED, sha="d" * 40)

    assert run(env, ["start-next"]) == EXIT_OK, err.getvalue()
    assert len(resumed_argv(fake)) == 1
    reason = (tmp_path / "wt" / ".primers" / "520-resume.md").read_text()
    assert "Seconde reprise" not in reason
    assert records(env)[520].resumed_for == f"{'d' * 40}:red"
    assert not fake.ran(
        "git", "-C", str(tmp_path / "wt" / "520"), "status", "--porcelain=v1"
    )


def test_a_worker_at_rest_on_its_unanswered_red_is_parked_then_told_again(
    tmp_path: Path, transcripts: Path
) -> None:
    """The path the spec tells: at rest past its grace, parked — and its
    resume kept, so `start-next` tells it once more."""
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    answer_state(fake, 619, RED)
    resumed_501_for_red(tmp_path, fake, ago=parking.RESUME_GRACE_S)
    code, env, _, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window")
    pull = parking.pull_state(fake, "owner/repo", 619, ("operator-login",))
    assert parking.due_on_open(pull, records(env)[501])


# --- status ---------------------------------------------------------------------------


def test_status_says_red_for_a_parked_chantier_serve_read_red(tmp_path: Path) -> None:
    """31/10: « light check (PR #2152, its session closed) » for 4 h 30, its
    pull request red — `status` reads no GitHub; serve's last pass did."""
    fake = world(tmp_path)
    env, out, _ = make_env(fake, tmp_path)
    parking.park(env.settings, 520, SESSION, 720, 0)
    state = chantier_state.derive(
        chantier_state.Facts(parked_pull=720, ci_red=True, pull=720)
    )
    with closing(store.connect(env.settings.history_db)) as db:
        chantier_state.record(db, env.settings.github_repo, 520, state, None, 1)

    assert run(env, ["status"]) == EXIT_OK
    [line] = [x for x in out.getvalue().splitlines() if x.startswith("#520")]
    assert "parked (PR #720 red, not resumed yet)" in line
    assert "light check" not in line
