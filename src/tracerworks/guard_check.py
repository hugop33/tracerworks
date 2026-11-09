"""Before a worker runs in auto mode, its worktree's session guard answers (#1529).

A chantier resumed from a worktree older than the guard (before 16/10) ran in
auto mode with no hook, and nothing said so. Before a launch or a resume,
tracerworks reads the worktree's Claude Code project settings and runs each
PreToolUse command hook as Claude Code would (`sh -c`, `CLAUDE_PROJECT_DIR`
set, the call on stdin, only for the tools its matcher names): a harmless
Bash call must pass (0), a call to the
project's probe tool — one its rulebook refuses whatever it is — must be
refused (2). No settings, no hook, or any other answer: the launch or the
resume is refused, and says how to repair. A short process per hook.

The variables the project file strips from a worker are stripped here too:
the guard is judged as the worker will meet it, never with the operator's
escape open.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tracerworks import texts
from tracerworks.process import Runner

SETTINGS = Path(".claude") / "settings.json"


def hook_commands(worktree: Path) -> list[tuple[str, str]] | None:
    """The PreToolUse command hooks of the worktree, each with its matcher
    (a regular expression on the tool's name; none or empty: every tool);
    None: unreadable."""
    try:
        data = json.loads((worktree / SETTINGS).read_text(encoding="utf-8"))
        entries = data["hooks"]["PreToolUse"]
        return [
            (str(entry.get("matcher") or ""), str(hook["command"]))
            for entry in entries
            for hook in entry["hooks"]
            if hook.get("type") == "command" and hook.get("command")
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _matches(matcher: str, tool: str) -> bool:
    """Whether Claude Code runs the hook for `tool`: as it reads a matcher —
    empty or `*` is every tool, else a regular expression; one that does not
    compile matches nothing (the guard is then not wired for it)."""
    if matcher in ("", "*"):
        return True
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def guard_blocker(
    run: Runner,
    worktree: Path,
    probe_tool: str,
    stripped: tuple[str, ...],
    base: str,
) -> str | None:
    """Why the worktree's guard is absent or broken; None: it answers.

    Each probe goes to the hooks whose matcher Claude Code would run for its
    tool — a hook wired for MCP tools only must not pass for Bash (#1575
    review). A harmless Bash call must pass every hook that sees it, the
    probe tool must be refused by one, and each must be seen by at least one."""
    hooks = hook_commands(worktree)
    if not hooks:
        return texts.GUARD_BROKEN.format(
            path=worktree, why=texts.GUARD_NOT_WIRED, base=base
        )
    harmless = {
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "cwd": str(worktree),
    }
    refused = {"tool_name": probe_tool, "tool_input": {}, "cwd": str(worktree)}
    unset = [word for name in stripped for word in ("-u", name)]
    for payload, expected in ((harmless, 0), (refused, 2)):
        tool = str(payload["tool_name"])
        seen = [command for matcher, command in hooks if _matches(matcher, tool)]
        if not seen:
            why = texts.GUARD_NOT_SEEN.format(tool=tool)
            return texts.GUARD_BROKEN.format(path=worktree, why=why, base=base)
        codes = []
        for command in seen:
            argv = [
                "env", *unset, f"CLAUDE_PROJECT_DIR={worktree}", "sh", "-c", command
            ]  # fmt: skip
            answer = run(argv, cwd=worktree, stdin=json.dumps(payload).encode())
            codes.append(answer.returncode)
        ok = all(c == 0 for c in codes) if expected == 0 else 2 in codes
        if not ok:
            code = next((c for c in codes if c != expected), codes[0])
            why = texts.GUARD_ANSWERED.format(tool=tool, code=code, expected=expected)
            return texts.GUARD_BROKEN.format(path=worktree, why=why, base=base)
    return None
