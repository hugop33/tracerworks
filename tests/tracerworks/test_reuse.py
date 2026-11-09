"""Reusing a light lane's green for the same code diff (#1844, spec #1843),
on real git repositories: each rule, and its counter-proof."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tracerworks.git_world import PULL, START, Repos
from tracerworks.reuse import Earlier, judge

APP = START + "\n\ndef b():\n    return 2\n"


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path)


def green(sha: str, conclusion: str = "success", *, finished: bool = True) -> Earlier:
    return Earlier(
        sha, finished, finished and conclusion == "success", conclusion,
        f"https://github.com/owner/repo/actions/runs/{sha[:4]}",
    )  # fmt: skip


def opened(repos: Repos) -> str:
    """A pull request changing code (`src/app.py`) and a doc."""
    repos.branch("feature")
    return repos.commit(
        "feature", branch="feature", files={"src/app.py": APP, "docs/a.md": "a\n"}
    )


def test_a_merge_of_the_base_that_moved_only_docs_reuses_the_green(
    repos: Repos,
) -> None:
    before = opened(repos)
    repos.commit("registre", branch="main", files={"docs/registre/x.md": "x\n"})
    head = repos.merge_main("feature")
    verdict = judge(repos.code(), PULL, head, [green(before)])
    assert verdict.reused_from == before, verdict.why
    assert verdict.url.endswith(before[:4])


def test_a_doc_changed_by_the_pull_request_itself_reuses_the_green(
    repos: Repos,
) -> None:
    """The conflict resolved on a doc: its content is not code."""
    before = opened(repos)
    head = repos.commit(
        "doc", branch="feature", files={"docs/a.md": "b\n", "N.md": "n"}
    )
    assert judge(repos.code(), PULL, head, [green(before)]).reused_from == before


def test_a_code_change_is_never_reused(repos: Repos) -> None:
    before = opened(repos)
    head = repos.commit(
        "code", branch="feature", files={"src/app.py": APP + "\n\ndef c():\n    pass\n"}
    )
    verdict = judge(repos.code(), PULL, head, [green(before)])
    assert (verdict.reused_from, verdict.decided) == (None, True)
    assert verdict.why == "no earlier head with the same code diff"


def test_a_whitespace_change_is_a_code_change(repos: Repos) -> None:
    """An indent is code in Python: the patch id keeps whitespace."""
    before = opened(repos)
    # the pull request's own line only, re-indented
    indented = START + "\n\ndef b():\n  return 2\n"
    head = repos.commit("indent", branch="feature", files={"src/app.py": indented})
    assert judge(repos.code(), PULL, head, [green(before)]).reused_from is None


def test_a_code_file_changed_by_the_base_and_the_pull_request_is_never_reused(
    repos: Repos,
) -> None:
    before = opened(repos)
    # the base changes the same file, far from the pull request's lines: the
    # merge is clean, the diff of the pull request the same
    repos.commit("main", branch="main", files={"src/app.py": "# top\n" + START})
    head = repos.merge_main("feature")
    verdict = judge(repos.code(), PULL, head, [green(before)])
    assert verdict.reused_from is None
    assert verdict.why == "code changed by the base and the pull request: src/app.py"


def test_code_changed_by_the_base_elsewhere_still_reuses(repos: Repos) -> None:
    """The counter-proof: the base's code, in a file the pull request leaves."""
    before = opened(repos)
    repos.commit("main", branch="main", files={"src/other.py": "x = 1\n"})
    head = repos.merge_main("feature")
    assert judge(repos.code(), PULL, head, [green(before)]).reused_from == before


@pytest.mark.parametrize("conclusion", ["failure", "timed_out"])
def test_an_earlier_light_lane_red_is_never_reused(
    repos: Repos, conclusion: str
) -> None:
    before = opened(repos)
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    verdict = judge(repos.code(), PULL, head, [green(before, conclusion)])
    assert (verdict.reused_from, verdict.decided) == (None, True)
    assert verdict.why == f"the light lane of {before[:8]} ended {conclusion}"


def test_an_earlier_light_lane_cancelled_is_never_reused(repos: Repos) -> None:
    before = opened(repos)
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    verdict = judge(repos.code(), PULL, head, [green(before, "cancelled")])
    assert (verdict.reused_from, verdict.decided) == (None, True)


def test_a_light_lane_cancelled_by_a_newer_push_passes_to_the_green_before_it(
    repos: Repos,
) -> None:
    """Two quick pushes: the middle one's light lane cancelled says nothing
    of the code; the green before it, same diff, still counts."""
    first = opened(repos)
    middle = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "c\n"})
    earlier = [green(middle, "cancelled"), green(first)]
    assert judge(repos.code(), PULL, head, earlier).reused_from == first


def test_two_binary_changes_are_two_code_diffs(repos: Repos) -> None:
    """Their contents differ, whatever git prints for them (`--binary`)."""
    repos.branch("feature")
    before = repos.commit(
        "bin", branch="feature", files={"tests/fixtures/a.bin": "\0one\0"}
    )
    head = repos.commit(
        "bin", branch="feature", files={"tests/fixtures/a.bin": "\0two\0"}
    )
    assert judge(repos.code(), PULL, head, [green(before)]).reused_from is None


def test_an_earlier_light_lane_still_running_is_asked_again(repos: Repos) -> None:
    before = opened(repos)
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    verdict = judge(repos.code(), PULL, head, [green(before, "", finished=False)])
    assert (verdict.reused_from, verdict.decided) == (None, False)


def test_the_newest_earlier_head_with_the_same_diff_decides(repos: Repos) -> None:
    """A red on the same code after a green: that red is the code's."""
    first = opened(repos)
    second = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "c\n"})
    earlier = [green(second, "failure"), green(first)]
    assert judge(repos.code(), PULL, head, earlier).reused_from is None


def test_an_earlier_head_with_another_diff_is_passed_over(repos: Repos) -> None:
    first = opened(repos)
    second = repos.commit("code", branch="feature", files={"src/app.py": "x = 2\n"})
    back = repos.commit("revert", branch="feature", files={"src/app.py": APP})
    head = repos.commit("doc", branch="feature", files={"docs/a.md": "b\n"})
    earlier = [green(back), green(second, "failure"), green(first)]
    assert judge(repos.code(), PULL, head, earlier).reused_from == back


def test_reuse_ignore_leaves_its_files_out_of_the_code(repos: Repos) -> None:
    before = opened(repos)
    head = repos.commit("notes", branch="feature", files={"tools/notes/a.txt": "a\n"})
    assert judge(repos.code(), PULL, head, [green(before)]).reused_from is None
    ignoring = repos.code(("tools/notes/**",))
    assert judge(ignoring, PULL, head, [green(before)]).reused_from == before


def test_no_earlier_head_decides_no(repos: Repos) -> None:
    head = opened(repos)
    verdict = judge(repos.code(), PULL, head, [])
    assert (verdict.reused_from, verdict.decided) == (None, True)


def test_an_earlier_head_git_cannot_have_is_passed_over(repos: Repos) -> None:
    head = opened(repos)
    gone = "0" * 40
    verdict = judge(repos.code(), PULL, head, [green(gone)])
    assert (verdict.reused_from, verdict.decided) == (None, True)
