"""`tracerworks clean`: a finished chantier's worktree goes, after its usage is posted.

The recorded world of `test_start_stop` (#501 and #510 in their windows, #520
stopped with 3 commits never pushed) plus two stopped chantiers whose work is
all pushed: #540, merged into the base branch, and #550, not yet merged. Only
#540 may go; and only after ONE usage comment on its spec.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import REPO, TMUX, FakeRunner, fixture, target
from tests.tracerworks.test_start_stop import make_env, world
from tests.tracerworks.test_usage import _worktree_logs
from tracerworks import store
from tracerworks.cleanup import RED_MARKER, USAGE_MARKER, usage_comment
from tracerworks.cli import EXIT_BROKEN, EXIT_OK, Env, run
from tracerworks.messaging import Delivery
from tracerworks.process import Result
from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime
from tracerworks.usage import chantier_usage

PUSHED = """# branch.oid {oid}
# branch.head {branch}
# branch.upstream origin/{branch}
# branch.ab +0 -0
"""


def clean_world(
    tmp_path: Path, comments: Sequence[str] = (), slug_540: str = "fini"
) -> FakeRunner:
    fake = world(tmp_path)
    root = tmp_path / "wt"
    listing = fixture("worktree-list.txt").replace(
        "/home/dev/.agent-worktrees", str(root)
    )
    for n, oid in ((540, "4" * 40), (550, "5" * 40)):
        name = f"agent/{n}-{slug_540 if n == 540 else 'fini'}"
        branch = f"refs/heads/{name}"
        listing += f"\nworktree {root / str(n)}\nHEAD {oid}\nbranch {branch}\n"
        fake.answer(
            "git",
            "-C",
            str(root / str(n)),
            "status",
            "--porcelain=v2",
            "--branch",
            stdout=PUSHED.format(oid=oid, branch=name),
        )
        fake.answer(
            "git", "-C", str(root / str(n)), "log", "-1", "--format=%ct", stdout="1\n"
        )
    fake.answer(
        "git", "-C", str(REPO), "worktree", "list", "--porcelain", stdout=listing
    )
    # #540 takes 1,5 Go on disk.
    fake.answer("du", "-sk", str(root / "540"), stdout=f"1572864\t{root / '540'}\n")
    fake.answer("git", "-C", str(REPO), "fetch", "--quiet", "origin", "main")
    ancestor = ("merge-base", "--is-ancestor", "HEAD", "origin/main")
    fake.answer("git", "-C", str(root / "540"), *ancestor)
    fake.answer("git", "-C", str(root / "550"), *ancestor, returncode=1)
    fake.answer("git", "-C", str(REPO), "worktree", "remove", prefix=True)
    payload = json.dumps(
        {
            "comments": [
                {"body": body, "author": {"login": "operator-login"}}
                for body in comments
            ]
        }
    )
    fake.answer(
        "gh",
        "issue",
        "view",
        "540",
        "--repo",
        "owner/repo",
        "--json",
        "comments",
        stdout=payload,
    )
    fake.answer("gh", "issue", "comment", "540", prefix=True)
    for n, slug in ((540, slug_540), (501, "console-operateur")):
        fake.answer(*pr_list(f"agent/{n}-{slug}"), stdout="[]")
    # #501 works, everything pushed: not merged, no open PR — kept (#1025).
    fake.answer("git", "-C", str(root / "501"), *ancestor, returncode=1)
    answer_open_pull(fake, None)
    answer_open_issues(fake, [])  # no spec has an open ticket left
    return fake


def answer_open_pull(fake: FakeRunner, pull: int | None) -> None:
    opened = "[]" if pull is None else json.dumps([{"number": pull}])
    fake.answer(
        "gh", "pr", "list", "--repo", "owner/repo", "--head",
        "agent/501-console-operateur", "--state", "open", "--json", "number",
        "--limit", "1", stdout=opened,
    )  # fmt: skip


def pr_list(branch: str) -> tuple[str, ...]:
    return (
        "gh",
        "pr",
        "list",
        "--repo",
        "owner/repo",
        "--head",
        branch,
        "--state",
        "merged",
        "--json",
        "number",
        "--limit",
        "1",
    )


GREEN = [
    {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
     "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "compose", "status": "COMPLETED",
     "conclusion": "SKIPPED"},
    {"__typename": "StatusContext", "context": "deploy", "state": "SUCCESS"},
]  # fmt: skip
RED = [*GREEN, {"__typename": "CheckRun", "name": "browser",
                "status": "COMPLETED", "conclusion": "FAILURE"}]  # fmt: skip
RUNNING = [*GREEN, {"__typename": "CheckRun", "name": "browser",
                    "status": "IN_PROGRESS", "conclusion": ""}]  # fmt: skip


def answer_checks(fake: FakeRunner, pull: int, rollup: list[dict[str, str]]) -> None:
    fake.answer(
        "gh", "pr", "view", str(pull), "--repo", "owner/repo", "--json",
        "statusCheckRollup", stdout=json.dumps({"statusCheckRollup": rollup}),
    )  # fmt: skip


def by_operator(body: str) -> dict[str, object]:
    """A comment as `gh --json comments` gives it, by the operator (#1529)."""
    return {"body": body, "author": {"login": "operator-login"}}


def answer_comments(fake: FakeRunner, spec: int, bodies: Sequence[str]) -> None:
    fake.answer(
        "gh", "issue", "view", str(spec), "--repo", "owner/repo", "--json",
        "comments", stdout=json.dumps({"comments": [by_operator(b) for b in bodies]}),
    )  # fmt: skip


def finished_501(
    fake: FakeRunner,
    tmp_path: Path,
    pull: int | None,
    checks: list[dict[str, str]] = GREEN,
    comments: Sequence[str] = (),
) -> None:
    """#501 at rest in its window, everything pushed, its HEAD in the base."""
    fake.answer(
        *TMUX, "capture-pane", "-p", "-t", target("worker-501"),
        stdout=fixture("pane-claude-idle.txt"),
    )  # fmt: skip
    wt = tmp_path / "wt" / "501"
    fake.answer(
        "git", "-C", str(wt), "merge-base", "--is-ancestor", "HEAD", "origin/main"
    )
    fake.answer("du", "-sk", str(wt), stdout=f"1024\t{wt}\n")
    merged = "[]" if pull is None else json.dumps([{"number": pull}])
    fake.answer(*pr_list("agent/501-console-operateur"), stdout=merged)
    fake.answer(
        "gh", "issue", "view", "501", "--repo", "owner/repo", "--json", "state",
        stdout='{"state": "OPEN"}',
    )  # fmt: skip
    fake.answer("gh", "issue", "close", "501", prefix=True)
    fake.answer("gh", "issue", "comment", "501", prefix=True)
    answer_comments(fake, 501, comments)
    if pull is not None:
        answer_checks(fake, pull, checks)


@pytest.fixture
def transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Claude Code's logs of #540's worktree, under a config dir of the test."""
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    _worktree_logs(config / "projects", tmp_path / "wt" / "540")
    return config


def cleaned(tmp_path: Path, fake: FakeRunner, *args: str) -> tuple[int, Env, str, str]:
    env, out, err = make_env(fake, tmp_path)
    code = run(env, ["clean", *args])
    return code, env, out.getvalue(), err.getvalue()


def removed(fake: FakeRunner) -> list[str]:
    return [c.argv[-1] for c in fake.ran("git", "-C", str(REPO), "worktree", "remove")]


def posted(fake: FakeRunner) -> list[bytes | None]:
    return [c.stdin for c in fake.ran("gh", "issue", "comment")]


def test_a_worker_with_an_open_window_is_kept(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert "#501 kept — window worker-501 open" in out
    assert "#510 kept — window worker-510 open" in out
    assert str(tmp_path / "wt" / "501") not in removed(fake)


def test_unpushed_work_is_kept(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert "#520 kept — 3 unpushed commit(s) (branch never pushed)" in out
    assert str(tmp_path / "wt" / "520") not in removed(fake)


def test_an_unreadable_git_state_is_kept(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    fake.answer(
        "git",
        "-C",
        str(tmp_path / "wt" / "540"),
        "status",
        "--porcelain=v2",
        "--branch",
        returncode=128,
    )
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert "#540 kept — git state unreadable" in out
    assert removed(fake) == [] and posted(fake) == []


def test_a_chantier_not_yet_merged_is_kept(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert "#550 kept — not merged into origin/main yet" in out
    assert str(tmp_path / "wt" / "550") not in removed(fake)
    assert not fake.ran("gh", "issue", "view", "550")


def test_a_merged_chantier_gets_one_usage_comment_then_its_worktree_goes(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [body] = posted(fake)
    assert body is not None
    text = body.decode("utf-8")
    assert text.startswith(USAGE_MARKER)
    assert "délégué « Implement #578 »" in text and "Durée : 2 h 10" in text
    assert "Worktree : 1.5 GB (supprimé)." in text
    [comment] = fake.ran("gh", "issue", "comment")
    assert comment.argv[-2:] == ("--body-file", "-")
    assert not any("Consommation" in arg for arg in comment.argv)
    assert removed(fake) == [str(tmp_path / "wt" / "540")]
    [remove] = fake.ran("git", "-C", str(REPO), "worktree", "remove")
    assert "--force" not in remove.argv
    # The comment is posted BEFORE the worktree goes.
    assert fake.calls.index(comment) < fake.calls.index(remove)
    assert "#540: usage posted on #540; worktree" in out
    assert "removed (1.5 GB)" in out
    assert out.splitlines()[-1] == "freed: 1.5 GB (1 worktree(s))"
    assert transcripts.exists()  # Claude's logs are never touched


def test_a_second_run_does_not_post_again(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path, comments=["merci", f"{USAGE_MARKER}\ndéjà là"])
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert posted(fake) == []
    assert removed(fake) == [str(tmp_path / "wt" / "540")]
    assert "already posted" in out


def test_without_transcript_nothing_is_posted_and_it_still_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    fake = clean_world(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert posted(fake) == []
    assert removed(fake) == [str(tmp_path / "wt" / "540")]
    assert "no Claude session" in out


def test_dry_run_changes_nothing(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake, "--dry-run")

    assert code == EXIT_OK
    assert posted(fake) == [] and removed(fake) == []
    assert "#540: would post the usage on #540, then would remove" in out
    assert "(1.5 GB) (--dry-run)" in out
    assert out.splitlines()[-1] == "would free: 1.5 GB (1 worktree(s))"
    assert "#550 kept" in out


def test_a_failed_comment_keeps_the_worktree(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    fake.on("gh", "issue", "comment", "540", prefix=True)(
        Result(1, "", "HTTP 502: bad gateway")
    )
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert len(posted(fake)) == 1
    assert removed(fake) == []
    assert "NOT posted" in err and "bad gateway" in err and "worktree kept" in err


def test_an_unreadable_comment_list_keeps_the_worktree(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    fake.on("gh", "issue", "view", "540", prefix=True)(Result(1, "", "offline"))
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert posted(fake) == [] and removed(fake) == []
    assert "worktree kept" in err


def test_git_refusing_the_removal_is_an_error_not_a_force(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    fake.on("git", "-C", str(REPO), "worktree", "remove", prefix=True)(
        Result(128, "", "fatal: contains modified or untracked files")
    )
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert "modified or untracked" in err
    assert all("--force" not in c.argv for c in fake.calls)


def test_an_unknown_size_is_said_and_never_blocks(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    fake.answer("du", "-sk", str(tmp_path / "wt" / "540"), returncode=1)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [body] = posted(fake)
    assert body is not None and "Worktree : ? (supprimé)." in body.decode()
    assert removed(fake) == [str(tmp_path / "wt" / "540")]
    assert out.splitlines()[-1] == "freed: 0 MB + unknown sizes (1 worktree(s))"


def test_the_comment_format(tmp_path: Path) -> None:
    worktree = Path("/home/dev/.agent-worktrees/577")
    _worktree_logs(tmp_path, worktree)
    usage = chantier_usage(tmp_path, worktree)
    assert usage is not None

    assert usage_comment(577, usage, 943718) == (
        f"{USAGE_MARKER}\n"
        "**Consommation du chantier #577** — mesurée par `tracerworks clean` à la"
        " suppression du worktree, dans les journaux de Claude Code.\n"
        "\n"
        "| | total | entrée | cache écrit | cache lu | sortie | réponses |\n"
        "|---|---:|---:|---:|---:|---:|---:|\n"
        "| owner | 583 | 3 | 100 | 450 | 30 | 2 |\n"
        "| délégué « Implement #578 » | 345 | 5 | 300 | 0 | 40 | 1 |\n"
        "| délégués (total) | 345 | 5 | 300 | 0 | 40 | 1 |\n"
        "| **chantier** | 928 | 8 | 400 | 450 | 70 | 3 |\n"
        "\n"
        # #2253: these logs name no model nor effort
        "Modèle · effort : inconnu · inconnu (constatés dans le journal de la"
        " dernière session du worker).\n"
        "\n"
        "Durée : 2 h 10 (première → dernière activité).\n"
        "\n"
        "Worktree : 0.9 GB (supprimé).\n"
    )


# --- a finished worker, still in its window (26/09) ---------------------------


def test_a_finished_worker_at_rest_is_closed_its_worktree_goes_and_its_spec_closes(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [kill] = fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert str(tmp_path / "wt" / "501") in removed(fake)
    [remove] = [
        c
        for c in fake.ran("git", "-C", str(REPO), "worktree", "remove")
        if c.argv[-1] == str(tmp_path / "wt" / "501")
    ]
    assert fake.calls.index(kill) < fake.calls.index(remove)
    [close] = fake.ran("gh", "issue", "close", "501")
    assert fake.calls.index(remove) < fake.calls.index(close)
    assert "#601" in " ".join(close.argv)
    assert "#501: PR #601 merged, worker-501 closed" in out
    assert "#501: spec closed (delivered by #601)" in out


def test_a_delivered_spec_closes_when_its_folder_is_no_longer_a_worktree(
    tmp_path: Path, transcripts: Path
) -> None:
    """#1907 (27/10, 14:15 UTC): « git worktree remove failed (exit 128):
    fatal: '…/1907' is not a working tree » — the spec stayed open, first
    in the queue, and no later pass saw it again. Now: git's list pruned,
    the folder said, the spec closed, the usage posted once."""
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601)
    wt = tmp_path / "wt" / "501"
    wt.mkdir(parents=True)  # the folder is still there, no longer git's
    fake.on("git", "-C", str(REPO), "worktree", "remove", str(wt))(
        Result(128, "", f"fatal: '{wt}' is not a working tree")
    )
    fake.answer("git", "-C", str(REPO), "worktree", "prune")
    code, _, out, err = cleaned(tmp_path, fake)

    assert code == EXIT_OK, err
    assert len(fake.ran("git", "-C", str(REPO), "worktree", "prune")) == 1
    [close] = fake.ran("gh", "issue", "close", "501")
    assert "#601" in " ".join(close.argv)
    assert "#501: spec closed (delivered by #601)" in out
    assert f"{wt} was no longer a git worktree — git's list pruned" in out
    assert "its folder is still there" in out
    assert wt.exists(), "never deleted blind"
    assert len(fake.ran("gh", "issue", "comment", "501")) <= 1  # usage, once


def test_a_removal_git_refuses_keeps_the_worktree_and_closes_the_spec(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601)
    wt = tmp_path / "wt" / "501"
    fake.on("git", "-C", str(REPO), "worktree", "remove", str(wt))(
        Result(128, "", "fatal: contains modified or untracked files")
    )
    code, _, out, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert "modified or untracked" in err
    assert not fake.ran("git", "-C", str(REPO), "worktree", "prune")
    assert "#501: spec closed (delivered by #601)" in out
    assert f"worktree {wt} removed" not in out


def test_a_worker_at_rest_without_a_merged_pull_request_is_kept(
    tmp_path: Path, transcripts: Path
) -> None:
    # A fresh branch with no commit is also « in the base »: only a merged PR
    # says the chantier is delivered.
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=None)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert "#501 kept — window worker-501 open, no merged PR" in out
    assert not fake.ran(*TMUX, "kill-window")
    assert not fake.ran(*TMUX, "send-keys")
    assert str(tmp_path / "wt" / "501") not in removed(fake)
    assert not fake.ran("gh", "issue", "close")


def test_dry_run_closes_nothing(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601)
    code, _, out, _ = cleaned(tmp_path, fake, "--dry-run")

    assert code == EXIT_OK
    assert "#501: PR #601 merged, would close worker-501" in out
    assert not fake.ran(*TMUX, "send-keys") and not fake.ran(*TMUX, "kill-window")
    assert removed(fake) == [] and not fake.ran("gh", "issue", "close")


# --- a merged PR's checks are waited for, red ones told once (05/10) ---------


def test_a_merged_pr_red_after_the_merge_is_told_to_its_worker_once(
    tmp_path: Path, transcripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    told: list[tuple[int, str]] = []

    def send(
        _: object, spec: int, text: str, stamp: str, wait_idle: bool = False
    ) -> Delivery:
        told.append((spec, text))
        return Delivery(True, "m-abcdef", ["received"])

    monkeypatch.setattr(TmuxWorktreeRuntime, "send", send)
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601, checks=RED)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [(spec, text)] = told
    assert spec == 501 and "#601" in text and "browser" in text
    assert "NOUVELLE branche" in text and "origin/main" in text
    [comment] = [c.stdin for c in fake.ran("gh", "issue", "comment", "501")]
    assert comment is not None
    assert RED_MARKER.format(pull=601) in comment.decode()
    assert "prévenu" in comment.decode()
    assert "#501: PR #601 merged but red after the merge (browser)" in out
    # The worker stays for this pass: it has to do something about it.
    assert not fake.ran(*TMUX, "kill-window")
    assert str(tmp_path / "wt" / "501") not in removed(fake)
    assert not fake.ran("gh", "issue", "close")


def test_a_red_already_told_closes_the_worker_as_usual(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    finished_501(
        fake, tmp_path, pull=601, checks=RED,
        comments=[f"{RED_MARKER.format(pull=601)}\nLa PR #601…"],
    )  # fmt: skip
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    assert not fake.ran("gh", "issue", "comment", "501")
    assert "#501: spec closed (delivered by #601)" in out


def test_a_worker_not_reached_is_not_marked_told(
    tmp_path: Path, transcripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def send(
        _: object, spec: int, text: str, stamp: str, wait_idle: bool = False
    ) -> Delivery:
        return Delivery(False, "m-abcdef", ["field still full"])

    monkeypatch.setattr(TmuxWorktreeRuntime, "send", send)
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601, checks=RED)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert "worker-501 not told: field still full" in out
    assert not fake.ran("gh", "issue", "comment", "501")
    assert not fake.ran(*TMUX, "kill-window")


def test_a_red_with_no_worker_left_keeps_the_spec_open_with_a_comment(
    tmp_path: Path, transcripts: Path
) -> None:
    # #540: no window (its worker gave its slot back before the merge).
    fake = clean_world(tmp_path)
    fake.answer(*pr_list("agent/540-fini"), stdout=json.dumps([{"number": 640}]))
    answer_checks(fake, 640, RED)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert str(tmp_path / "wt" / "540") in removed(fake)
    bodies = [b.decode() for b in posted(fake) if b is not None]
    [red] = [b for b in bodies if RED_MARKER.format(pull=640) in b]
    assert "reste ouvert" in red
    assert not fake.ran("gh", "issue", "close", "540")
    assert "#540: spec kept open — PR #640 red after the merge (browser)" in out


def test_unreadable_checks_keep_the_worker(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    finished_501(fake, tmp_path, pull=601)
    fake.answer(
        "gh", "pr", "view", "601", "--repo", "owner/repo", "--json",
        "statusCheckRollup", stdout="not json",
    )  # fmt: skip
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert "#501" in err and "kept" in err
    assert not fake.ran(*TMUX, "kill-window")


# --- a worker waiting on its open PR gives its slot back (27/09) --------------


def waiting_501(
    fake: FakeRunner, tmp_path: Path, pull: int | None, pane: str | None = None
) -> None:
    """#501 at rest, everything pushed, NOT merged, maybe with an open PR."""
    fake.answer(
        *TMUX, "capture-pane", "-p", "-t", target("worker-501"),
        stdout=pane if pane is not None else fixture("pane-claude-idle.txt"),
    )  # fmt: skip
    wt = tmp_path / "wt" / "501"
    ancestor = ("merge-base", "--is-ancestor", "HEAD", "origin/main")
    fake.answer("git", "-C", str(wt), *ancestor, returncode=1)
    answer_open_pull(fake, pull)
    if pull is not None:
        answer_state(fake, pull, GREEN)


#: The head commit of an open PR in these tests.
HEAD = "c" * 40


def answer_state(
    fake: FakeRunner,
    pull: int,
    rollup: Sequence[dict[str, str]],
    *,
    state: str = "OPEN",
    sha: str = HEAD,
    mergeable: str = "MERGEABLE",
    comments: Sequence[str] = (),
    commented_at: str | None = None,
) -> None:
    """`parking.pull_state`'s one read of a pull request, its comments with it
    (a worker's « red unrelated » mark, #1280), dated `commented_at` (an ISO
    time; none: GitHub gives no date, an answer whenever, #2175)."""
    date = {} if commented_at is None else {"createdAt": commented_at}
    payload = {
        "state": state,
        "headRefOid": sha,
        "mergeable": mergeable,
        "statusCheckRollup": list(rollup),
        "comments": [
            {"body": body, "author": {"login": "operator-login"}, **date}
            for body in comments
        ],
    }
    fake.answer(
        "gh", "pr", "view", str(pull), "--repo", "owner/repo", "--json",
        "state,headRefOid,mergeable,statusCheckRollup,comments",
        stdout=json.dumps(payload),
    )  # fmt: skip


def test_a_worker_waiting_on_its_open_pr_gives_its_slot_back(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert fake.ran(*TMUX, "kill-window", "-t", target("worker-501"))
    # The worktree, the branch, the spec: all kept.
    assert str(tmp_path / "wt" / "501") not in removed(fake)
    assert not fake.ran("gh", "issue", "close")
    assert "#501: PR #619 open, worker-501 closed — parked" in out


def test_a_worker_without_an_open_pr_keeps_its_slot(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=None)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — not merged into origin/main yet" in out


def test_a_worker_whose_test_still_runs_in_a_shell_keeps_its_slot(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    busy = fixture("pane-claude-idle.txt").replace(
        "· ← for agents", "· 2 shells · ← for agents"
    )
    assert "2 shells" in busy
    waiting_501(fake, tmp_path, pull=619, pane=busy)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window")
    assert "#501 kept — PR #619 open, but a shell is still running" in out


def test_dry_run_gives_nothing_back(tmp_path: Path, transcripts: Path) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    code, _, out, _ = cleaned(tmp_path, fake, "--dry-run")

    assert code == EXIT_OK
    assert not fake.ran(*TMUX, "kill-window") and not fake.ran(*TMUX, "send-keys")
    assert "#501: PR #619, would park worker-501 and give its slot back" in out


def chantiers(tmp_path: Path) -> list[tuple[int, str]]:
    base = tmp_path / "history" / "factory.sqlite3"
    if not base.exists():
        return []
    with closing(store.connect(base)) as db:
        return db.execute("SELECT spec, source FROM chantier").fetchall()


def test_a_merged_chantier_goes_into_the_history(
    tmp_path: Path, transcripts: Path
) -> None:
    code, _, _, _ = cleaned(tmp_path, clean_world(tmp_path))
    assert code == EXIT_OK
    assert chantiers(tmp_path) == [(540, "clean")]


def test_dry_run_writes_no_history(tmp_path: Path, transcripts: Path) -> None:
    cleaned(tmp_path, clean_world(tmp_path), "--dry-run")
    assert chantiers(tmp_path) == []


def test_a_history_base_that_fails_never_keeps_the_worktree(
    tmp_path: Path, transcripts: Path
) -> None:
    (tmp_path / "history").write_text("a file where the folder should be")
    fake = clean_world(tmp_path)
    code, _, _, err = cleaned(tmp_path, fake)
    assert code == EXIT_OK
    assert removed(fake) == [str(tmp_path / "wt" / "540")]
    assert "#540: history NOT written" in err


def test_a_window_on_a_permission_prompt_is_never_closed() -> None:
    """#984: closing it sends Escape — « No » to the prompt. tracerworks never
    answers it: the operator does."""
    from tracerworks.cleanup import why_kept
    from tracerworks.git import Saved
    from tracerworks.observe import Worker, WorkerState
    from tracerworks.runtime.tmux import Window

    worker = Worker(
        spec=976,
        state=WorkerState.ASKING,
        provider="claude",
        window=Window("worker-976", "claude", 0, "claude"),
        worktree=None,
        saved=Saved(upstream="origin/x", unpushed=0, dirty=0),
        last_activity=None,
        asking="Bash command: rm node_modules",
    )
    assert why_kept(worker, "worker-976") is not None


# --- the spec's mode label follows it to its pull request (#1297) ---------------------


def test_clean_puts_the_spec_s_mode_label_back_on_its_open_pull_request(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    waiting_501(fake, tmp_path, pull=619)
    for n in (510, 520, 540, 550):
        fake.answer(
            "gh", "issue", "view", str(n), "--repo", "owner/repo", "--json",
            "labels", stdout='{"labels": []}',
        )  # fmt: skip
    fake.answer(
        "gh", "issue", "view", "501", "--repo", "owner/repo", "--json", "labels",
        stdout='{"labels": [{"name": "spec"}, {"name": "ci-github"}]}',
    )  # fmt: skip
    fake.answer(
        "gh", "pr", "list", "--repo", "owner/repo", "--head",
        "agent/501-console-operateur", "--state", "open", "--json", "number,labels",
        "--limit", "1", stdout='[{"number": 619, "labels": []}]',
    )  # fmt: skip
    fake.answer("gh", "api", "--paginate", prefix=True)
    fake.answer("gh", "pr", "edit", "619", prefix=True)
    env, out, err = make_env(fake, tmp_path)
    env = dataclasses.replace(
        env,
        settings=dataclasses.replace(
            env.settings, carried_labels=("ci-hybrid", "ci-vps", "ci-github")
        ),
    )

    code = run(env, ["clean"])

    assert code == EXIT_OK, err.getvalue()
    [edit] = fake.ran("gh", "pr", "edit")
    assert edit.argv[3:] == ("619", "--repo", "owner/repo", "--add-label", "ci-github")
    assert "#501: ci-github put back on PR #619, as on its spec" in out.getvalue()


# --- a spec with a ticket still open is never « delivered » (#1566) -----------

ISSUE_LIST = (
    "gh", "issue", "list", "-R", "owner/repo", "--state", "open", "--limit",
    "1000", "--json", "number,title,body,labels,assignees,author",
)  # fmt: skip


def answer_open_issues(fake: FakeRunner, issues: Sequence[tuple[Any, ...]]) -> None:
    """(number, body, label[, author]) — an empty label: none; no author:
    the operator's."""
    fake.answer(
        *ISSUE_LIST,
        stdout=json.dumps(
            [
                {"number": n, "title": f"#{n}", "body": body, "assignees": [],
                 "labels": [{"name": label}] if label else [],
                 "author": {"login": author}}
                for n, body, label, author in (
                    (*i, "operator-login") if len(i) == 3 else i for i in issues
                )
            ]
        ),
    )  # fmt: skip


def remembered_comments(fake: FakeRunner, spec: int) -> None:
    """The spec's comments are those `clean` posted: a second pass sees them."""

    def comments(_: Sequence[str], __: bytes | None) -> Result:
        sent = fake.ran("gh", "issue", "comment", str(spec))
        bodies = [c.stdin.decode() for c in sent if c.stdin is not None]
        return Result(0, json.dumps({"comments": [by_operator(b) for b in bodies]}), "")

    fake.on(
        "gh", "issue", "view", str(spec), "--repo", "owner/repo", "--json", "comments"
    )(comments)


def reopened_540(tmp_path: Path) -> FakeRunner:
    """The 08/10 scenario: #540's PR #640 merged and green, its worker gone,
    but its ticket #541 (added after the merge) still open and in progress;
    #542 open too, with no label; #561 in progress for another spec."""
    fake = clean_world(tmp_path)
    fake.answer(*pr_list("agent/540-fini"), stdout=json.dumps([{"number": 640}]))
    answer_checks(fake, 640, GREEN)
    answer_open_issues(
        fake,
        [
            (540, "Une spec", ""),
            (541, "## Parent\n\nParent : #540\n", "in-progress"),
            (542, "Parent : #540", ""),
            (561, "Parent : #560", "in-progress"),
            # #1529: a stranger's issue naming the spec is no ticket of it
            (543, "Parent : #540", "in-progress", "stranger"),
        ],
    )
    fake.answer("gh", "issue", "edit", prefix=True)
    remembered_comments(fake, 540)
    return fake


def kept_open(fake: FakeRunner) -> list[str]:
    return [b.decode() for b in posted(fake) if b and b"tracerworks:kept-open" in b]


def test_a_spec_with_a_ticket_still_open_stays_open_and_says_so_once(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = reopened_540(tmp_path)
    first = cleaned(tmp_path, fake)
    second = cleaned(tmp_path, fake)

    assert first[0] == second[0] == EXIT_OK
    assert str(tmp_path / "wt" / "540") in removed(fake)
    assert not fake.ran("gh", "issue", "close")
    [comment] = kept_open(fake)
    assert "Gardée ouverte : #541, #542 encore ouverts" in comment
    assert "#543" not in comment
    assert "#640" in comment
    assert (
        "#540: spec kept open — PR #640 merged, tickets still open: #541, #542"
        in (first[2])
    )
    edits = fake.ran("gh", "issue", "edit")
    assert edits and all(
        c.argv == ("gh", "issue", "edit", "541", "--repo", "owner/repo",
                   "--remove-label", "in-progress")
        for c in edits if c.argv[3] == "541"
    )  # fmt: skip
    # #1926: the launch label off the spec, once — with the comment
    launch = [c.argv for c in edits if c.argv[3] == "540"]
    assert launch == [
        ("gh", "issue", "edit", "540", "--repo", "owner/repo",
         "--remove-label", "to-launch"),
    ]  # fmt: skip


def test_a_spec_kept_open_waits_for_the_operator_before_its_next_tranche(
    tmp_path: Path, transcripts: Path
) -> None:
    """#1800 (25-26/10): #1858 merged with #1856 still open; tracerworks
    launched `-r2` in silence, then `-r3`, `-r4` — four pull requests for
    one chantier. Now `to-launch` comes off the spec, said on it, so
    `start-next` has nothing to launch until the operator puts it back
    (its rule: the label counts when he put it last, #1529)."""
    fake = reopened_540(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [comment] = kept_open(fake)
    assert "La tranche suivante ne part pas seule" in comment
    assert "`to-launch` est retiré de la spec. Repose-le pour la lancer" in comment
    assert "no next tranche before the operator puts `to-launch` back" in out
    edit = next(c for c in fake.calls if c.argv[:4] == ("gh", "issue", "edit", "540"))
    post = next(
        c
        for c in fake.ran("gh", "issue", "comment", "540")
        if c.stdin and b"tracerworks:kept-open" in c.stdin
    )
    assert fake.calls.index(edit) < fake.calls.index(post), "off, then said"

    # put back by the operator: the comment is there, never taken off again
    cleaned(tmp_path, fake)
    assert len(fake.ran("gh", "issue", "edit", "540")) == 1


def test_the_queue_admits_the_spec_again_once_to_launch_is_back() -> None:
    from tracerworks.frontier import BodyRules, Queue, parse_issues

    queue = Queue(frozenset({"ready-for-agent", "to-launch"}), "priority")
    rules = BodyRules(("Parent",), ("Blocked by",))

    def spec(*labels: str) -> Any:
        payload = [
            {"number": 1800, "title": "Spec", "body": "", "assignees": [],
             "labels": [{"name": name} for name in labels],
             "author": {"login": "operator-login"}}
        ]  # fmt: skip
        [found] = parse_issues(json.dumps(payload), rules)
        return found

    assert not queue.admits(spec("ready-for-agent"))  # kept open: waits
    assert queue.admits(spec("ready-for-agent", "to-launch"))  # his word


def test_a_spec_with_no_ticket_open_closes_as_before(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = reopened_540(tmp_path)
    answer_open_issues(fake, [(540, "Une spec", ""), (561, "Parent : #560", "x")])
    fake.answer(
        "gh", "issue", "view", "540", "--repo", "owner/repo", "--json", "state",
        stdout='{"state": "OPEN"}',
    )  # fmt: skip
    fake.answer("gh", "issue", "close", "540", prefix=True)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [close] = fake.ran("gh", "issue", "close", "540")
    assert (
        close.argv[-1] == "Livré par #640 (fusionnée) — fermé par `tracerworks clean`."
    )
    assert "#540: spec closed (delivered by #640)" in out
    assert not kept_open(fake) and not fake.ran("gh", "issue", "edit")


def test_dry_run_says_it_would_keep_the_spec_open(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = reopened_540(tmp_path)
    code, _, out, _ = cleaned(tmp_path, fake, "--dry-run")

    assert code == EXIT_OK
    assert "#540: would keep the spec open — tickets still open: #541, #542" in out
    assert removed(fake) == [] and not posted(fake)
    assert not fake.ran("gh", "issue", "edit") and not fake.ran("gh", "issue", "close")


def test_an_unreadable_ticket_list_never_closes_the_spec(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = reopened_540(tmp_path)
    fake.answer(*ISSUE_LIST, stdout="not json")
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert "#540: spec NOT closed" in err
    assert not fake.ran("gh", "issue", "close")


def test_a_relaunched_spec_closes_on_its_new_pull_request(
    tmp_path: Path, transcripts: Path
) -> None:
    # #540 relaunched on agent/540-fini-r2 (#1567): its own PR #690 merged,
    # no ticket left — the spec closes, naming the new PR, not the old one.
    fake = clean_world(tmp_path, slug_540="fini-r2")
    fake.answer(*pr_list("agent/540-fini-r2"), stdout=json.dumps([{"number": 690}]))
    answer_checks(fake, 690, GREEN)
    fake.answer(
        "gh", "issue", "view", "540", "--repo", "owner/repo", "--json", "state",
        stdout='{"state": "OPEN"}',
    )  # fmt: skip
    fake.answer("gh", "issue", "close", "540", prefix=True)
    code, _, out, _ = cleaned(tmp_path, fake)

    assert code == EXIT_OK
    [close] = fake.ran("gh", "issue", "close", "540")
    assert (
        close.argv[-1] == "Livré par #690 (fusionnée) — fermé par `tracerworks clean`."
    )
    assert "#540: spec closed (delivered by #690)" in out


# --- a delivered spec closes without its worktree (#2077) -----------------------

PULLS_OF_SPECS = (
    "gh", "pr", "list", "--repo", "owner/repo", "--state", "all", "--limit",
    "200", "--json", "number,headRefName,state,mergedAt",
)  # fmt: skip


def answer_spec_pulls(fake: FakeRunner, pulls: Sequence[tuple[int, str, str]]) -> None:
    """(number, branch, state) — the pull requests `clean` maps to specs."""
    fake.answer(
        *PULLS_OF_SPECS,
        stdout=json.dumps(
            [
                {"number": n, "headRefName": branch, "state": state,
                 "mergedAt": "2026-10-29T16:14:00Z" if state == "MERGED" else None}
                for n, branch, state in pulls
            ]
        ),
    )  # fmt: skip


def vanished_560(tmp_path: Path, transcripts: Path) -> FakeRunner:
    """The 29/10 scenario (#2047): #560's PR #655 merged and green, its
    tickets closed, its worktree and local branch gone outside tracerworks —
    still `to-launch`, so the queue showed it « queued » again. Its Claude
    logs stay, filed under the worktree's path."""
    _worktree_logs(transcripts / "projects", tmp_path / "wt" / "560")
    fake = clean_world(tmp_path)
    # #561, its last ticket, closed: only the spec is left open
    answer_open_issues(fake, [(560, "Une spec", "to-launch")])
    answer_spec_pulls(
        fake,
        [(655, "agent/560-livree", "MERGED"), (640, "agent/540-fini", "MERGED")],
    )
    answer_checks(fake, 655, GREEN)
    fake.answer(
        "gh", "issue", "view", "560", "--repo", "owner/repo", "--json", "state",
        stdout='{"state": "OPEN"}',
    )  # fmt: skip
    fake.answer("gh", "issue", "close", "560", prefix=True)
    fake.answer("gh", "issue", "comment", "560", prefix=True)
    answer_reopened(fake, 560, [])
    remembered_comments(fake, 560)
    return fake


def answer_reopened(fake: FakeRunner, spec: int, stamps: Sequence[str]) -> None:
    fake.answer(
        "gh", "api", "--paginate", f"repos/owner/repo/issues/{spec}/events",
        "--jq", '.[] | select(.event == "reopened") | .created_at',
        stdout="".join(f"{s}\n" for s in stamps),
    )  # fmt: skip


def usage_on(fake: FakeRunner, spec: int) -> list[str]:
    return [
        c.stdin.decode()
        for c in fake.ran("gh", "issue", "comment", str(spec))
        if c.stdin and USAGE_MARKER.encode() in c.stdin
    ]


def test_a_delivered_spec_closes_without_its_worktree_its_usage_posted_once(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    first = cleaned(tmp_path, fake)

    assert first[0] == EXIT_OK, first[3]
    [close] = fake.ran("gh", "issue", "close", "560")
    assert (
        close.argv[-1] == "Livré par #655 (fusionnée) — fermé par `tracerworks clean`."
    )
    assert (
        "#560: spec closed (delivered by #655), its worktree already gone" in (first[2])
    )
    [usage] = usage_on(fake, 560)
    assert "Consommation du chantier #560" in usage
    assert "Worktree : déjà absent" in usage
    with closing(store.connect(first[1].settings.history_db)) as db:
        rows = db.execute("SELECT spec, source FROM chantier").fetchall()
    assert (560, "clean") in rows
    # the close comes after the usage: a failed post never closes in silence
    post = fake.ran("gh", "issue", "comment", "560")[0]
    assert fake.calls.index(post) < fake.calls.index(close)

    # closed by now: GitHub's open list no longer has it — nothing more
    answer_open_issues(fake, [])
    second = cleaned(tmp_path, fake)
    assert second[0] == EXIT_OK
    assert len(usage_on(fake, 560)) == 1
    assert len(fake.ran("gh", "issue", "close", "560")) == 1


def test_a_second_pass_on_a_spec_still_open_posts_nothing_more(
    tmp_path: Path, transcripts: Path
) -> None:
    """The close failed after the post: the next pass closes, posts nothing."""
    fake = vanished_560(tmp_path, transcripts)
    fake.on("gh", "issue", "close", "560", prefix=True)(Result(1, "", "HTTP 502"))
    first = cleaned(tmp_path, fake)
    assert first[0] == EXIT_BROKEN
    assert "#560: spec NOT closed" in first[3]

    fake.answer("gh", "issue", "close", "560", prefix=True)
    second = cleaned(tmp_path, fake)
    assert second[0] == EXIT_OK
    assert len(usage_on(fake, 560)) == 1
    assert "#560: spec closed (delivered by #655)" in second[2]


def test_a_spec_without_worktree_with_a_ticket_open_stays_open(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    answer_open_issues(
        fake, [(560, "Une spec", "to-launch"), (561, "Parent : #560", "")]
    )
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_OK, err
    assert not fake.ran("gh", "issue", "close", "560")
    assert not fake.ran("gh", "issue", "comment", "560")


@pytest.mark.parametrize(
    "pulls",
    [
        [],  # never delivered: a spec waiting for its first worker
        [(655, "agent/560-livree", "CLOSED")],  # closed unmerged
        # a new tranche in review: not delivered yet
        [(656, "agent/560-livree-r2", "OPEN"), (655, "agent/560-livree", "MERGED")],
        [(655, "agent/5600-autre", "MERGED")],  # another spec's branch
    ],
)
def test_a_spec_without_a_merged_pull_request_is_left_alone(
    tmp_path: Path, transcripts: Path, pulls: list[tuple[int, str, str]]
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    answer_spec_pulls(fake, pulls)
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_OK, err
    assert not fake.ran("gh", "issue", "close", "560")
    assert not fake.ran("gh", "issue", "comment", "560")


def test_a_spec_without_worktree_whose_pull_went_red_after_merge_stays_open(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    answer_checks(fake, 655, RED)
    cleaned(tmp_path, fake)
    cleaned(tmp_path, fake)

    assert not fake.ran("gh", "issue", "close", "560")
    reds = [
        c for c in fake.ran("gh", "issue", "comment", "560")
        if c.stdin and b"tracerworks:red-after-merge #655" in c.stdin
    ]  # fmt: skip
    assert len(reds) == 1


def test_a_spec_reopened_after_its_merge_is_left_to_the_operator(
    tmp_path: Path, transcripts: Path
) -> None:
    """Reopened to add a ticket, `to-launch` still on: not closed under him;
    a reopening older than the merge counts for nothing."""
    fake = vanished_560(tmp_path, transcripts)
    answer_reopened(fake, 560, ["2026-10-20T08:00:00Z", "2026-10-29T17:00:00Z"])
    cleaned(tmp_path, fake)
    assert not fake.ran("gh", "issue", "close", "560")

    answer_reopened(fake, 560, ["2026-10-20T08:00:00Z"])
    cleaned(tmp_path, fake)
    assert len(fake.ran("gh", "issue", "close", "560")) == 1


def test_an_unreadable_reopening_never_closes_the_spec(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    answer_reopened(fake, 560, ["not a date"])
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_BROKEN
    assert "#560: spec NOT closed" in err
    assert not fake.ran("gh", "issue", "close", "560")


def test_an_unreadable_worktree_list_never_closes_a_spec_as_gone(
    tmp_path: Path, transcripts: Path
) -> None:
    """git could not list the worktrees: #560's may well be there."""
    fake = vanished_560(tmp_path, transcripts)
    fake.answer(
        "git", "-C", str(REPO), "worktree", "list", "--porcelain", returncode=128
    )
    cleaned(tmp_path, fake)

    assert not fake.ran("gh", "issue", "close", "560")
    assert not usage_on(fake, 560)


def test_only_a_trusted_launched_spec_is_closed_without_worktree(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    answer_open_issues(fake, [(560, "Une spec", "to-launch", "stranger")])
    code, _, _, err = cleaned(tmp_path, fake)

    assert code == EXIT_OK, err
    assert not fake.ran("gh", "issue", "close", "560")


def test_dry_run_says_it_would_close_the_spec_without_worktree(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = vanished_560(tmp_path, transcripts)
    code, _, out, _ = cleaned(tmp_path, fake, "--dry-run")

    assert code == EXIT_OK
    assert "#560: would close the spec (delivered by #655), its worktree" in out
    assert not fake.ran("gh", "issue", "close") and not posted(fake)


# --- a worktree that vanishes outside `clean` is said (#2077) -------------------


def listing_without(fake: FakeRunner, tmp_path: Path, *specs: int) -> None:
    """The worktree list of `clean_world`, those specs gone from it."""
    listed = fake(["git", "-C", str(REPO), "worktree", "list", "--porcelain"]).stdout
    blocks = listed.strip().split("\n\n")
    keep = [
        b for b in blocks
        if not any(f"worktree {tmp_path / 'wt' / str(n)}\n" in b + "\n" for n in specs)
    ]  # fmt: skip
    fake.answer(
        "git", "-C", str(REPO), "worktree", "list", "--porcelain",
        stdout="\n\n".join(keep) + "\n",
    )  # fmt: skip


def test_a_worktree_gone_outside_clean_is_said_and_alerted(
    tmp_path: Path, transcripts: Path
) -> None:
    from tracerworks import alerts, worktree_watch

    fake = clean_world(tmp_path)
    first = cleaned(tmp_path, fake)
    assert "vanished" not in first[2]
    # #520 (unpushed, kept) disappears between two passes, as #2047 did
    listing_without(fake, tmp_path, 520)
    env = first[1]
    seen = datetime.fromtimestamp(env.now(), UTC).strftime("%H:%M:%S")
    second = cleaned(tmp_path, fake)

    line = f"#520: worktree vanished, not removed by clean, last seen {seen}"
    assert line in second[2]
    assert "#540: worktree vanished" not in second[2]  # `clean` removed it
    with closing(store.connect(env.settings.history_db)) as db:
        now = env.now()
        found = worktree_watch.vanished(db, "owner/repo", now)
        alarming, known = alerts.conditions(
            db, "owner/repo", "main", "ci.yml", now,
            operator="Sam", human_label="needs-human", blocked=None,
            vanished=found,
        )  # fmt: skip
    assert [spec for spec, _ in found] == [520]
    [alert] = [c for c in alarming if c.key == "vanished owner/repo #520"]
    assert "worktree of #520 vanished" in alert.text
    assert "vanished owner/repo" in known

    third = cleaned(tmp_path, fake)  # said once: the next pass is quiet
    assert "vanished" not in third[2]


def test_a_worktree_removed_by_clean_is_never_said_vanished(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    first = cleaned(tmp_path, fake)
    assert str(tmp_path / "wt" / "540") in removed(fake)
    listing_without(fake, tmp_path, 540)
    second = cleaned(tmp_path, fake)

    assert "vanished" not in first[2] + second[2]


def test_an_unreadable_worktree_list_never_says_vanished(
    tmp_path: Path, transcripts: Path
) -> None:
    fake = clean_world(tmp_path)
    cleaned(tmp_path, fake)
    fake.answer(
        "git", "-C", str(REPO), "worktree", "list", "--porcelain", returncode=128
    )
    _, _, out, _ = cleaned(tmp_path, fake)

    assert "vanished" not in out
