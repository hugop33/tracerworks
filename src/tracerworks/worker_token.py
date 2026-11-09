"""The workers' fine-grained GitHub token (#1529): theirs when it is there.

A worker held the owner's token (`repo, workflow, gist, read:org`). When the
file `[workers] github_token_file` holds a token, a worker's `gh` and
`git push` use it instead: the launch script reads the file and exports
`GH_TOKEN` — the VPS's git credential helper is `gh auth git-credential`,
which follows it — and points `GH_CONFIG_DIR` at a folder of the tool's with
no token in it, so that unsetting one variable never falls back to the
owner's stored configuration.

- present and accepted by GitHub (a read of the repository with it): used;
- absent, empty or readable by others: the launch goes on with the machine's
  authentication, and says so — its output, and one alert per episode;
- present but refused by GitHub (expired, revoked): the launch is refused
  before anything is created — an expired token must never silently hand
  the worker the owner's own.

THE VALUE IS NEVER SAID: not in an argument, the primer, a journal, the
snapshot or an error. Only its state is: present, absent, refused.
"""

from __future__ import annotations

import stat
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from tracerworks import texts

API = "https://api.github.com/repos/{repo}"

#: (url, token) → the HTTP status; the token goes in a header, never in a URL.
Fetch = Callable[[str, str], int]


class TokenMissing(ValueError):
    """No usable token in the file: the launch goes on, and says why."""


def fetch_status(url: str, token: str) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return int(answer.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0  # unreachable: never « accepted »


def read(path: Path) -> str:
    """The token, or `TokenMissing` saying why there is none."""
    try:
        mode = path.stat().st_mode
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise TokenMissing(texts.WORKER_TOKEN_ABSENT.format(path=path)) from exc
    except OSError as exc:
        raise TokenMissing(
            texts.WORKER_TOKEN_UNREADABLE.format(path=path, kind=type(exc).__name__)
        ) from exc
    if stat.S_IMODE(mode) & 0o077:
        raise TokenMissing(texts.READABLE_BY_OTHERS.format(path=path))
    if not text:
        raise TokenMissing(texts.EMPTY_FILE.format(path=path))
    return text


def missing(path: Path) -> str | None:
    """Why the workers have no token of theirs; None: the file holds one."""
    try:
        read(path)
    except TokenMissing as exc:
        return str(exc)
    return None


def refused(path: Path, repo: str, fetch: Fetch | None = None) -> str | None:
    """Why a token that IS there may not be used: GitHub refuses it, or could
    not be asked — « unknown » is never « accepted ». None: accepted, or no
    token at all (`missing` says that one)."""
    try:
        token = read(path)
    except TokenMissing:
        return None
    status = (fetch or fetch_status)(API.format(repo=repo), token)
    if status == 200:
        return None
    return texts.WORKER_TOKEN_REFUSED.format(path=path, status=status or "unreachable")
