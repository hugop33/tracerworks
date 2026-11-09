"""The session guard protects its own files (#1529, ticket #1534).

A session could switch its guard off by editing its wiring, its rulebook or
its code: the rulebook names them, and any write naming one is refused to
the session it guards — the forms of the V1 rule. Changing the guard is the
operator's gesture: outside a session, or in one started with the escape.
The escape's name never enters a Claude Code settings file, escape or not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from session_guard import ESCAPE, Rules, refusal
from tests.test_session_guard import SYNTHETIC, bash, guard_tree, hook, tool

PROJECT = Path("/home/dev/project")
GUARDED = Rules(
    read_only_dirs=("legacy-repo",),
    read_only_shops=("prod-shop.example.com",),
    forbidden_tool_prefixes=("mcp__claude_ai_",),
    protected_paths=(
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/session_guard.json",
        "tools/session_guard",
    ),
)


def at_root(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "cwd": str(PROJECT)}


SELF_REFUSED = [
    tool("Edit", file_path=str(PROJECT / ".claude/settings.json"), new_string="{}"),
    tool("Write", file_path=".claude/session_guard.json", content="{}"),
    tool("Write", file_path="tools/session_guard/__init__.py", content=""),
    {"tool_name": "MultiEdit", "tool_input": {
        "file_path": ".claude/settings.local.json", "edits": []}},
    tool("NotebookEdit", notebook_path="tools/session_guard/x.ipynb"),
    bash("echo '{}' > .claude/session_guard.json"),
    bash("printf x | tee tools/session_guard/__main__.py"),
    bash("sed -i 's/2/0/' tools/session_guard/__main__.py"),
    bash("rm -rf tools/session_guard"),
    bash("mv .claude/settings.json /tmp/s.json"),
    bash("cp /tmp/empty.json .claude/settings.local.json"),
    bash("git checkout -- .claude/settings.json"),
    bash("git restore tools/session_guard/__init__.py"),
    bash("cd tools && rm -rf session_guard"),
    bash("cd tools/session_guard && echo x > __init__.py"),
    bash(f"echo x > {PROJECT}/.claude/session_guard.json"),
    # a command that sets the escape before writing opens nothing
    bash(f"export {ESCAPE}=1 && echo '{{}}' > .claude/session_guard.json"),
]  # fmt: skip

SELF_ALLOWED = [
    tool("Read", file_path=".claude/settings.json"),
    bash("cat .claude/settings.json tools/session_guard/__init__.py"),
    bash("cp .claude/session_guard.json /tmp/rulebook.json"),
    bash("git merge origin/main"),
    bash("git pull --ff-only"),
    bash("git add -A && git commit -m wip"),
    bash("git diff -- .claude/settings.json && git log -3 -- tools/session_guard"),
    tool("Write", file_path=".claude/settings.json.example", content="{}"),
    tool("Edit", file_path="tools/session_guard_notes.md", new_string="x"),
    bash("echo x > tools/tracerworks/notes.txt"),
    bash("cd /elsewhere && echo x > .claude/settings.json"),
]


def _id(payload: dict[str, Any]) -> str:
    return str(payload["tool_input"] or payload["tool_name"])


@pytest.mark.parametrize("payload", SELF_REFUSED, ids=_id)
def test_the_session_it_guards_cannot_write_the_guard(payload: dict[str, Any]) -> None:
    reason = refusal(at_root(payload), GUARDED, root=PROJECT)
    assert reason is not None
    assert "geste de l'opérateur" in reason and ESCAPE in reason


@pytest.mark.parametrize("payload", SELF_ALLOWED, ids=_id)
def test_reading_the_guard_and_merging_main_stay_allowed(
    payload: dict[str, Any],
) -> None:
    assert refusal(at_root(payload), GUARDED, root=PROJECT) is None


@pytest.mark.parametrize("payload", SELF_REFUSED, ids=_id)
def test_with_the_escape_the_operator_writes_the_guard(payload: dict[str, Any]) -> None:
    assert refusal(at_root(payload), GUARDED, root=PROJECT, escape=True) is None


@pytest.mark.parametrize("escape", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        tool(
            "Edit",
            file_path=str(PROJECT / ".claude/settings.json"),
            new_string=f'"env": {{"{ESCAPE}": "1"}}',
        ),
        tool(
            "Write",
            file_path="/home/dev/.claude/settings.json",
            content=f'{{"env": {{"{ESCAPE}": "1"}}}}',
        ),
        {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "/home/dev/.claude/settings.local.json",
                "edits": [{"old_string": "a", "new_string": ESCAPE}],
            },
        },
        bash(f"""jq '.env.{ESCAPE}="1"' ~/.claude/settings.json > /tmp/s"""),
        bash(f"echo {ESCAPE} >> .claude/settings.local.json"),
    ],
    ids=["project", "user", "local", "bash user", "bash local"],
)
def test_the_escape_s_name_never_enters_a_claude_settings_file(
    payload: dict[str, Any], escape: bool
) -> None:
    reason = refusal(at_root(payload), GUARDED, root=PROJECT, escape=escape)
    assert reason is not None and "réglages de Claude Code" in reason


def test_the_escape_s_name_in_a_document_passes() -> None:
    for payload in (
        tool("Write", file_path="docs/RUNBOOK.md", content=f"{ESCAPE}=1 claude"),
        bash(f"grep -rn {ESCAPE} docs"),
        tool("Write", file_path="/home/dev/.claude/settings.json", content="{}"),
    ):
        assert refusal(at_root(payload), GUARDED, root=PROJECT) is None, payload


def test_the_hook_process_refuses_a_protected_write_and_the_escape_opens_it(
    tmp_path: Path,
) -> None:
    tools = guard_tree(tmp_path, SYNTHETIC)
    project = tools.parent
    write = {
        **tool("Write", file_path=".claude/settings.json", content="{}"),
        "cwd": str(project),
    }
    refused = hook(write, tools)
    assert refused.returncode == 2 and "geste de l'opérateur" in refused.stderr
    assert hook(write, tools, escape=True).returncode == 0
    sets = bash(f"{ESCAPE}=1 rm -rf tools/session_guard", cwd=str(project))
    assert hook(sets, tools).returncode == 2
