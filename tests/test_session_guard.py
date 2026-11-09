"""The session guard refuses what CLAUDE.md forbids, and only that.

Its refusals are the point; its silences matter as much. A hook that blocks a
read of the V1 repository, or a GraphQL query to the shop, gets switched off
on its first false positive, and the rule goes back to being a sentence.

The names are synthetic here: the real ones live in the operator's rulebook
(`.claude/session_guard.json`), never in code (D12). The tests that pin this
repository's wiring — the rulebook, the hook and the permissions shipped
together — are in `test_session_guard_project.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import session_guard
from session_guard import (
    ESCAPE,
    PayloadError,
    RulebookError,
    Rules,
    load_rules,
    refusal,
)

RULES = Rules(
    read_only_dirs=("legacy-repo",),
    read_only_shops=("prod-shop.example.com",),
    forbidden_tool_prefixes=("mcp__claude_ai_",),
)
GRAPHQL = "https://prod-shop.example.com/admin/api/2025-01/graphql.json"
REST = "https://prod-shop.example.com/admin/api/2025-01/orders"


def bash(command: str, cwd: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def tool(name: str, **args: str) -> dict[str, Any]:
    return {"tool_name": name, "tool_input": args}


REFUSED = [
    tool("mcp__claude_ai_Gmail__search_threads"),
    tool("Edit", file_path="/home/dev/legacy-repo/src/a.ts"),
    tool("Write", file_path="../legacy-repo/notes.md"),
    tool("NotebookEdit", notebook_path="/x/legacy-repo/n.ipynb"),
    bash("echo x > ../legacy-repo/a.txt"),
    bash("printf x | tee -a /home/dev/legacy-repo/log"),
    bash("sed -i 's/a/b/' ../legacy-repo/src/a.ts"),
    bash("rm -rf ../legacy-repo/node_modules"),
    bash("mv ../legacy-repo/a ./a"),
    bash("cp notes.md ../legacy-repo/docs/"),
    bash("git -C ../legacy-repo commit -am wip"),
    bash("cd ../legacy-repo && git push"),
    bash("cd /home/dev/legacy-repo; echo x > out.txt"),
    bash(f"""curl -X POST {GRAPHQL} -d '{{"query":"mutation {{ orderUpdate }}"}}'"""),
    bash(f"curl -X PUT {REST}/1.json"),
    bash(f"""curl {REST}.json -d '{{"order":{{}}}}'"""),
    # #1529: the session's directory, from the payload, and what moves it
    bash("echo x > notes.txt", cwd="/home/dev/legacy-repo"),
    bash("rm -rf node_modules", cwd="/home/dev/legacy-repo/front"),
    {**tool("Write", file_path="src/a.ts"), "cwd": "/home/dev/legacy-repo"},
    bash("pushd ../legacy-repo && echo x > out.txt"),
    bash("(cd ../legacy-repo && git reset --hard)"),
    bash("sleep 1 & rm -rf ../legacy-repo/x"),
    bash("env FOO=1 rm -rf ../legacy-repo/x"),
    # the shop as a host, whatever its case; every body option of curl
    bash(
        f"curl -X POST {GRAPHQL.replace('prod-shop', 'Prod-Shop')}"
        " -d 'mutation { x }'"
    ),
    bash("curl https://PROD-SHOP.EXAMPLE.COM/admin/api/orders/1.json -X DELETE"),
    bash(f"curl {REST}.json --data-urlencode 'note=x'"),
    bash("curl prod-shop.example.com/admin/api/orders.json --json '{}'"),
]

ALLOWED = [
    tool("Read", file_path="/home/dev/legacy-repo/a.ts"),
    tool("Edit", file_path="/home/dev/widgets/docs/legacy-repo-notes.md"),
    tool("mcp__database__list_tables"),
    bash("grep -rn order ../legacy-repo/src | head"),
    bash("cat ../legacy-repo/a.ts > /tmp/a.ts"),
    bash("cp ../legacy-repo/a.ts ./a.ts"),
    bash("git -C ../legacy-repo log --oneline -5"),
    bash("cd ../legacy-repo && git log -1 2>/dev/null"),
    bash("cd ../legacy-repo && git pull --ff-only"),
    bash("cd ../legacy-repo && ls; cd ../widgets && echo x > notes.txt"),
    bash(f"""curl -X POST {GRAPHQL} -d '{{"query":"{{ shop {{ name }} }}"}}'"""),
    bash(f"curl {REST}.json"),
    bash("curl -X POST https://test-shop.example.com/graphql.json -d 'mutation { x }'"),
    # #1529: a redirection's `&` separates nothing
    bash("cd ../legacy-repo && git log -1 2>&1 | head"),
    bash("ls ../legacy-repo >&2 && cat ../legacy-repo/a &> /tmp/x"),
    # the audit's false positive: a document citing the shop next to `--data`
    bash(
        "cat > docs/note.md <<'EOF'\n"
        "Le hook refusait `curl https://prod-shop.example.com/admin --data x`.\n"
        "EOF"
    ),
    bash(
        "cat > docs/note.md <<EOF\nprod-shop.example.com -X POST --data x\nEOF\n"
        "git add docs/note.md"
    ),
    # the domain cited, then a body sent elsewhere
    bash("echo prod-shop.example.com; curl -d x https://example.com/hook"),
    bash("grep -rn prod-shop.example.com docs | head && curl -d x https://x.io"),
    # reads in the V1, from a directory in it
    bash("git log --oneline -3 && cat src/a.ts", cwd="/home/dev/legacy-repo"),
    bash("ls; cd /home/dev/widgets && echo x > notes.txt", cwd="/home/dev/legacy-repo"),
    {**tool("Write", file_path="docs/a.md"), "cwd": "/home/dev/widgets"},
]


@pytest.mark.parametrize(
    "payload", REFUSED, ids=lambda p: str(p["tool_input"] or p["tool_name"])
)
def test_it_refuses_what_claude_md_forbids(payload: dict[str, Any]) -> None:
    assert refusal(payload, RULES)


@pytest.mark.parametrize(
    "payload", ALLOWED, ids=lambda p: str(p["tool_input"] or p["tool_name"])
)
def test_it_lets_reads_and_other_targets_through(payload: dict[str, Any]) -> None:
    assert refusal(payload, RULES) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_input": {"command": "ls"}},
        {"tool_name": "", "tool_input": {}},
        {"tool_name": "Bash", "tool_input": ["rm", "-rf"]},
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": {"command": ["rm"]}},
        ["not", "an", "object"],
    ],
    ids=["no tool", "empty tool", "input a list", "no input", "command a list", "list"],
)
def test_a_payload_of_an_unexpected_shape_cannot_be_judged(payload: Any) -> None:
    with pytest.raises(PayloadError):
        refusal(payload, RULES)


def _book(**data: object) -> str:
    full: dict[str, object] = {
        "read_only_dirs": ["a"],
        "read_only_shops": ["b"],
        "forbidden_tool_prefixes": ["c"],
        "protected_paths": ["d"],
    }
    full.update(data)
    return json.dumps({k: v for k, v in full.items() if v is not None})


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{", "JSON invalide"),
        ("[]", "un objet JSON est attendu"),
        (_book(forbidden_tool_prefixes=None), "forbidden_tool_prefixes"),
        (_book(read_only_dirs=[]), "read_only_dirs"),
        (_book(read_only_dirs="a"), "read_only_dirs"),
        (_book(extra=[]), "inconnue"),
        # #1529: the guard's own files — required, relative to the root
        (_book(protected_paths=None), "protected_paths"),
        (_book(protected_paths=["/etc/x"]), "relatif à la racine"),
        (_book(protected_paths=["../x"]), "relatif à la racine"),
    ],
)
def test_a_broken_rulebook_is_refused_by_its_loader(
    tmp_path: Path, content: str, why: str
) -> None:
    path = tmp_path / "session_guard.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RulebookError, match=why):
        load_rules(path)
    with pytest.raises(RulebookError, match="illisible"):
        load_rules(tmp_path / "absent.json")


# --- the real process: codes, failing closed, the escape ---------------------------


def guard_tree(tmp_path: Path, rulebook: str | None) -> Path:
    """A copy of the guard with a rulebook of the test's own (None: none)."""
    import shutil

    tools = tmp_path / "project" / "tools"
    shutil.copytree(
        Path(session_guard.__file__).parent,
        tools / "session_guard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    claude = tmp_path / "project" / ".claude"
    claude.mkdir()
    if rulebook is not None:
        (claude / "session_guard.json").write_text(rulebook, encoding="utf-8")
    return tools


SYNTHETIC = json.dumps(
    {
        "read_only_dirs": ["legacy-repo"],
        "read_only_shops": ["prod-shop.example.com"],
        "forbidden_tool_prefixes": ["mcp__claude_ai_"],
        "protected_paths": [".claude/settings.json", "tools/session_guard"],
    }
)


def hook(
    payload: Any, tools: Path, escape: bool = False
) -> subprocess.CompletedProcess[str]:
    import os

    environ = {k: v for k, v in os.environ.items() if k != ESCAPE}
    if escape:
        environ[ESCAPE] = "1"
    return subprocess.run(
        [sys.executable, "-m", "session_guard"],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=tools,
        env=environ,
        check=False,
    )


def test_the_hook_exits_2_with_its_reason(tmp_path: Path) -> None:
    run = hook(
        tool("mcp__claude_ai_Gmail__send_message"), guard_tree(tmp_path, SYNTHETIC)
    )
    assert run.returncode == 2
    assert "claude.ai" in run.stderr


@pytest.mark.parametrize(
    ("rulebook", "payload"),
    [
        ("{", bash("ls")),
        (None, bash("ls")),
        ('{"read_only_dirs": ["a"]}', bash("ls")),
        (SYNTHETIC, {"tool_name": "Bash", "tool_input": ["ls"]}),
        (SYNTHETIC, {"tool_input": {"command": "ls"}}),
        (SYNTHETIC, "not json"),
    ],
    ids=["rulebook not JSON", "no rulebook", "key missing", "input a list",
         "no tool name", "payload not JSON"],
)  # fmt: skip
def test_a_guard_that_cannot_judge_refuses_and_says_why(
    tmp_path: Path, rulebook: str | None, payload: Any
) -> None:
    """#1529: it exited 1 — Claude Code shows the error and RUNS the call."""
    run = hook(payload, guard_tree(tmp_path, rulebook))
    assert run.returncode == 2
    assert "ne peut pas juger" in run.stderr and "appel refusé" in run.stderr
    assert ESCAPE in run.stderr  # what the operator can do


def test_with_the_escape_a_guard_that_cannot_judge_lets_the_call_through_said(
    tmp_path: Path,
) -> None:
    run = hook(bash("ls"), guard_tree(tmp_path, "{"), escape=True)
    assert run.returncode == 1
    assert "appel laissé passer" in run.stderr and ESCAPE in run.stderr


@pytest.mark.parametrize(
    "payload",
    [
        bash("echo x > ../legacy-repo/a.txt"),
        bash(f"curl -X PUT {REST}/1.json"),
        tool("mcp__claude_ai_Gmail__send_message"),
    ],
    ids=["V1", "shop", "claude.ai"],
)
def test_the_escape_never_opens_the_absolute_guard_rails(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    run = hook(payload, guard_tree(tmp_path, SYNTHETIC), escape=True)
    assert run.returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        f"export {ESCAPE}=1 && ls",
        f"{ESCAPE}=1 ls",
        f"env {ESCAPE}=1 ls",
    ],
)
def test_a_command_that_sets_the_escape_opens_nothing(
    tmp_path: Path, command: str
) -> None:
    """The hook reads its OWN environment, never the payload."""
    run = hook(bash(command), guard_tree(tmp_path, "{"))
    assert run.returncode == 2
