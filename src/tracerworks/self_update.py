"""`tracerworks self-update`: install the tool from the base branch, never a worktree.

A worker editing its worktree — even the tool's own folder on its branch — must
not change the tool that drives it (architecture §6.1). So the installed copy
is extracted from the COMMIT the remote base branch points to, by `git
archive`, into `<install_dir>/<sha>/`, together with the project's
configuration file (reviewed like the code, installed with it); a `current`
symlink is swapped atomically; and the launcher `<bin_dir>/tracerworks` runs that
copy with the system Python — the tool needs nothing outside the standard
library — pointing `TRACERWORKS_CONFIG` at the installed configuration unless the
caller set one.

Bootstrap, once, from the main clone of the project:
    PYTHONPATH=<parent of the tool's folder> python3 -m tracerworks \\
        --config <project file> self-update
"""

from __future__ import annotations

import os
import shlex
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from tracerworks.process import Runner, checked
from tracerworks.settings import Settings

LAUNCHER = """#!/bin/sh
# Written by `tracerworks self-update` — do not edit; rerun it to update.
: "${{TRACERWORKS_CONFIG:={config}}}"
export TRACERWORKS_CONFIG
PYTHONPATH={path} exec python3 -m tracerworks "$@"
"""


@dataclass(frozen=True)
class Installed:
    previous: str | None
    current: str


def installed_commit(settings: Settings) -> str | None:
    link = settings.install_dir / "current"
    return link.readlink().name if link.is_symlink() else None


def self_update(runner: Runner, settings: Settings) -> Installed:
    repo, base = str(settings.repo_dir), settings.base_ref
    checked(
        runner(
            [
                "git",
                "-C",
                repo,
                "fetch",
                "--quiet",
                settings.remote,
                settings.base_branch,
            ]
        ),
        "git fetch",
    )
    sha = checked(
        runner(["git", "-C", repo, "rev-parse", "--verify", f"{base}^{{commit}}"]),
        f"git rev-parse {base}",
    ).strip()
    previous = installed_commit(settings)
    target = settings.install_dir / sha
    if not (target / settings.source_dir / "__main__.py").exists():
        settings.install_dir.mkdir(parents=True, exist_ok=True)
        archive = settings.install_dir / f".{sha}.tar"
        checked(
            runner(
                [
                    "git",
                    "-C",
                    repo,
                    "archive",
                    "--format=tar",
                    f"--output={archive}",
                    sha,
                    settings.source_dir,
                    settings.config_in_repo,
                ]
            ),
            "git archive",
        )
        staging = settings.install_dir / f".{sha}.staging"
        # A crashed earlier run may have left these: start clean.
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        with tarfile.open(archive) as tar:
            tar.extractall(staging, filter="data")
        archive.unlink()
        staging.replace(target)
    link = settings.install_dir / "current"
    tmp_link = settings.install_dir / f".current.{os.getpid()}"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(sha)
    tmp_link.replace(link)
    settings.bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = settings.bin_dir / "tracerworks"
    tmp_launcher = settings.bin_dir / f".tracerworks.{os.getpid()}"
    tmp_launcher.write_text(
        LAUNCHER.format(
            config=shlex.quote(str(link / settings.config_in_repo)),
            path=shlex.quote(str(link / Path(settings.source_dir).parent)),
        ),
        encoding="utf-8",
    )
    tmp_launcher.chmod(0o755)
    tmp_launcher.replace(launcher)
    return Installed(previous, sha)
