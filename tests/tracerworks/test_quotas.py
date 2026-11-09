"""The quotas on the dashboard, read and never declared (#822, #823).

Claude: what Claude Code hands its status line (`rate_limits`), recorded by
`tracerworks statusline`, read back by the snapshot. GitHub Actions: the billing
usage of the month, asked with a dedicated read-only token at most once per
15 minutes. Whatever cannot be read is `None` with its reason — never a
number made up, never an old one passed off as current.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tracerworks.quotas import (
    ACTIONS_TTL,
    actions_quota,
    claude_quota,
    included_minutes,
    measured_weekly,
    record_statusline,
    statusline_settings,
    used_percent,
)

NOW = datetime(2026, 9, 30, 16, 0, tzinfo=UTC).timestamp()
HOUR = 3600.0


def status(five: float | None = 62.0, week: float | None = 88.4) -> str:
    """What Claude Code sends its status line (the documented fields only)."""
    limits: dict[str, Any] = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": NOW + 2 * HOUR}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week, "resets_at": NOW + 77 * HOUR}
    return json.dumps({"model": {"id": "x"}, "rate_limits": limits})


@pytest.fixture
def quota_file(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "claude-quota.json"


# --- Claude: the status line records, the snapshot reads ------------------------------


def test_the_status_line_records_both_windows_and_prints_them(quota_file: Path) -> None:
    line = record_statusline(status(), quota_file, NOW)
    assert line == "Claude quota 5 h 62% · week 88%"
    assert stat.S_IMODE(quota_file.stat().st_mode) == 0o600
    assert claude_quota(quota_file, NOW + 60) == {
        "five_hour": {
            "used_percent": 62.0,
            "resets_at": NOW + 2 * HOUR,
            "seen_at": NOW,
        },
        "seven_day": {
            "used_percent": 88.4,
            "resets_at": NOW + 77 * HOUR,
            "seen_at": NOW,
        },
        "previous": {},
        "error": None,
    }


def test_a_status_without_rate_limits_keeps_what_was_seen(quota_file: Path) -> None:
    """Before the session's first answer there are no `rate_limits`: that is
    not a reading, and it must not erase the last one."""
    record_statusline(status(), quota_file, NOW)
    assert record_statusline(json.dumps({"model": {}}), quota_file, NOW + 5) == ""
    record_statusline(status(five=None, week=90), quota_file, NOW + 10)
    read = claude_quota(quota_file, NOW + 10)
    assert read["five_hour"]["used_percent"] == 62.0
    assert read["five_hour"]["seen_at"] == NOW
    assert (read["seven_day"]["used_percent"], read["seven_day"]["seen_at"]) == (
        90,
        NOW + 10,
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": "62"}}}),
        json.dumps({"rate_limits": {"seven_day": {"used_percentage": 50}}}),
        json.dumps(
            {"rate_limits": {"five_hour": {"used_percentage": True, "resets_at": 1}}}
        ),
    ],
)
def test_a_malformed_status_records_nothing_and_never_fails(
    quota_file: Path, payload: str
) -> None:
    assert record_statusline(payload, quota_file, NOW) == ""
    assert not quota_file.exists()


def test_a_window_past_its_reset_is_unknown(quota_file: Path) -> None:
    """After `resets_at` the recorded percent belongs to the previous window."""
    record_statusline(status(), quota_file, NOW)
    read = claude_quota(quota_file, NOW + 3 * HOUR)
    assert read["five_hour"] is None
    assert read["seven_day"] is not None


def test_a_window_past_its_reset_keeps_the_previous_reading_apart(
    quota_file: Path,
) -> None:
    """#862: the new window is unknown, but when it opened, what the previous
    one ended on, and when the new one ends (5 h after the previous) are known."""
    record_statusline(status(five=50.0), quota_file, NOW)
    read = claude_quota(quota_file, NOW + 3 * HOUR)
    assert read["five_hour"] is None
    assert read["previous"] == {
        "five_hour": {
            "used_percent": 50.0,
            "seen_at": NOW,
            "ended_at": NOW + 2 * HOUR,
            "next_resets_at": NOW + 7 * HOUR,
        }
    }


def test_a_window_more_than_one_period_ago_gives_no_next_reset(
    quota_file: Path,
) -> None:
    """Past the end of the window after it, its end is not known: none is said."""
    record_statusline(status(five=50.0), quota_file, NOW)
    read = claude_quota(quota_file, NOW + 8 * HOUR)
    assert read["previous"]["five_hour"]["ended_at"] == NOW + 2 * HOUR
    assert read["previous"]["five_hour"]["next_resets_at"] is None


def test_the_week_past_its_reset_ends_a_week_later(quota_file: Path) -> None:
    record_statusline(status(), quota_file, NOW)
    read = claude_quota(quota_file, NOW + 78 * HOUR)
    assert read["seven_day"] is None
    assert read["previous"]["seven_day"]["next_resets_at"] == NOW + (77 + 168) * HOUR


def test_a_fresh_reading_leaves_no_previous_window(quota_file: Path) -> None:
    record_statusline(status(five=50.0), quota_file, NOW)
    record_statusline(
        json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 4, "resets_at": NOW + 7 * HOUR}
                }
            }
        ),
        quota_file,
        NOW + 3 * HOUR,
    )
    read = claude_quota(quota_file, NOW + 3 * HOUR)
    assert read["five_hour"]["used_percent"] == 4
    assert "five_hour" not in read["previous"]


def test_nothing_recorded_is_unknown_with_its_reason(quota_file: Path) -> None:
    read = claude_quota(quota_file, NOW)
    assert (read["five_hour"], read["seven_day"]) == (None, None)
    assert "no Claude status line" in read["error"]
    quota_file.parent.mkdir(parents=True)
    quota_file.write_text("{broken", encoding="utf-8")
    assert "unreadable" in claude_quota(quota_file, NOW)["error"]


def test_the_admission_cap_reads_the_measured_week(quota_file: Path) -> None:
    assert measured_weekly(quota_file, NOW) is None
    record_statusline(status(week=81.6), quota_file, NOW)
    assert measured_weekly(quota_file, NOW) == 81.6
    assert measured_weekly(quota_file, NOW + 78 * HOUR) is None


def test_the_worker_status_line_is_only_given_when_switched_on(tmp_path: Path) -> None:
    from dataclasses import replace

    from tests.tracerworks.fakes import recorded_settings

    settings = recorded_settings(tmp_path)
    assert statusline_settings(replace(settings, claude_statusline=False)) == {}
    given = statusline_settings(replace(settings, claude_statusline=True))
    assert given["statusLine"] == {
        "type": "command",
        "command": f"{tmp_path / 'bin' / 'tracerworks'} statusline",
    }


# --- GitHub Actions: the month's minutes, with a read-only token ---------------------


class Billing:
    """GitHub's billing API, as recorded: counts the calls, never sees the
    network."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = body if body is not None else summary()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, token: str) -> tuple[int, bytes]:
        self.calls.append((url, token))
        return self.status, json.dumps(self.body).encode()


def summary(*minutes: float) -> dict[str, Any]:
    items = [
        {
            "product": "actions",
            "sku": "actions_linux",
            "unitType": "Minutes",
            "grossQuantity": m,
            "netQuantity": 0,
        }
        for m in (minutes or (1520.0,))
    ]
    items.append(
        {
            "product": "actions",
            "sku": "actions_storage",
            "unitType": "GigabyteHours",
            "grossQuantity": 99.0,
            "netQuantity": 0,
        }
    )
    return {
        "timePeriod": {"year": 2026, "month": 9},
        "user": "acme-ops",
        "usageItems": items,
    }


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    token = tmp_path / "github-billing.token"
    token.write_text("github_pat_secret\n", encoding="utf-8")
    token.chmod(0o600)
    return token


def read_actions(
    token_file: Path, tmp_path: Path, billing: Billing, now: float = NOW
) -> dict[str, Any]:
    return actions_quota(
        token_file, "acme-ops", 2000, tmp_path / "cache" / "actions.json", now, billing
    )


def test_the_month_minutes_against_the_plan(token_file: Path, tmp_path: Path) -> None:
    billing = Billing(body=summary(1000.0, 520.0))
    read = read_actions(token_file, tmp_path, billing)
    assert read == {
        "used_minutes": 1520.0,
        "included_minutes": 2000,
        "used_percent": 76.0,
        "resets_at": datetime(2026, 10, 1, tzinfo=UTC).timestamp(),
        "read_at": NOW,
        "error": None,
    }
    [(url, token)] = billing.calls
    assert url == (
        "https://api.github.com/users/acme-ops/settings/billing/usage/summary"
        "?year=2026&month=9&product=actions"
    )
    assert token == "github_pat_secret"


@pytest.mark.parametrize(
    ("used", "included", "percent"),
    [(1520.0, 2000, 76.0), (1520.0, 4000, 38.0), (0.0, 0, 100.0), (5.0, 0, 100.0)],
)
def test_the_percent_of_the_plan(used: float, included: int, percent: float) -> None:
    """#2192: zero minutes included — every minute is over the plan."""
    assert used_percent(used, included) == percent


def test_a_cached_reading_is_weighed_against_the_minutes_in_force(
    token_file: Path, tmp_path: Path
) -> None:
    billing = Billing(body=summary(1000.0, 520.0))
    read_actions(token_file, tmp_path, billing)
    raised = actions_quota(
        token_file, "acme-ops", 4000, tmp_path / "cache" / "actions.json", NOW, billing
    )
    assert (raised["included_minutes"], raised["used_percent"]) == (4000, 38.0)
    assert len(billing.calls) == 1, "GitHub is not asked again for it"


def test_the_minutes_in_force_are_the_tab_s_else_the_project_file_s(
    tmp_path: Path,
) -> None:
    from tests.tracerworks.fakes import recorded_settings
    from tracerworks.state_file import State

    settings = recorded_settings(tmp_path)
    assert included_minutes(settings, State()) == settings.actions_included_minutes
    assert included_minutes(settings, State(github_actions_included_minutes=0)) == 0


def test_asked_at_most_once_per_fifteen_minutes(
    token_file: Path, tmp_path: Path
) -> None:
    billing = Billing()
    read_actions(token_file, tmp_path, billing)
    again = read_actions(token_file, tmp_path, billing, NOW + ACTIONS_TTL - 1)
    assert len(billing.calls) == 1
    assert again["read_at"] == NOW
    read_actions(token_file, tmp_path, billing, NOW + ACTIONS_TTL)
    assert len(billing.calls) == 2


def test_the_token_never_leaves_the_call(token_file: Path, tmp_path: Path) -> None:
    read_actions(token_file, tmp_path, Billing())
    read_actions(token_file, tmp_path, Billing(status=403), NOW + ACTIONS_TTL)
    cached = (tmp_path / "cache" / "actions.json").read_text(encoding="utf-8")
    assert "github_pat_secret" not in cached


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (403, {"message": "Resource not accessible"}, "HTTP 403"),
        (404, {"message": "Not Found"}, '"Plan: read"'),
        (200, {"usageItems": "nope"}, "unreadable"),
        (200, [], "unreadable"),
    ],
)
def test_a_refusal_or_an_odd_answer_is_unknown_with_its_reason(
    token_file: Path, tmp_path: Path, status: int, body: Any, reason: str
) -> None:
    read = read_actions(token_file, tmp_path, Billing(status, body))
    assert read["used_percent"] is None and read["used_minutes"] is None
    assert reason in read["error"]


def test_no_token_or_a_readable_token_is_unknown_and_not_asked(
    token_file: Path, tmp_path: Path
) -> None:
    billing = Billing()
    token_file.chmod(0o644)
    read = read_actions(token_file, tmp_path, billing)
    assert (read["used_percent"], billing.calls) == (None, [])
    assert "chmod 600" in read["error"]
    token_file.unlink()
    read = read_actions(token_file, tmp_path, billing, NOW + ACTIONS_TTL)
    assert "no billing token" in read["error"]


def test_the_network_failing_is_unknown(token_file: Path, tmp_path: Path) -> None:
    def down(url: str, token: str) -> tuple[int, bytes]:
        raise OSError("connection refused")

    read = actions_quota(token_file, "acme-ops", 2000, tmp_path / "a.json", NOW, down)
    assert read["used_percent"] is None
    assert "connection refused" in read["error"]


# --- the command Claude Code runs -----------------------------------------------------


def test_tracerworks_statusline_records_from_stdin(tmp_path: Path) -> None:
    import io
    from dataclasses import replace

    from tests.tracerworks.fakes import FakeRunner, recorded_settings
    from tracerworks.cli import EXIT_OK, Env, run

    quota = tmp_path / "claude-quota.json"
    settings = replace(recorded_settings(tmp_path), claude_quota_file=quota)
    out = io.StringIO()
    env = Env(
        settings,
        runner=FakeRunner(),
        now=lambda: NOW,
        stdin=io.BytesIO(status().encode()),
        out=out,
    )
    assert run(env, ["statusline"]) == EXIT_OK
    assert out.getvalue() == "Claude quota 5 h 62% · week 88%\n"
    assert claude_quota(quota, NOW)["seven_day"]["used_percent"] == 88.4
    env = Env(settings, runner=FakeRunner(), stdin=io.BytesIO(b"\xff garbage"), out=out)
    assert run(env, ["statusline"]) == EXIT_OK
