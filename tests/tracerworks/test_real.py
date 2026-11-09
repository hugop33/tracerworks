"""`tracerworks` against a REAL tmux and git — on a private server, never `widgets`.

The proof of spec #565 that does not need an AI: throwaway repositories (a
bare « origin » and its clone), a tmux server of its own (`-L`), and a stub
standing in for the provider's interface (`fixtures/tracerworks/stub/claude`: a
thread, a prompt line, `/exit`). Launching a real provider is not part of it.

It walks scenario F of the architecture: `stop` refused while a commit is not
pushed (and the `wip:` request actually received by the worker), `stop`
accepted once pushed, the worktree kept, and `status` right after the tmux
server is killed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from tests.tracerworks.fakes import (
    LEVEL,
    private_tmux_socket,
    recorded_settings,
    with_root,
)
from tracerworks import messaging
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, Env, run
from tracerworks.process import CommandError, run_process
from tracerworks.settings import Settings

STUB = Path(__file__).parents[1] / "fixtures" / "tracerworks" / "stub"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("git") is None,
    reason="needs tmux and git",
)


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setattr(messaging, "SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(messaging, "VERIFY_SECONDS", 1.0)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True
    )
    (repo / "README").write_text("sandbox\n")
    git("add", "README", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("push", "-q", "origin", "HEAD:main", cwd=repo)
    git("fetch", "-q", "origin", cwd=repo)
    socket = private_tmux_socket(tmp_path)
    settings = replace(
        with_root(recorded_settings(tmp_path), tmp_path / "wt"),
        repo_dir=repo,
        tmux_socket=socket,
        meminfo=Path("/proc/meminfo"),
    )
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "widgets"], check=True
    )
    try:
        yield settings
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"], check=False, capture_output=True
        )


def tracerworks(settings: Settings, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = Env(settings, runner=run_process, stdin=io.BytesIO(), out=out, err=err)
    return run(env, list(argv)), out.getvalue(), err.getvalue()


def launch_stub(settings: Settings, spec: int, log: Path) -> Path:
    """The conventional worktree and window, with the stub as « provider »."""
    worktree = settings.worktree_root / str(spec)
    git(
        "worktree",
        "add",
        "-q",
        "--no-track",
        "-b",
        f"agent/{spec}-demo",
        str(worktree),
        "origin/main",
        cwd=settings.repo_dir,
    )
    assert settings.tmux_socket is not None
    subprocess.run(
        [
            "tmux",
            "-L",
            settings.tmux_socket,
            "new-window",
            "-d",
            "-t",
            "=widgets:",
            "-n",
            f"worker-{spec}",
            "-c",
            str(worktree),
            "-e",
            f"STUB_LOG={log}",
            # tmux reads the process name from argv[0]: `exec -a` makes the
            # stub show as `claude`, as the real interface does.
            "bash",
            "-c",
            'exec -a claude /bin/bash "$0"',
            str(STUB / "claude"),
        ],
        check=True,
    )
    subprocess.run(["sleep", "0.5"], check=True)
    return worktree


def test_scenario_f_stop_refused_until_pushed_then_status_after_tmux_restart(
    sandbox: Settings, tmp_path: Path
) -> None:
    log = tmp_path / "stub.log"
    worktree = launch_stub(sandbox, 7, log)

    code, out, _ = tracerworks(sandbox, "status", "--json")
    assert code == EXIT_OK
    [worker] = json.loads(out)["workers"]
    assert (worker["spec"], worker["state"], worker["provider"]) == (
        7,
        "waiting",
        "claude",
    )

    # A commit that exists only in the worktree.
    (worktree / "work.txt").write_text("ticket 1\n")
    git("add", "work.txt", cwd=worktree)
    git("commit", "-q", "-m", "feat: ticket 1", cwd=worktree)

    code, _, err = tracerworks(sandbox, "stop", "7")
    assert code == EXIT_REFUSED, err
    assert "1 unpushed commit(s) (branch never pushed)" in err
    assert "wip: commit asked of the worker" in err
    assert "wip:" in log.read_text(encoding="utf-8")  # the worker really got it

    git("push", "-q", "-u", "origin", "HEAD", cwd=worktree)
    code, out, err = tracerworks(sandbox, "stop", "7")
    assert code == EXIT_OK, err
    assert f"worktree kept: {worktree}" in out
    assert (worktree / "work.txt").exists()

    code, out, _ = tracerworks(sandbox, "status", "--json")
    [worker] = json.loads(out)["workers"]
    assert (worker["state"], worker["unpushed_commits"]) == ("stopped", 0)


def test_a_message_reaches_the_stub_byte_for_byte(
    sandbox: Settings, tmp_path: Path
) -> None:
    log = tmp_path / "stub.log"
    launch_stub(sandbox, 8, log)
    pwned = tmp_path / "pwned"
    text = (
        f"Ne lance pas $(touch {pwned}) ni `id` ;"
        " l'apostrophe reste, $HOME aussi. é → ✓"
    )
    out, err = io.StringIO(), io.StringIO()
    env = Env(
        sandbox, runner=run_process, stdin=io.BytesIO(text.encode()), out=out, err=err
    )

    assert run(env, ["message", "8"]) == EXIT_OK, err.getvalue()

    received = log.read_text(encoding="utf-8").splitlines()
    assert received[0].startswith("Message de Sam (") and received[1] == text
    assert not pwned.exists()


def test_a_worker_waiting_for_memory_is_told_to_go_on_when_it_is_back(
    sandbox: Settings, tmp_path: Path
) -> None:
    """#1730, the night of 18/10 replayed on the machine: a worker ended its
    turn on the memory marker; short of memory, nothing; once it is back,
    the real message reaches its window."""
    from tracerworks import cli
    from tracerworks.hook_state import MEMORY_MARKER, hook_file
    from tracerworks.observe import observe_workers

    log = tmp_path / "stub.log"
    launch_stub(sandbox, 11, log)
    stop = {"event": "Stop", "text": f"Il manque de la mémoire.\n{MEMORY_MARKER}"}
    path = hook_file(sandbox.cache_dir, 11)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**stop, "at": 1.0}) + "\n", encoding="utf-8")
    meminfo = tmp_path / "meminfo"

    def env_with(available_mb: int) -> tuple[Env, io.StringIO]:
        meminfo.write_text(f"MemAvailable: {available_mb * 1024} kB\n")
        out = io.StringIO()
        settings = replace(sandbox, meminfo=meminfo)
        return Env(settings, runner=run_process, out=out, err=io.StringIO()), out

    env, _ = env_with(900)
    workers = observe_workers(env.runner, env.settings, env.tmux)
    [worker] = [w for w in workers if w.spec == 11]
    assert worker.waits_for_memory
    assert cli._memory_back(env, workers) is None
    assert not log.exists() or "mémoire" not in log.read_text(encoding="utf-8")

    # the stub as a Claude Code with hooks: what reaches it is submitted (#1731)
    import threading
    import time

    def hook_of_the_stub() -> None:
        for _ in range(200):
            seen = log.read_text(encoding="utf-8") if log.exists() else ""
            if "mémoire est revenue" in seen:
                line = {"event": "UserPromptSubmit", "text": seen, "at": 2.0}
                with path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(line) + "\n")
                return
            time.sleep(0.1)

    threading.Thread(target=hook_of_the_stub, daemon=True).start()
    env, out = env_with(4600)
    assert cli._memory_back(env, workers) == 11
    assert "#11: memory back (4600 MB available)" in out.getvalue()
    assert "La mémoire est revenue : 4600 Mo" in log.read_text(encoding="utf-8")


def test_status_after_the_tmux_server_is_killed(
    sandbox: Settings, tmp_path: Path
) -> None:
    launch_stub(sandbox, 9, tmp_path / "stub.log")
    assert sandbox.tmux_socket is not None
    subprocess.run(["tmux", "-L", sandbox.tmux_socket, "kill-server"], check=True)

    code, out, err = tracerworks(sandbox, "status")

    # Its error stream said, when red (#2221): the night of 02/11 only had 3.
    assert code == EXIT_OK, err
    assert out.startswith("#9 demo — provider ? — parked (stopped) — agent/9-demo")


def socket_path(name: str) -> Path:
    """Where `tmux -L name` connects: `$TMUX_TMPDIR/tmux-<uid>/name`."""
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / name


def dead_within(path: Path, seconds: float) -> bool:
    """Has the server behind `path` stopped accepting, before the deadline?"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            return True
        finally:
            probe.close()
        time.sleep(0.05)
    return False


def test_status_while_the_tmux_server_dies_reads_the_worker_stopped(
    sandbox: Settings, tmp_path: Path
) -> None:
    """#2221, the night of 02/11: `kill-server` returns before the server is
    dead, and the next client meets a server that accepts and goes away —
    « server exited unexpectedly ». Made deterministic here: the real server
    killed and gone, a listener on its socket that accepts and closes every
    connection, as a dying server does."""
    launch_stub(sandbox, 9, tmp_path / "stub.log")
    assert sandbox.tmux_socket is not None
    path = socket_path(sandbox.tmux_socket)
    subprocess.run(["tmux", "-L", sandbox.tmux_socket, "kill-server"], check=True)
    assert dead_within(path, seconds=10), f"the killed server still answers on {path}"
    path.unlink(missing_ok=True)  # tmux leaves its socket file behind
    dying = socket.socket(socket.AF_UNIX)
    dying.bind(str(path))
    dying.listen(8)
    dying.settimeout(0.1)
    stop = threading.Event()

    def accept_and_die() -> None:
        while not stop.is_set():
            try:
                peer, _ = dying.accept()
            except TimeoutError:
                continue
            peer.close()

    thread = threading.Thread(target=accept_and_die, daemon=True)
    thread.start()
    try:
        died = subprocess.run(
            ["tmux", "-L", sandbox.tmux_socket, "list-sessions"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "server exited" in died.stderr, died.stderr  # the case, met
        code, out, err = tracerworks(sandbox, "status")
    finally:
        stop.set()
        thread.join(timeout=5)
        dying.close()
        path.unlink(missing_ok=True)

    assert code == EXIT_OK, err
    assert out.startswith("#9 demo — provider ? — parked (stopped) — agent/9-demo")


@pytest.mark.parametrize("ignored_by", ["repository", "launch", None])
def test_clean_takes_a_worker_s_drafts_with_its_worktree_only_if_git_ignores_them(
    sandbox: Settings, ignored_by: str | None
) -> None:
    """#1554: a worker's TMPDIR is its worktree's scratch folder. Both guards of
    `clean` that read the worktree — everything saved (`git status`) and
    `git worktree remove` without `--force` — must let a merged worktree go
    with its drafts: the repository ignores the folder. Without that rule,
    both refuse, and the worktree would stay forever. The launch's own rule
    (the main clone's `info/exclude`) covers a branch older than the
    repository's."""
    from tracerworks.cleanup import remove_worktree, why_kept
    from tracerworks.observe import observe_workers
    from tracerworks.runtime.tmux_worktree import ignore_scratch

    repo, scratch_dir = sandbox.repo_dir, sandbox.conventions.scratch_dir
    if ignored_by == "repository":
        (repo / ".gitignore").write_text(f"/{scratch_dir}/\n")
        git("add", ".gitignore", cwd=repo)
        git("commit", "-q", "-m", "ignore the scratch folder", cwd=repo)
        git("push", "-q", "origin", "HEAD:main", cwd=repo)
        git("fetch", "-q", "origin", cwd=repo)
    worktree = sandbox.worktree_root / "9"
    git(
        "worktree",
        "add",
        "-q",
        "-b",
        "agent/9-demo",
        str(worktree),
        "origin/main",
        cwd=repo,
    )
    git("push", "-q", "-u", "origin", "HEAD", cwd=worktree)
    scratch = sandbox.conventions.scratch_path(9)
    (scratch / "vitecache").mkdir(parents=True)
    (scratch / "vitecache" / "deps.json").write_text("{}")
    (scratch / "capture.png").write_bytes(b"\x89PNG")
    if ignored_by == "launch":
        ignore_scratch(sandbox)

    env = Env(sandbox, runner=run_process, stdin=io.BytesIO())
    [worker] = observe_workers(env.runner, sandbox, env.tmux)
    kept = why_kept(worker, env.window(9))

    if ignored_by is not None:
        assert kept is None
        remove_worktree(env.runner, sandbox, worker)
        assert not worktree.exists()
    else:
        assert kept == "1 uncommitted file(s)", kept
        with pytest.raises(CommandError, match="contains modified or untracked"):
            remove_worktree(env.runner, sandbox, worker)
        assert (scratch / "capture.png").exists()


def test_the_workers_token_reaches_the_provider_s_environment_only(
    sandbox: Settings, tmp_path: Path
) -> None:
    """#1529: with the workers' token file there, the window's shell exports
    it — the provider's process holds it in its environment, while neither
    its argv, the window's process nor the window's text carry the value."""
    import os
    import time

    from tracerworks.providers import Provider
    from tracerworks.runtime.tmux import Tmux
    from tracerworks.runtime.tmux_worktree import TmuxWorktreeRuntime

    secret = "github_pat_11REALTESTFAKE0000value"
    token = tmp_path / "secrets" / "worker-github.token"
    token.parent.mkdir()
    token.write_text(secret + "\n", encoding="utf-8")
    token.chmod(0o600)
    seen = tmp_path / "seen"
    stub = tmp_path / "provider-stub"
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s" "$GH_TOKEN" > {seen}.env\n'
        f'printf "%s" "$GH_CONFIG_DIR" > {seen}.config\n'
        f'printf "%s\\n" "$0" "$@" > {seen}.argv\n'
        "echo stub ready\n"
        "exec sleep 30\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    settings = replace(sandbox, worker_token_file=token, cache_dir=tmp_path / "cache")
    (settings.worktree_root / "9").mkdir(parents=True)
    provider = Provider(
        name="stub", processes=frozenset({"sleep"}), status_bar_busy="",
        spinner_busy=None, prompt_glyph=">", exit_command="exit", queued_hint=None,
        trust_dialog=None, launch=(str(stub),),
    )  # fmt: skip
    tmux = Tmux(run_process, settings.tmux_session, settings.tmux_socket)
    runtime = TmuxWorktreeRuntime(run_process, settings, tmux, time.sleep, time.time)

    runtime.spawn(9, provider, "primer", LEVEL)
    deadline = time.time() + 15
    while not Path(f"{seen}.argv").exists() and time.time() < deadline:
        time.sleep(0.2)

    assert Path(f"{seen}.env").read_text() == secret
    assert Path(f"{seen}.config").read_text() == str(settings.cache_dir / "gh-worker")
    assert secret not in Path(f"{seen}.argv").read_text()
    assert settings.tmux_socket is not None
    pane = subprocess.run(
        ["tmux", "-L", settings.tmux_socket, "capture-pane", "-p", "-t",
         "=widgets:worker-9"],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert "stub ready" in pane and secret not in pane
    pid = subprocess.run(
        ["tmux", "-L", settings.tmux_socket, "display-message", "-p", "-t",
         "=widgets:worker-9", "#{pane_pid}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
    assert secret not in cmdline and os.getpid() != int(pid)
