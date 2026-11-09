"""`python -m session_guard`: the PreToolUse hook entry point.

Claude Code sends the tool call as JSON on stdin. Exit 2 refuses it and hands
stderr to the agent as the reason; exit 0 lets it run.

IT FAILS CLOSED (#1529). Until then a payload or a rulebook it could not read
exited 1, which Claude Code shows and lets run: a broken guard was loud and
OPEN, and a session in auto mode went on unguarded. Now anything that keeps
it from judging — a rulebook absent, unreadable or incomplete, a payload of
an unexpected shape, any exception — refuses the call (exit 2) and says what
to repair. The hook's command in `.claude/settings.json` does the same for
what happens before this code runs (python missing, the project's folder
unknown, a crash).

The operator's escape (`ESCAPE`, read in this process's own environment):
a failure then lets the call run with a visible error (exit 1) — the way to
repair a guard that freezes every call. Once the rulebook reads, the V1,
shop and claude.ai rules hold with or without it.
"""

from __future__ import annotations

import json
import sys

from session_guard import ESCAPE, escaped, load_rules, refusal

REPAIR = (
    "réparer le garde-fou (son règlement .claude/session_guard.json ou son code"
    " tools/session_guard), ou, pour l'opérateur seulement, relancer la session"
    f" avec {ESCAPE}=1 (RUNBOOK_VPS_DEV §10)"
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        rules = load_rules()
        reason = refusal(payload, rules, escape=escaped())
    except Exception as exc:  # anything that keeps it from judging
        what = f"{type(exc).__name__}: {exc}"
        if escaped():
            print(
                f"session_guard : ne peut pas juger ({what}) — appel laissé passer,"
                f" échappée {ESCAPE} posée par l'opérateur",
                file=sys.stderr,
            )
            return 1
        print(
            f"session_guard : ne peut pas juger ({what}) — appel refusé ; {REPAIR}.",
            file=sys.stderr,
        )
        return 2
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
