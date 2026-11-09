"""A recorded world for `tracerworks`: each `gh`/`git`/`tmux` call answered by fixtures.

`FakeRunner` matches a call by its argument list (exactly, or by prefix) and
answers what was recorded; an unexpected call fails the test loudly instead of
returning an empty success — a fake that says « ok » to everything is how a
guard gets tested without teeth. Every call is kept, with its stdin, so a test
can assert what was SENT (the message bytes) and what was never run.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import tracerworks
from tracerworks.level import DEFAULT, Choice, Level
from tracerworks.process import Result
from tracerworks.runtime.tmux import WINDOW_FORMAT
from tracerworks.settings import Settings, parse

Answer = Result | Callable[[Sequence[str], bytes | None], Result]

#: The test project's default level (`[workers] model`, `effort`), for a
#: runtime's spawn or resume called directly (#2251).
LEVEL = Level(Choice("big", DEFAULT), Choice("high", DEFAULT))


@dataclass
class Call:
    argv: tuple[str, ...]
    cwd: Path | None
    stdin: bytes | None


@dataclass
class FakeRunner:
    rules: list[tuple[tuple[str, ...], bool, Answer]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The world's default (#1529): the launch label was put by the
        # operator — a test of another actor answers its own events.
        self.answer(
            "gh", "api", "--paginate", prefix=True,
            stdout="to-launch\toperator-login\n",
        )  # fmt: skip
        # The merge queue (#1714, #1801): every open pull request — none,
        # unless a test fills it.
        self.answer(
            "gh", "pr", "list", "--repo", "owner/repo", "--state", "open",
            "--limit", prefix=True, stdout="[]",
        )  # fmt: skip
        self.answer(
            "gh", "pr", "list", "--repo", "owner/repo", "--head",
            prefix=True, stdout="[]",
        )  # fmt: skip
        # A spec's labels, read at each start for its level (#2251): none,
        # unless a test answers its own.
        self.on("gh", "issue", "view", prefix=True)(self._no_labels)
        # A worker at rest has its field read with its colours (#2003): a pane
        # recorded without any reads the same with `-e`, unless a test says.
        self.on("tmux", "capture-pane", "-p", "-e", prefix=True)(
            lambda argv, stdin: self([a for a in argv if a != "-e"], stdin=stdin)
        )

    @staticmethod
    def _no_labels(argv: Sequence[str], stdin: bytes | None) -> Result:
        if tuple(argv[-2:]) != ("--json", "labels"):
            raise AssertionError(f"unexpected command: {tuple(argv)}")
        return Result(0, '{"labels": []}', "")

    def on(self, *argv: str, prefix: bool = False) -> Callable[[Answer], None]:
        def register(answer: Answer) -> None:
            # Most recent rule first: a test can override a default answer.
            self.rules.insert(0, (tuple(argv), prefix, answer))

        return register

    def answer(
        self, *argv: str, stdout: str = "", returncode: int = 0, prefix: bool = False
    ) -> None:
        self.on(*argv, prefix=prefix)(
            Result(returncode, stdout, "" if returncode == 0 else stdout)
        )

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
    ) -> Result:
        call = tuple(argv)
        self.calls.append(Call(call, cwd, stdin))
        for pattern, prefix, answer in self.rules:
            if call == pattern or (prefix and call[: len(pattern)] == pattern):
                return answer(call, stdin) if callable(answer) else answer
        raise AssertionError(f"unexpected command: {call}")

    def ran(self, *argv: str) -> list[Call]:
        return [c for c in self.calls if c.argv[: len(argv)] == tuple(argv)]


# gh ≥ 2.101 (the VPS's, 28/10): a response with terminal escape sequences is
# refused, exit 1, unless the read passes `--allow-escape-sequences`.
GH_REFUSES_ESCAPES = (
    "the response contains terminal escape sequences; pass"
    " --allow-escape-sequences to output it anyway"
)


def answer_job_log(
    fake: FakeRunner, repo: str, job: int, *, stdout: str = "", returncode: int = 0
) -> None:
    """A job's log as `gh` 2.101 gives it: coloured, only to the flagged read."""
    url = f"repos/{repo}/actions/jobs/{job}/logs"
    fake.answer(
        "gh", "api", "--allow-escape-sequences", url,
        stdout=stdout, returncode=returncode,
    )  # fmt: skip
    if "\x1b" in stdout:
        fake.answer("gh", "api", url, returncode=1, stdout=GH_REFUSES_ESCAPES)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "tracerworks"
#: The package's own folder (its page, its sources), installed or not.
PACKAGE = Path(tracerworks.__file__).parent
REPO = Path("/home/dev/dev/widgets")
ROOT = Path("/home/dev/.agent-worktrees")
TMUX = ("tmux",)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def target(window: str) -> str:
    return f"=widgets:={window}"


def private_tmux_socket(tmp_path: Path) -> str:
    """A tmux server of the test's own, for THIS run only.

    The test's name alone is not enough: two CI runners share the VPS's
    account, hence `/tmp/tmux-<uid>/`, and on 19/10 two unit runs at once met
    on `tracerworks-test-test_scenario_f_stop_refused_u0` (« duplicate session »);
    the first one's teardown would have killed the other's server too. The
    pytest process id tells two simultaneous runs, and their xdist workers,
    apart.
    """
    return f"tracerworks-test-{os.getpid()}-{tmp_path.name}"


#: `private_tmux_socket`'s names: the pytest process id, then the test.
TEST_TMUX = re.compile(r"tracerworks-test-([0-9]+)-.+")


def tmux_dir() -> Path:
    """Where tmux keeps its sockets, one per server."""
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return Path(base) / f"tmux-{os.getuid()}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's process: it exists
        return True
    return True


def reap_stale_tmux() -> None:
    """Stop the test tmux servers a killed pytest left (#1724), then drop
    their socket: those whose pytest process is dead."""
    try:
        names = sorted(entry.name for entry in tmux_dir().iterdir())
    except OSError:
        return
    for name in names:
        owner = TEST_TMUX.fullmatch(name)
        if owner and not _alive(int(owner.group(1))):
            subprocess.run(
                ["tmux", "-L", name, "kill-server"], capture_output=True, check=False
            )
            (tmux_dir() / name).unlink(missing_ok=True)


def recorded_settings(tmp_path: Path) -> Settings:
    """The test project file, with every path it writes moved under tmp_path."""
    project = FIXTURES / "project.toml"
    settings = parse(tomllib.loads(project.read_text(encoding="utf-8")), project, {})
    return replace(
        settings,
        state_file=tmp_path / "state.yaml",
        cache_dir=tmp_path / "cache",
        install_dir=tmp_path / "install",
        bin_dir=tmp_path / "bin",
        meminfo=FIXTURES / "meminfo.txt",
        proc=tmp_path / "proc",  # absent: no process, never the VPS's (#2096)
        systemd_dir=tmp_path / "systemd",
        history_db=tmp_path / "history" / "factory.sqlite3",
        backup_dir=tmp_path / "history" / "backups",
    )


def with_root(settings: Settings, root: Path) -> Settings:
    return replace(
        settings, conventions=replace(settings.conventions, worktree_root=root)
    )


def recorded_world() -> FakeRunner:
    """Four workers, one per state, as the VPS would show them.

    - #501 working: window, Claude busy, worktree fully pushed;
    - #510 waiting: window, Claude at rest, 2 commits and 2 paths unsaved;
    - #520 stopped: worktree only, branch never pushed, 3 commits ahead of main;
    - #530 dead: window whose Claude exited back to bash, no worktree.

    Disk (`du -sk`): #501 0,9 Go, #510 312 Mo, #520 unreadable (any other path too).

    Around them, what the conventions must ignore: the operator's windows (`claude`,
    `factory`) and a worktree outside the root (`.claude/worktrees/…`).
    """
    fake = FakeRunner()
    fake.answer(
        *TMUX,
        "list-windows",
        "-t",
        "=widgets",
        "-F",
        WINDOW_FORMAT,
        stdout=fixture("list-windows.txt"),
    )
    fake.answer(
        *TMUX,
        "capture-pane",
        "-p",
        "-t",
        target("worker-501"),
        stdout=fixture("pane-claude-working.txt"),
    )
    fake.answer(
        *TMUX,
        "capture-pane",
        "-p",
        "-t",
        target("worker-510"),
        stdout=fixture("pane-claude-idle.txt"),
    )
    fake.answer(
        *TMUX,
        "capture-pane",
        "-p",
        "-t",
        target("worker-530"),
        stdout=fixture("pane-dead.txt"),
    )
    fake.answer(
        "git",
        "-C",
        str(REPO),
        "worktree",
        "list",
        "--porcelain",
        stdout=fixture("worktree-list.txt"),
    )
    for n in (501, 510, 520):
        fake.answer(
            "git",
            "-C",
            str(ROOT / str(n)),
            "status",
            "--porcelain=v2",
            "--branch",
            stdout=fixture(f"status-{n}.txt"),
        )
    fake.answer(
        "git",
        "-C",
        str(ROOT / "520"),
        "rev-list",
        "--count",
        "origin/main..HEAD",
        stdout="3\n",
    )
    fake.answer(
        "git",
        "-C",
        str(ROOT / "520"),
        "log",
        "-1",
        "--format=%ct",
        stdout="1790400000\n",
    )
    fake.answer(
        "gh",
        "api",
        "repos/owner/repo/actions/runners",
        stdout=fixture("runners.json"),
    )
    for spec in (501, 510, 520, 530):  # no ticket commit pushed (#1729)
        fake.answer(
            "git", "-C", str(ROOT / str(spec)), "log", "--format=%s", prefix=True
        )
    fake.answer("du", "-sk", prefix=True, returncode=1, stdout="du: denied")
    fake.answer("du", "-sk", str(ROOT / "501"), stdout=f"943718\t{ROOT / '501'}\n")
    fake.answer("du", "-sk", str(ROOT / "510"), stdout=f"320000\t{ROOT / '510'}\n")
    return fake
