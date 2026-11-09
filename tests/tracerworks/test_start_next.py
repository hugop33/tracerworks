"""`start-next` reads the GitHub frontier; `self-update` installs from main (#570).

The frontier is read from issue bodies as `to-tickets` writes them in this
repository (`Parent : #565. Dépend de : #566`), plus GitHub's native
dependencies when a ticket has some.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tests.tracerworks.fakes import PACKAGE, REPO, FakeRunner, recorded_settings
from tests.tracerworks.test_start_stop import (
    calm,
    git_repo_answers,
    issue_answer,
    make_env,
    world,
)
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, run
from tracerworks.frontier import BodyRules, Issue, Queue, choose, parse_issues
from tracerworks.relauncher import RELAUNCHER_LOG_LINES
from tracerworks.self_update import self_update

LABEL = "ready-for-agent"
LAUNCH = "to-launch"
READY = [{"name": LABEL}, {"name": LAUNCH}]
QUEUE = Queue(frozenset({LABEL}), "priority")
RULES = BodyRules(["Parent"], ["Dépend de", "Depends on", "Blocked by"])
LABELED_BY_OPERATOR = "ready-for-agent\toperator-login\nto-launch\toperator-login\n"


def gh_issue(
    n: int,
    title: str,
    body: str,
    *,
    ready: bool = True,
    assigned: bool = False,
    author: str = "operator-login",
) -> dict[str, object]:
    return {
        "number": n,
        "title": title,
        "body": body,
        "labels": READY if ready else [],
        "assignees": [{"login": "someone"}] if assigned else [],
        "author": {"login": author},
    }


#: The factory's own family, as published on 26/09, plus neighbours.
FACTORY = [
    gh_issue(565, "Spec — Factory V1 : tracerworks", "## Le but\n…"),
    gh_issue(566, "Factory 1/6 — status", "Parent : #565. Dépend de : rien."),
    gh_issue(567, "Factory 2/6 — start et stop", "Parent : #565. Dépend de : #566"),
    gh_issue(568, "Factory 3/6 — message", "Parent : #565. Dépend de : #566"),
    gh_issue(570, "Factory 5/6 — start-next", "Parent : #565. Dépend de : #568, #569"),
]


def issues(*items: dict[str, object]) -> list[Issue]:
    return parse_issues(json.dumps(list(items)), RULES)


def never_blocked(_: int, __: list[int]) -> bool:
    return False


def no_worker(_: int) -> str | None:
    return None


# --- the frontier ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "parent", "blockers"),
    [
        ("Parent : #565. Dépend de : rien.", 565, []),
        ("Parent : #565. Dépend de : #568, #569", 565, [568, 569]),
        ("Parent : #525 — écran 7. Dépend de : #526", 525, [526]),
        ("## Parent\n\nChantier 15 — spec #444.\n\n## Blocked by\n\n", 444, []),
        ("Part of #12\nBlocked by: #13, #14\n", None, [13, 14]),
        ("Parent : #565. Indépendant ; rapport seulement.", 565, []),
    ],
)
def test_parent_and_blockers_are_read_from_the_body(
    body: str, parent: int | None, blockers: list[int]
) -> None:
    [issue] = issues(gh_issue(1, "t", body))
    assert (issue.parent, list(issue.blockers)) == (parent, blockers)


def test_the_head_is_the_first_ticket_whose_blockers_are_all_closed() -> None:
    candidate, _ = choose(issues(*FACTORY), QUEUE, no_worker, never_blocked)
    assert candidate is not None
    assert (candidate.spec.number, candidate.head.number) == (565, 566)


def test_a_ticket_is_free_once_its_blocker_is_closed() -> None:
    """#566 left the open list: GitHub is asked whether it is really closed."""
    without_566 = [i for i in FACTORY if i["number"] != 566]
    asked: list[tuple[int, list[int]]] = []

    def closed(ticket: int, unlisted: list[int]) -> bool:
        asked.append((ticket, unlisted))
        return False

    candidate, _ = choose(issues(*without_566), QUEUE, no_worker, closed)
    assert candidate is not None and candidate.head.number == 567
    assert asked == [(567, [566])]


def test_a_blocker_absent_from_the_list_but_not_closed_still_blocks() -> None:
    """A PR number, a typo, an issue past the list limit: not « closed » by default."""
    without_566 = [i for i in FACTORY if i["number"] != 566]
    candidate, skipped = choose(
        issues(*without_566), QUEUE, no_worker, lambda _, unlisted: 566 in unlisted
    )
    assert candidate is None
    assert skipped == ["#565: no unblocked, free ticket"]


def test_every_dependency_line_counts() -> None:
    [issue] = issues(gh_issue(1, "t", "Parent : #5.\nDépend de : #6\n\nBlocked by: #7"))
    assert issue.blockers == (6, 7)


@pytest.mark.parametrize(
    ("body", "blockers"),
    [
        # 12/10, 22/10: the list under the title, as `to-tickets` wrote it
        (
            "Parent : #5.\n\n## Blocked by\n\n- #1639\n"
            "- #1640 (Rebonds 2/7, après #1600)\n- [ ] #1641, #1642\n",
            [1639, 1640, 1641, 1642],
        ),
        (
            "## Parent\n\n#5\n\n## Dépend de\n\n* #6\n1. #7\n\n## Notes\n\n- #8\n",
            [6, 7],
        ),
        # both forms in one body
        ("Parent : #5.\nDépend de : #6\n\n### Blocked by\n\n- #7\n", [6, 7]),
        # prose under the title is not a list: only its one-line form counts
        ("## Blocked by\n\nAttend A (#1701).\n\nBlocked by: None — can start\n", []),
        ("## Blocked by\n\n- None — can start immediately\n", []),
    ],
    ids=["list", "list-other-keywords", "mixed", "prose", "none"],
)
def test_blockers_are_read_as_a_list_under_their_title(
    body: str, blockers: list[int]
) -> None:
    [issue] = issues(gh_issue(1, "t", body))
    assert list(issue.blockers) == blockers


def test_a_ticket_whose_listed_blocker_is_open_is_not_free() -> None:
    """22/10: #1634 (« Rebonds 2/7 ») looked free before its blocker #1639."""
    tickets = [
        gh_issue(1632, "Spec — Rebonds", "## Le but\n…"),
        gh_issue(
            1634, "Rebonds 2/7", "## Parent\n\n#1632\n\n## Blocked by\n\n- #1639\n"
        ),
        gh_issue(1639, "Rebonds 1/7", "## Parent\n\n#1632\n\n## Blocked by\n\nNone\n"),
    ]
    candidate, _ = choose(issues(*tickets), QUEUE, no_worker, never_blocked)
    assert candidate is not None and candidate.head.number == 1639


@pytest.mark.parametrize(
    ("body", "parent"),
    [
        # 11/10, #1190: « Parent » in a sentence is no parent
        ("## Notes\n\nParent bug: #1151, see there.\n", None),
        ("Le parent de ce bug est #1151.\n\n## Parent\n\n#1708\n", 1708),
        ("Voir le Parent : #9 plus bas.\n\n## Parent\n\nSpec #1708.\n", 1708),
        ("Parent: #1708\n", 1708),
        ("## Parent\n\n## Blocked by\n\n- #3\n", None),
    ],
    ids=["sentence-only", "sentence-then-section", "mid-line", "line", "empty-section"],
)
def test_the_parent_is_read_only_in_its_section_or_at_a_line_start(
    body: str, parent: int | None
) -> None:
    [issue] = issues(gh_issue(1, "t", body))
    assert issue.parent == parent


def test_a_claimed_ticket_is_not_on_the_frontier() -> None:
    claimed = [
        gh_issue(566, "status", "Parent : #565. Dépend de : rien.", assigned=True)
    ]
    rest = [i for i in FACTORY if i["number"] != 566]
    candidate, skipped = choose(
        issues(*rest, *claimed), QUEUE, no_worker, never_blocked
    )
    assert candidate is None
    assert skipped == ["#565: no unblocked, free ticket"]


def test_a_native_dependency_blocks_what_the_text_leaves_free() -> None:
    candidate, _ = choose(issues(*FACTORY), QUEUE, no_worker, lambda t, _: t == 566)
    assert candidate is None


def test_a_chantier_with_a_worker_is_skipped_and_the_next_one_taken() -> None:
    other = [
        gh_issue(600, "Spec — Console", "…"),
        gh_issue(601, "Console 1/2", "Parent : #600. Dépend de : rien."),
    ]
    candidate, skipped = choose(
        issues(*FACTORY, *other),
        QUEUE,
        lambda n: "already has a worker (working)" if n == 565 else None,
        never_blocked,
    )
    assert candidate is not None and candidate.spec.number == 600
    assert skipped == ["#565: already has a worker (working)"]


def test_only_ready_specs_with_tickets_are_chantiers() -> None:
    items = [
        gh_issue(700, "Spec pas prête", "…", ready=False),
        gh_issue(701, "ticket", "Parent : #700. Dépend de : rien."),
        gh_issue(702, "Spec sans tickets", "…"),
    ]
    assert choose(issues(*items), QUEUE, no_worker, never_blocked) == (None, [])


# --- the command ---------------------------------------------------------------------


def gh_lists(fake: FakeRunner, items: list[dict[str, object]]) -> None:
    fake.answer("gh", "issue", "list", prefix=True, stdout=json.dumps(items))
    fake.answer("gh", "api", prefix=True, stdout="0\n")
    # The runner query is also `gh api`: keep its own answer on top.
    runners = (
        Path(__file__).parents[1] / "fixtures" / "tracerworks" / "runners-idle.json"
    ).read_text()
    fake.answer("gh", "api", "repos/owner/repo/actions/runners", stdout=runners)
    # #1529: who put the launch label — the operator, unless a test says else
    fake.answer("gh", "api", "--paginate", prefix=True, stdout=LABELED_BY_OPERATOR)


def test_start_next_prepares_the_oldest_ready_chantier(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake = world(tmp_path)
    gh_lists(fake, FACTORY)
    issue_answer(fake, 565, "Spec — Factory V1 : tracerworks")
    git_repo_answers(fake)
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)

    code = run(env, ["start-next"])

    assert "#565 Spec — Factory V1 : tracerworks — head ticket #566" in out.getvalue()
    [add] = fake.ran("git", "-C", str(REPO), "worktree", "add")
    assert "agent/565-factory-v1-tracerworks" in add.argv
    primer = (tmp_path / "cache" / "primer-565.md").read_text()
    assert "Ticket de tête d'après GitHub : #566 — Factory 1/6 — status" in primer
    assert code == EXIT_OK, err.getvalue()
    assert [(n, p) for n, p, _ in launched] == [(565, "claude")]


def test_start_next_never_resumes_a_stopped_chantier(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake = world(tmp_path)
    gh_lists(
        fake,
        [
            gh_issue(520, "Spec — Dashboard", "…"),
            gh_issue(521, "Dashboard 1/1", "Parent : #520. Dépend de : rien."),
        ],
    )
    env, out, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "skipped — #520: stopped (worktree present)" in out.getvalue()
    assert "no spec" in err.getvalue()


def test_start_next_is_refused_by_the_guards_before_reading_the_backlog(
    tmp_path: Path,
) -> None:
    fake = world(tmp_path)
    env, _, err = make_env(fake, tmp_path)

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "no new worker" in err.getvalue()
    assert not fake.ran("gh", "issue", "list")


def test_start_next_never_goes_over_the_cap(tmp_path: Path) -> None:
    fake = world(tmp_path)
    gh_lists(fake, FACTORY)
    env, _, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    env.settings.state_file.write_text("claude:\n  weekly_used: 80\n")

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "cap reached" in err.getvalue()
    assert not fake.ran("gh", "issue", "list")


def test_start_next_starts_nothing_while_the_queue_is_paused(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#2192: the Settings tab's pause — the reason in the relauncher's
    journal, nothing read nor started; the workers running are not touched."""
    fake = world(tmp_path)
    gh_lists(fake, FACTORY)
    env, _, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    env.settings.state_file.write_text("queue:\n  paused: true\n")

    assert run(env, ["start-next"]) == EXIT_REFUSED
    assert "queue paused (settings)" in err.getvalue()
    assert not fake.ran("gh", "issue", "list")
    assert not fake.ran("tmux", "kill-window")
    assert launched == []


def test_start_next_caps_its_journal_first(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    """#2288: every pass, even one refused at once, cuts the relauncher's
    journal to its last `RELAUNCHER_LOG_LINES` lines."""
    fake = world(tmp_path)
    gh_lists(fake, FACTORY)
    env, _, _ = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    journal = tmp_path / "next.log"
    journal.write_text("".join(f"{i}\n" for i in range(RELAUNCHER_LOG_LINES + 3)))
    env.settings = replace(env.settings, relauncher_log=journal)
    env.settings.state_file.write_text("queue:\n  paused: true\n")

    assert run(env, ["start-next"]) == EXIT_REFUSED
    lines = journal.read_text().splitlines()
    assert len(lines) == RELAUNCHER_LOG_LINES and lines[0] == "3"


def test_start_of_one_chantier_still_starts_while_the_queue_is_paused(
    tmp_path: Path, launched: list[tuple[int, str, str]]
) -> None:
    fake = world(tmp_path)
    issue_answer(fake, 600, "Spec — Console opérateur")
    git_repo_answers(fake)
    env, _, err = make_env(fake, tmp_path)
    env = calm(fake, tmp_path, env)
    env.settings.state_file.write_text("queue:\n  paused: true\n")

    assert run(env, ["start", "600"]) == EXIT_OK, err.getvalue()
    assert [(n, p) for n, p, _ in launched] == [(600, "claude")]


# --- self-update, for real, from a throwaway origin ----------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_self_update_installs_the_commit_of_origin_main_not_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@example.invalid")
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True
    )
    shutil.copytree(
        PACKAGE,
        repo / "tools" / "tracerworks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (repo / "tracerworks.toml").write_text(
        "# the project file, installed with the tool\n"
    )
    for args in (
        ["add", "tools", "tracerworks.toml"],
        ["commit", "-q", "-m", "tracerworks"],
        ["push", "-q", "origin", "HEAD:main"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    # A change that is NOT on origin/main must not be installed.
    (repo / "tools" / "tracerworks" / "__init__.py").write_text(
        "raise SystemExit('worktree copy')\n"
    )
    settings = replace(recorded_settings(tmp_path), repo_dir=repo)

    from tracerworks.process import run_process

    first = self_update(run_process, settings)
    again = self_update(run_process, settings)

    assert first.previous is None and again.previous == first.current
    launcher = settings.bin_dir / "tracerworks"
    result = subprocess.run(
        [str(launcher), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "start-next" in result.stdout
    current = settings.install_dir / "current"
    assert (
        "worktree copy"
        not in (current / "tools" / "tracerworks" / "__init__.py").read_text()
    )
    # The project file comes with the tool, and the launcher points at it.
    assert (current / "tracerworks.toml").read_text().startswith("# the project file")
    assert f"TRACERWORKS_CONFIG:={current / 'tracerworks.toml'}" in launcher.read_text()


def test_self_update_command_reports_the_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tracerworks.cli as cli
    from tracerworks.self_update import Installed

    monkeypatch.setattr(
        cli, "self_update", lambda runner, settings: Installed("a" * 40, "b" * 40)
    )
    fake = FakeRunner()
    fake.answer("systemctl", "--user", prefix=True)
    env, out, _ = make_env(fake, tmp_path)
    assert run(env, ["self-update"]) == EXIT_OK
    assert "tracerworks aaaaaaaaa → bbbbbbbbb (origin/main)" in out.getvalue()


def test_a_ready_spec_without_the_launch_label_waits() -> None:
    """A spec written in advance is not a request to build it (the operator, 26/09)."""
    only_ready = [
        {**i, "labels": [{"name": LABEL}]} if i["number"] == 565 else i for i in FACTORY
    ]
    queue = Queue(frozenset({LABEL, LAUNCH}), "priority")
    candidate, _ = choose(issues(*only_ready), queue, no_worker, never_blocked)
    assert candidate is None


def test_a_priority_spec_goes_before_an_older_one() -> None:
    older = [
        gh_issue(600, "Spec — ancienne", "…"),
        gh_issue(601, "ancienne 1/1", "Parent : #600. Dépend de : rien."),
    ]
    urgent = [
        {
            **gh_issue(700, "Spec — urgente", "…"),
            "labels": [*READY, {"name": "priority"}],
        },
        gh_issue(701, "urgente 1/1", "Parent : #700. Dépend de : rien."),
    ]
    queue = Queue(frozenset({LABEL, LAUNCH}), "priority")
    candidate, _ = choose(issues(*older, *urgent), queue, no_worker, never_blocked)
    assert candidate is not None and candidate.spec.number == 700


def test_a_starred_spec_never_goes_before_an_older_one() -> None:
    """The star is the operator's pick, not an order (#1875): priority alone is."""
    older = [
        gh_issue(600, "Spec — ancienne", "…"),
        gh_issue(601, "ancienne 1/1", "Parent : #600. Dépend de : rien."),
    ]
    starred = [
        {
            **gh_issue(700, "Spec — étoilée", "…"),
            "labels": [*READY, {"name": "starred"}],
        },
        gh_issue(701, "étoilée 1/1", "Parent : #700. Dépend de : rien."),
    ]
    queue = Queue(frozenset({LABEL, LAUNCH}), "priority")
    candidate, _ = choose(issues(*starred, *older), queue, no_worker, never_blocked)
    assert candidate is not None and candidate.spec.number == 600
