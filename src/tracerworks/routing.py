"""Which provider a NEW worker gets (architecture §4).

The configured provider decides; the weekly cap is only an ADMISSION guard on
top of it. The weekly usage is the one Claude Code reports (`measured`, read
from the workers' status line, #822); the one the operator declares (`tracerworks quota
78`) is the fallback. Waiting for « usage limit reached » would be too late.
At or above the cap, Claude is not admitted — Codex if its route is open,
else a refusal. Going over the cap is an explicit human gesture
(`start <n> --over-cap`), which `start-next` never makes.
"""

from __future__ import annotations

from dataclasses import dataclass

from tracerworks import texts
from tracerworks.state_file import State


@dataclass(frozen=True)
class Route:
    provider: str | None  # None: refused
    reason: str


CODEX_CLOSED = texts.CODEX_CLOSED


def route(
    state: State, *, over_cap: bool = False, measured: float | None = None
) -> Route:
    if state.provider == "codex":
        if state.codex_available:
            return Route("codex", texts.PROVIDER_CODEX)
        return Route(None, CODEX_CLOSED)
    cap = state.claude_weekly_cap
    if measured is not None:
        used: int | None = round(measured)
        source = texts.MEASURED
    else:
        used, source = state.claude_weekly_used, texts.DECLARED
    if used is None:
        return Route("claude", texts.CLAUDE_NO_QUOTA)
    said = {"used": used, "cap": cap, "source": source}
    if used < cap:
        return Route("claude", texts.CLAUDE_UNDER_CAP.format(**said))
    if over_cap:
        return Route("claude", texts.OVER_CAP.format(**said))
    if state.codex_available:
        return Route("codex", texts.TO_CODEX.format(**said))
    return Route(None, texts.CAP_AND_CLOSED.format(**said, closed=CODEX_CLOSED))
