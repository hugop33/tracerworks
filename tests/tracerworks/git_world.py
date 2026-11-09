"""Real git repositories for the reuse of a light lane (#1844): GitHub's side
(a bare `origin`, with `refs/pull/<n>/head` as GitHub keeps them), a working
copy where the test writes commits, and tracerworks's main clone, which only
fetches."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tracerworks.process import run_process
from tracerworks.reuse import Code

PULL = 7
#: Ten lines before the code the pull requests change: the base can change
#: the top of the same file without touching their hunks' context.
TOP = "".join(f"x{i} = {i}\n" for i in range(10))
START = TOP + "def a():\n    return 1\n"


def git(where: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *argv], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


class Repos:
    def __init__(self, root: Path) -> None:
        self.origin, self.work, self.clone = (
            root / "origin",
            root / "work",
            root / "clone",
        )
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", "main", str(self.origin)],
            check=True,
        )
        subprocess.run(
            ["git", "init", "--quiet", "-b", "main", str(self.work)], check=True
        )
        for key, value in (("user.name", "t"), ("user.email", "t@t"),
                           ("commit.gpgsign", "false")):  # fmt: skip
            git(self.work, "config", key, value)
        git(self.work, "remote", "add", "origin", str(self.origin))
        self.write({"src/app.py": START, "README.md": "v1\n"})
        self.commit("start", branch="main")
        subprocess.run(
            ["git", "clone", "--quiet", str(self.origin), str(self.clone)], check=True
        )

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def commit(
        self, message: str, *, branch: str, files: dict[str, str] | None = None
    ) -> str:
        if git(self.work, "symbolic-ref", "--short", "HEAD") != branch:
            git(self.work, "checkout", "--quiet", branch)
        self.write(files or {})
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "--allow-empty", "-m", message)
        git(
            self.work,
            "push",
            "--quiet",
            "--force",
            "origin",
            f"HEAD:refs/heads/{branch}",
        )
        if branch != "main":
            git(self.work, "push", "--quiet", "--force", "origin",
                f"HEAD:refs/pull/{PULL}/head")  # fmt: skip
        return git(self.work, "rev-parse", "HEAD")

    def branch(self, name: str, *, at: str = "main") -> None:
        git(self.work, "checkout", "--quiet", "-b", name, at)

    def merge_main(self, branch: str) -> str:
        """The worker merges the base into its branch and pushes."""
        git(self.work, "checkout", "--quiet", branch)
        git(self.work, "merge", "--quiet", "--no-edit", "main")
        git(self.work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        git(self.work, "push", "--quiet", "--force", "origin",
            f"HEAD:refs/pull/{PULL}/head")  # fmt: skip
        return git(self.work, "rev-parse", "HEAD")

    def code(self, ignore: tuple[str, ...] = ()) -> Code:
        return Code(run_process, self.clone, "origin", "main", ignore)
