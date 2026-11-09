"""What `tracerworks` needs to know about each provider's terminal interface.

A provider is `claude` (Claude Code) or `codex` (Codex CLI); the exact model is
a detail of the launch, not of routing (architecture §4.1). Each profile says
how to tell it is alive (the pane's foreground process), busy (the « esc to
interrupt » both interfaces print while they work) or idle, where its input
field is, and how to ask it to quit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    processes: frozenset[str]  # `#{pane_current_command}` while it runs
    # « Working » is read in two places. BELOW the input field, the status bar:
    # it is cut to the window's width — measured on 26/09 with a worker running
    # subagents, 80 columns left « esc to int… », hence a prefix. ABOVE the
    # input field, the spinner line, which survives any width.
    status_bar_busy: str
    spinner_busy: str | None
    prompt_glyph: str  # first character of the input field's line
    exit_command: str
    # A message typed while it works waits in a queue; this hint follows each
    # queued message — or None while that has not been measured.
    queued_hint: str | None
    # The question asked when it opens in a folder it does not trust yet.
    trust_dialog: str | None
    # How a worker of this provider is started, first message excepted — or
    # None while that launch has not been measured on the machine.
    launch: tuple[str, ...] | None


CLAUDE = Provider(
    name="claude",
    processes=frozenset({"claude"}),
    status_bar_busy="esc to int",
    # While it thinks: « ✻ Topsy-turvying… (28s · ↓ 2.0k tokens) »; at rest the
    # same line reads « ✻ Brewed for 5s · done 10:58 AM ».
    spinner_busy=r"…\s\((?:\d+h\s)?(?:\d+m\s)?\d+s\s·",
    prompt_glyph="\u276f",  # heavy right-pointing angle
    exit_command="/exit",
    # Measured on 30/09 (v2.1.285): the queued message sits above the spinner,
    # « ctrl+x ctrl+s to send now » under it, and the empty field reads, dimmed,
    # « Press up to edit queued messages ».
    queued_hint="to send now",
    trust_dialog="Yes, I trust this folder",
    # Auto mode: the worker acts without asking at each step, under Claude Code's
    # own safety classifier. Decided by the operator on 26/09 (spec #565 thread).
    launch=("claude", "--permission-mode", "auto"),
)

# Provisional until the spike #571 measures it on the VPS.
CODEX = Provider(
    name="codex",
    processes=frozenset({"codex"}),
    status_bar_busy="esc to int",
    spinner_busy=None,
    prompt_glyph="\u203a",  # single right-pointing angle quotation mark
    exit_command="/quit",
    queued_hint=None,
    trust_dialog=None,
    launch=None,  # not connected on the VPS yet; measured by the Codex spike first
)

PROVIDERS: dict[str, Provider] = {p.name: p for p in (CLAUDE, CODEX)}


def provider_of_process(command: str) -> Provider | None:
    for provider in PROVIDERS.values():
        if command in provider.processes:
            return provider
    return None
