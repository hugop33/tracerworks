"""The GitHub minutes' safety net (#1296, spec #1294).

Past `ABOVE` % of the month's included minutes, `serve` sets a repository
variable the project names (`[quotas] budget_variable`), which its CI reads —
for the project that built it, a pull request asking for GitHub's machines
then runs on its self-hosted runners, its summary saying why.
Below again — the month started over — the variable goes. No variable named:
no net.

Read each time the minutes are (`ACTIONS_EVERY`): the variable is the truth,
whoever set or removed it. Minutes that cannot be read change nothing — never
an « exhausted » made up; variables that cannot be read are an error, said.
Each switch is said in the service's journal; the Telegram alert is the
episode `alerts` opens from the same reading.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from tracerworks import texts
from tracerworks.process import CommandError, Runner, checked

ABOVE: Final = 90.0


def low(used_percent: float | None) -> bool | None:
    """Whether this month's minutes are past the net; None when unknown."""
    return None if used_percent is None else used_percent >= ABOVE


def is_set(runner: Runner, repo: str, variable: str) -> bool:
    command = f"gh variable list --repo {repo}"
    listed = checked(
        runner(["gh", "variable", "list", "--repo", repo, "--json", "name"]), command
    )
    try:
        return any(v["name"] == variable for v in json.loads(listed))
    except (ValueError, TypeError, KeyError) as exc:
        raise CommandError(texts.UNREADABLE_ANSWER.format(command=command)) from exc


def apply(
    runner: Runner, repo: str, variable: str, read: Mapping[str, Any], now: float
) -> str | None:
    """Set or remove the variable as this month's reading asks: what was
    switched, said, or None when nothing was. A reading of a past month (a
    cached one, just after the 1st) is no reading."""
    current = read.get("error") is None and float(read.get("resets_at") or 0) > now
    percent = read.get("used_percent") if current else None
    wanted = low(percent)
    if wanted is None or is_set(runner, repo, variable) == wanted:
        return None
    if wanted:
        setting = ["gh", "variable", "set", variable, "--repo", repo, "--body", "true"]
        checked(runner(setting), f"gh variable set {variable}")
        return texts.BUDGET_LOW.format(
            percent=percent, threshold=ABOVE, variable=variable
        )
    deleting = ["gh", "variable", "delete", variable, "--repo", repo]
    checked(runner(deleting), f"gh variable delete {variable}")
    return texts.BUDGET_BACK.format(percent=percent, variable=variable)
