"""`TmuxWorktreeRuntime`: a worker is a tmux window on a worktree, named by convention.

A provider without a `launch` (Codex, until its spike) cannot be spawned, and
`launch_blocker` says so before anything is created.

THE PRIMER NEVER BECOMES AN ARGUMENT. It is written to a file under the
worktree root, and the worker is started with one fixed sentence pointing at
that file — the same rule as every message (stdin / tmux buffer, never a shell
word). The only argument built from data is the file's path, quoted.

A WORKER'S TEMPORARY FILES LIVE IN ITS WORKTREE (#1554). `/tmp` on the VPS is
RAM, and the drafts workers left there (build caches, copies, prototypes)
were kept for days. The launch script exports `TMPDIR` to the worktree's
scratch folder, which the repository ignores: `clean` takes it with the
worktree, without `--force`. tmux reads `TMUX_TMPDIR`, never `TMPDIR`.
The main clone's `info/exclude`, which every worktree reads, ignores it too:
a worktree resumed on a branch older than that rule would otherwise keep its
drafts as untracked files, and `clean` would keep it forever.

A WORKTREE INSTALLS ITS OWN DEPENDENCIES (#1709). The front's `node_modules`,
borrowed from the main clone (`shared_dirs`, #984), stayed at that clone's
lock of 26/09, and `tsc` failed in every worktree. Each `[workers] installs`
folder is now the worktree's own: before the window opens, at launch and at
resume, its command runs in the folder of its lock whenever the lock is not
the one the folder was installed from — recorded in the folder. Run by
tracerworks, not in the window: the pane's process stays the provider alone. In
a login + interactive shell, as the provider's launch: that is where `npm` is.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from tracerworks import texts, worker_token
from tracerworks.forbidden import denied
from tracerworks.guard_check import guard_blocker
from tracerworks.hook_state import hook_file, prompt_heard, worker_hooks
from tracerworks.level import Level
from tracerworks.messaging import Delivery, Messenger
from tracerworks.observe import Worker, observe_workers, worker_provider
from tracerworks.process import CommandError, Runner
from tracerworks.providers import CLAUDE, Provider
from tracerworks.quotas import statusline_settings
from tracerworks.runtime.tmux import Tmux, Window
from tracerworks.settings import Install, Settings

#: Written in an installed folder: the hash of the lock it was installed from.
INSTALLED_FROM = ".tracerworks-installed-from"
#: What an interactive shell without a terminal says, never the cause.
_SHELL_NOISE = re.compile(r"^logout$|job control|terminal process group")
EXIT_WAIT_SECONDS = 30.0
EXIT_POLL_SECONDS = 1.0


class LaunchNotConfiguredError(RuntimeError):
    """`spawn` called although `launch_blocker` said why it cannot run."""


class NoWorkerError(RuntimeError):
    """No live provider in the window: nothing can be sent to it."""


@dataclass
class TmuxWorktreeRuntime:
    runner: Runner
    settings: Settings
    tmux: Tmux
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    err: IO[str] = field(default=sys.stderr)  # what the operator is told

    def launch_blocker(self, provider: Provider) -> str | None:
        """Why `spawn` cannot run, asked BEFORE anything is created: a worktree
        prepared for a launch that cannot happen would read as a stopped
        chantier and keep `start-next` away from it."""
        if provider.launch is None:
            return texts.NOT_WIRED.format(provider=provider.name)
        # a token there but refused must never hand the owner's back (#1529)
        return worker_token.refused(
            self.settings.worker_token_file, self.settings.github_repo
        )

    def guard_blocker(self, spec: int) -> str | None:
        """Why the chantier's worktree may not run in auto mode: its session
        guard is absent or broken (#1529). Asked before a launch or a resume."""
        return guard_blocker(
            self.runner,
            self.settings.conventions.worktree_path(spec),
            self.settings.guard_probe_tool,
            self.settings.stripped_env,
            self.settings.base_ref,
        )

    def github_auth(self, spec: int) -> str:
        """What a launched or resumed worker authenticates with — said by its
        launch, so that the relauncher's journal shows it; never the value."""
        why = worker_token.missing(self.settings.worker_token_file)
        if why is None:
            return texts.WORKER_TOKEN_USED.format(n=spec)
        return texts.WORKER_TOKEN_NOT_USED.format(n=spec, why=why)

    def spawn(self, spec: int, provider: Provider, primer: str, level: Level) -> None:
        if provider.launch is None:  # GitHub was asked by `launch_blocker`
            raise LaunchNotConfiguredError(self.launch_blocker(provider))
        primer_file = self.primer_path(spec)
        primer_file.parent.mkdir(parents=True, exist_ok=True)
        primer_file.write_text(primer, encoding="utf-8")
        instruction = (
            f"Lis le fichier {primer_file} en entier : c'est la consigne de ton"
            " chantier. Exécute-la."
        )
        # A login + interactive shell: on the VPS the provider's binary is only
        # on the PATH of such a shell (fnm). `exec` so the pane's process IS the
        # provider, which is how `status` recognises a live worker.
        # A Claude worker records the quota through its status line (#822),
        # when `[quotas] claude_statusline` says so. Its model and its effort
        # are the spec's (#2251); Codex's launch is not measured yet.
        extra = self._claude_args(spec, level) if provider is CLAUDE else []
        self._launch(spec, [*provider.launch, *extra, instruction])

    def resume(self, spec: int, session: str, reason: str, level: Level) -> None:
        """Reopen a parked Claude session (#1025) in the chantier's worktree:
        the same conversation, told to read why it is resumed. Like the
        primer, the reason is a file; only its path and the session id — a
        transcript's file name — become arguments. The level is the spec's
        labels' of NOW (#2251): one changed during the pause is obeyed."""
        reason_file = self.reason_path(spec)
        reason_file.parent.mkdir(parents=True, exist_ok=True)
        reason_file.write_text(reason, encoding="utf-8")
        instruction = (
            f"Lis le fichier {reason_file} en entier : c'est la raison de ta"
            " reprise. Exécute-la."
        )
        assert CLAUDE.launch is not None
        extra = self._claude_args(spec, level)
        self._launch(spec, [*CLAUDE.launch, *extra, "--resume", session, instruction])

    def _launch(self, spec: int, argv: list[str]) -> None:
        """The provider in the chantier's worktree, its `TMPDIR` in it too."""
        for install in self.settings.installs:
            self._install(spec, install)
        scratch = self.settings.conventions.scratch_path(spec)
        scratch.mkdir(parents=True, exist_ok=True)
        ignore_scratch(self.settings)
        exports = [f"export TMPDIR={shlex.quote(str(scratch))}"]
        if self.settings.stripped_env:  # never the operator's escape (#1529)
            exports.insert(0, f"unset -v {shlex.join(self.settings.stripped_env)}")
        if worker_token.missing(self.settings.worker_token_file) is None:
            exports += self._token_exports()
        script = f"{'; '.join(exports)}; exec {shlex.join(self._scoped(spec, argv))}"
        self.tmux.new_window(
            self._name(spec),
            self.settings.conventions.worktree_path(spec),
            ["bash", "-lic", script],
        )

    def _install(self, spec: int, install: Install) -> None:
        """Install the worktree's own folder from its lock, if not yet done.
        A link there (a borrowed folder of before #1709) goes first: through
        it, the install would empty the main clone's for every worktree. A
        failure is said and the worker starts anyway — never marked, the next
        launch or resume tries again, and so does `bin/ci` at the push."""
        worktree = self.settings.conventions.worktree_path(spec)
        lock = worktree / install.lock
        if not lock.is_file():
            return
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        folder = worktree / install.path
        mark = folder / INSTALLED_FROM
        if folder.is_symlink():
            folder.unlink()
        elif mark.is_file() and mark.read_text(encoding="utf-8").strip() == digest:
            return
        result = self.runner(["bash", "-lic", install.run], cwd=lock.parent)
        if result.ok and folder.is_dir():
            mark.write_text(digest + "\n", encoding="utf-8")
            return
        said = [
            line
            for line in (result.stderr or result.stdout).strip().splitlines()
            if not _SHELL_NOISE.search(line)
        ]
        if not result.ok:
            cause = said[-1] if said else texts.INSTALL_NO_OUTPUT
        else:
            cause = texts.INSTALL_NO_FOLDER
        print(
            texts.INSTALL_FAILED.format(
                n=spec,
                path=install.path,
                code=result.returncode,
                cause=cause,
            ),
            file=self.err,
            flush=True,
        )

    def _scoped(self, spec: int, argv: list[str]) -> list[str]:
        """`argv` in a scope of its own in the workers' slice (#1724). The
        scope executes the command itself, same PID: the pane's process is
        still the provider. Named by the launch time, so a resume never meets
        the scope of a session still being stopped."""
        if not self.settings.worker_slice:
            return argv
        # The pane runs `exec systemd-run …`: if it cannot reach the user's
        # manager (no bus), the worker would never start. Tried first; the
        # worker then starts unscoped, as before #1724.
        probe = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"]
        if not self.runner(probe).ok:
            return argv
        return [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--slice={self.settings.worker_slice}",
            f"--unit={self._unit(spec)}-{int(self.clock())}",
            # #2095: the kernel kills a runaway test inside the worker's cap,
            # swap included; `continue`, or systemd's default (`stop`) ends
            # the whole scope, the worker with it, at the first kill
            f"--property=MemoryMax={self.settings.worker_memory_max_gb}G",
            "--property=MemorySwapMax=0",
            "--property=OOMPolicy=continue",
            "--",
            *argv,
        ]

    def _unit(self, spec: int) -> str:
        return self.settings.conventions.unit_name(spec)

    def _token_exports(self) -> list[str]:
        """The workers' token, read from its file BY THE WINDOW'S SHELL: only
        the file's path is a word of the command line, never the value. `gh`
        and `git push` (its credential helper) follow `GH_TOKEN`; the
        configuration folder is the tool's, with no token in it."""
        config = self.settings.cache_dir / "gh-worker"
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file = shlex.quote(str(self.settings.worker_token_file))
        return [
            f'export GH_TOKEN="$(cat {token_file})"',
            f"export GH_CONFIG_DIR={shlex.quote(str(config))}",
        ]

    def _claude_args(self, spec: int, level: Level) -> list[str]:
        return [*level.args(), *self._settings_args(spec), *self._share(spec)]

    def _settings_args(self, spec: int) -> list[str]:
        """ONE `--settings` word, launch and resume alike: the status line
        (#822), the hooks that write where the worker is (#1728) — added to
        the project's, the session guard among them — and the rules the
        worker is denied (#1529). A deny rule holds
        in auto mode, before the classifier, over any `allow` of the project.
        Not `--disallowedTools`: variadic, it swallowed the instruction after
        it like `--add-dir` (09/10), and it splits its list on spaces and
        commas, which rules hold."""
        launch: dict[str, object] = {
            **statusline_settings(self.settings),
            "hooks": worker_hooks(self.settings.cache_dir, spec),
            "permissions": {"deny": denied(self.settings)},
        }
        return ["--settings", json.dumps(launch, ensure_ascii=False)]

    def _share(self, spec: int) -> list[str]:
        """Link each shared folder of the main clone into the worktree, and
        allow its target (#984): a worker that made that link itself stopped
        on « Do you want to proceed? » — a path outside its folder — with no
        one told. A worktree's own folder there is left as it is; a folder
        the main clone lacks is neither linked nor allowed."""
        worktree = self.settings.conventions.worktree_path(spec)
        args: list[str] = []
        for shared in self.settings.shared_dirs:
            target = self.settings.repo_dir / shared
            if not target.is_dir():
                continue
            link = worktree / shared
            if not link.exists() and not link.is_symlink():
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
            # One word, glued: `--add-dir <directories...>` is variadic, and
            # given apart it swallowed the instruction after it (09/10).
            args.append(f"--add-dir={target}")
        return args

    def reason_path(self, spec: int) -> Path:
        return (
            self.settings.conventions.worktree_root / ".primers" / f"{spec}-resume.md"
        )

    def primer_path(self, spec: int) -> Path:
        """Beside the worktrees, never inside one: a primer is not code."""
        return self.settings.conventions.worktree_root / ".primers" / f"{spec}.md"

    def status(self, spec: int) -> Worker | None:
        workers = observe_workers(self.runner, self.settings, self.tmux)
        return next((w for w in workers if w.spec == spec), None)

    def send(
        self, spec: int, text: str, stamp: str, wait_idle: bool = False
    ) -> Delivery:
        worker = self.status(spec)
        provider = worker_provider(worker.window) if worker and worker.window else None
        if provider is None:
            raise NoWorkerError(texts.NO_LIVE_PROVIDER.format(window=self._name(spec)))
        state = hook_file(self.settings.cache_dir, spec)
        messenger = Messenger(
            self.tmux,
            self._name(spec),
            provider,
            self.settings.operator,
            self.sleep,
            self.clock,
            # a Claude worker's hook proves a message arrived (#1731)
            heard=(lambda marker: prompt_heard(state, marker))
            if provider is CLAUDE
            else None,
            # a text left in its field, said before it is cleared (#2003)
            note=lambda line: print(line, file=self.err, flush=True),
        )
        return messenger.send(text, stamp, wait_idle)

    def stop(self, spec: int) -> None:
        """Ask the provider to quit, wait a little, then close the window.

        The worktree and the branch are kept: stopping never deletes code.
        """
        try:
            self._close(spec)
        finally:
            if self.settings.worker_slice:
                # What the provider started (tests, servers, browsers) is in
                # its scope whatever its parent, even once its window is gone:
                # stopping the scope stops all of it. Not checked: no scope
                # left is the usual case.
                self.runner(
                    ["systemctl", "--user", "stop", f"{self._unit(spec)}-*.scope"]
                )

    def _close(self, spec: int) -> None:
        name = self._name(spec)
        window = self._window(name)
        if window is None:
            return
        provider = worker_provider(window)
        if provider is not None and window.command in provider.processes:
            self.tmux.send_keys(name, "Escape")
            self.sleep(EXIT_POLL_SECONDS)
            self.tmux.send_keys(name, provider.exit_command, literal=True)
            self.tmux.send_keys(name, "Enter")
            deadline = self.clock() + EXIT_WAIT_SECONDS
            while self.clock() < deadline:
                self.sleep(EXIT_POLL_SECONDS)
                current = self._window(name)
                if current is None or current.command not in provider.processes:
                    break
        if self._window(name) is not None:
            try:
                self.tmux.kill_window(name)
            except CommandError:
                # `/exit` may close the window between the look and the kill
                # (23/10, #1682): already closed is closed.
                if self._window(name) is not None:
                    raise

    def _name(self, spec: int) -> str:
        return self.settings.conventions.window_name(spec)

    def _window(self, name: str) -> Window | None:
        return next((w for w in self.tmux.list_windows() if w.name == name), None)


def ignore_scratch(settings: Settings) -> None:
    """Add the scratch folder to the main clone's `info/exclude`, once.

    Every worktree of the clone reads that file, whatever its branch holds.
    A clone whose `.git` is not a folder is left alone.
    """
    git_dir = settings.repo_dir / ".git"
    if not git_dir.is_dir():
        return
    rule = f"/{settings.conventions.scratch_dir}/"
    exclude = git_dir / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if rule in lines:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join([*lines, rule]) + "\n", encoding="utf-8")
