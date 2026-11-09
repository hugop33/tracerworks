"""The quotas the dashboard shows, READ — never declared (#822, #823).

Claude: Claude Code hands its status line, on stdin, the account's usage of
the 5-hour and 7-day windows (`rate_limits.<window>.used_percentage`,
`resets_at`: documented, no token handled — code.claude.com/docs/en/statusline).
`tracerworks statusline` is that status line: it writes what it saw to a file,
and the snapshot reads it back. A window past its `resets_at` is unknown: its
percent belongs to the previous window, kept apart as such — with the time the
new window opened and, while it can only be the next one, its end (#862).
Nothing recorded is unknown too.

GitHub Actions: the billing usage of the current month
(`/users/{user}/settings/billing/usage/summary`), asked with a dedicated
fine-grained token (« Plan: read ») kept outside the repository, mode 600,
at most once per `ACTIONS_TTL`. The plan's included minutes are not in that
answer: they are configuration (`[quotas] actions_included_minutes`), which
the Settings tab may override in the state file (#2192, `included_minutes`),
read again at each reading: a reading kept in the cache is weighed again
against the minutes in force, never asked of GitHub again for that. Zero
included minutes: every minute is over the plan (100 %).

Whatever cannot be read is None with its reason, said on the dashboard.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracerworks import texts
from tracerworks.settings import Settings
from tracerworks.state_file import State

WINDOWS = ("five_hour", "seven_day")
PERIODS = {"five_hour": 5 * 3600.0, "seven_day": 7 * 24 * 3600.0}
ACTIONS_TTL = 15 * 60.0
BILLING_URL = (
    "https://api.github.com/users/{user}/settings/billing/usage/summary"
    "?year={year}&month={month}&product=actions"
)

Fetch = Callable[[str, str], tuple[int, bytes]]


# --- files ----------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_private(path: Path, data: Any) -> None:
    """Atomically, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False)
    partial.replace(path)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


# --- Claude ---------------------------------------------------------------------------


def _windows_of(payload: str) -> dict[str, dict[str, float]]:
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return {}
    seen: dict[str, dict[str, float]] = {}
    for name in WINDOWS:
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percentage"))
        resets = _number(window.get("resets_at"))
        if used is not None and resets is not None:
            seen[name] = {"used_percent": used, "resets_at": resets}
    return seen


def record_statusline(payload: str, path: Path, now: float) -> str:
    """Record what Claude Code handed its status line; the line to print.

    A status line must never fail: anything unreadable records nothing and
    prints nothing. A window absent from this payload keeps its last reading.
    """
    seen = _windows_of(payload)
    if not seen:
        return ""
    try:
        recorded = _read_json(path)
    except (OSError, ValueError):
        recorded = {}
    if not isinstance(recorded, dict):
        recorded = {}
    for name, window in seen.items():
        recorded[name] = {**window, "seen_at": now}
    with contextlib.suppress(OSError):  # a status line never fails
        _write_private(path, recorded)
    labels = texts.STATUSLINE_WINDOWS
    parts = [f"{labels[n]} {round(seen[n]['used_percent'])}%" for n in seen]
    return texts.STATUSLINE.format(parts=" · ".join(parts))


def _previous(
    used: float, seen: float, ended: float, now: float, period: float
) -> dict[str, Any]:
    """A window past its reset: what it ended on, when, and the end of the
    window after it — known only while `now` is still inside that one."""
    following = ended + period
    return {
        "used_percent": used,
        "seen_at": seen,
        "ended_at": ended,
        "next_resets_at": following if now < following else None,
    }


def claude_quota(path: Path, now: float) -> dict[str, Any]:
    """Both windows as last seen — each None when unknown — and why; under
    `previous`, the last reading of a window that has since reset (#862)."""
    unknown: dict[str, Any] = {**dict.fromkeys(WINDOWS), "previous": {}}
    try:
        recorded = _read_json(path)
    except FileNotFoundError:
        return {**unknown, "error": texts.NO_STATUSLINE}
    except (OSError, ValueError) as exc:
        return {**unknown, "error": texts.READING_UNREADABLE_WHY.format(error=exc)}
    if not isinstance(recorded, dict):
        return {**unknown, "error": texts.READING_UNREADABLE}
    read: dict[str, Any] = {**unknown, "previous": {}}
    for name in WINDOWS:
        window = recorded.get(name)
        if not isinstance(window, dict):
            continue
        used, resets, seen = (
            _number(window.get(k)) for k in ("used_percent", "resets_at", "seen_at")
        )
        if used is None or resets is None or seen is None:
            continue
        if resets <= now:
            read["previous"][name] = _previous(used, seen, resets, now, PERIODS[name])
            continue
        read[name] = {"used_percent": used, "resets_at": resets, "seen_at": seen}
    return {**read, "error": None}


def measured_weekly(path: Path, now: float) -> float | None:
    """The weekly percent the admission cap reads when it is known."""
    week = claude_quota(path, now)["seven_day"]
    return None if week is None else float(week["used_percent"])


def statusline_settings(settings: Settings) -> dict[str, object]:
    """What a Claude worker's launch settings carry so that its status line
    records the quota — nothing while `[quotas] claude_statusline` is off."""
    if not settings.claude_statusline:
        return {}
    command = f"{settings.bin_dir / 'tracerworks'} statusline"
    return {"statusLine": {"type": "command", "command": command}}


# --- GitHub Actions -------------------------------------------------------------------

INCLUDED_KEY = "github.actions_included_minutes"


def included_minutes(settings: Settings, state: State) -> int:
    """The month's included minutes in force: the Settings tab's (#2192),
    else the project file's."""
    tab = state.github_actions_included_minutes
    return settings.actions_included_minutes if tab is None else tab


def state_defaults(settings: Settings) -> dict[str, object]:
    """The defaults of the state file's optional settings, from the project."""
    return {INCLUDED_KEY: settings.actions_included_minutes}


def used_percent(used: float, included: int) -> float:
    return round(used / included * 100, 1) if included > 0 else 100.0


def _weighed(read: dict[str, Any], included: int) -> dict[str, Any]:
    """A reading against `included` minutes: its percent follows."""
    if read.get("included_minutes") == included:
        return read
    used = _number(read.get("used_minutes"))
    percent = None if used is None else used_percent(used, included)
    return {**read, "included_minutes": included, "used_percent": percent}


def fetch_billing(url: str, token: str) -> tuple[int, bytes]:
    """The real call; the token goes in a header, never in a URL or a log."""
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
            return answer.status, answer.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _token(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(texts.NO_BILLING_TOKEN.format(path=path)) from exc
    if stat.S_IMODE(mode) & 0o077:
        raise ValueError(texts.READABLE_BY_OTHERS.format(path=path))
    if not text:
        raise ValueError(texts.EMPTY_FILE.format(path=path))
    return text


def _next_month(now: float) -> float:
    today = datetime.fromtimestamp(now, UTC)
    year, month = (
        (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    )
    return datetime(year, month, 1, tzinfo=UTC).timestamp()


def _minutes(body: bytes) -> float:
    data = json.loads(body)
    items = data.get("usageItems") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(texts.NO_USAGE_ITEMS)
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(texts.BAD_USAGE_ITEMS)
        if str(item.get("unitType", "")).lower() != "minutes":
            continue
        quantity = _number(item.get("grossQuantity"))
        if quantity is None:
            raise ValueError(texts.NO_QUANTITY)
        total += quantity
    return total


def _ask_actions(
    token_file: Path, user: str, included: int, now: float, fetch: Fetch
) -> dict[str, Any]:
    unknown = {
        "used_minutes": None,
        "included_minutes": included,
        "used_percent": None,
        "resets_at": _next_month(now),
        "read_at": now,
    }
    try:
        token = _token(token_file)
    except (OSError, ValueError) as exc:
        return {**unknown, "error": str(exc)}
    today = datetime.fromtimestamp(now, UTC)
    url = BILLING_URL.format(user=user, year=today.year, month=today.month)
    try:
        status, body = fetch(url, token)
    except OSError as exc:
        return {**unknown, "error": texts.GITHUB_UNREACHABLE.format(error=exc)}
    if status != 200:
        return {**unknown, "error": texts.GITHUB_REFUSED.format(status=status)}
    try:
        used = _minutes(body)
    except ValueError as exc:
        return {**unknown, "error": texts.BILLING_UNREADABLE.format(error=exc)}
    return {
        **unknown,
        "used_minutes": used,
        "used_percent": used_percent(used, included),
        "error": None,
    }


def actions_quota(
    token_file: Path,
    user: str,
    included: int,
    cache_file: Path,
    now: float,
    fetch: Fetch = fetch_billing,
) -> dict[str, Any]:
    """This month's Actions minutes against the plan, asked at most once per
    `ACTIONS_TTL` (a failure included); the answer is kept in `cache_file`."""
    try:
        cached = _read_json(cache_file)
    except (OSError, ValueError):
        cached = None
    if (
        isinstance(cached, dict)
        and (read_at := _number(cached.get("read_at"))) is not None
        and 0 <= now - read_at < ACTIONS_TTL
    ):
        return _weighed(cached, included)
    read = _ask_actions(token_file, user, included, now, fetch)
    with contextlib.suppress(OSError):  # asked again next time, that is all
        _write_private(cache_file, read)
    return read
