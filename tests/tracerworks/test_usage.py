"""`tracerworks usage`: a chantier's cost read from Claude Code's session logs."""

from __future__ import annotations

import json
from pathlib import Path

from tracerworks.usage import chantier_usage, project_dir


def _assistant(mid: str, stamp: str, inp: int, write: int, read: int, out: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": stamp,
            "message": {
                "id": mid,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": write,
                    "cache_read_input_tokens": read,
                    "output_tokens": out,
                },
            },
        }
    )


def _worktree_logs(root: Path, worktree: Path) -> Path:
    folder = project_dir(root, worktree)
    session = folder / "abcdef12-session.jsonl"
    folder.mkdir(parents=True)
    # The same message three times (one line per content block), then another.
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-09-26T10:00:00Z"}),
        _assistant("m1", "2026-09-26T10:00:05Z", 2, 100, 50, 10),
        _assistant("m1", "2026-09-26T10:00:05Z", 2, 100, 50, 10),
        _assistant("m1", "2026-09-26T10:00:06Z", 2, 100, 50, 10),
        _assistant("m2", "2026-09-26T12:10:00Z", 1, 0, 400, 20),
        "{not json — a line being written",
    ]
    session.write_text("\n".join(lines), encoding="utf-8")
    agents = folder / "abcdef12-session" / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-x.jsonl").write_text(
        _assistant("s1", "2026-09-26T11:00:00Z", 5, 300, 0, 40), encoding="utf-8"
    )
    (agents / "agent-x.meta.json").write_text(
        json.dumps({"description": "Implement #578"}), encoding="utf-8"
    )
    return folder


def test_the_slug_is_claude_codes(tmp_path: Path) -> None:
    folder = project_dir(tmp_path, Path("/home/dev/.agent-worktrees/576"))
    assert folder.name == "-home-dev--agent-worktrees-576"


def test_a_repeated_message_is_counted_once_and_subagents_apart(tmp_path: Path) -> None:
    worktree = Path("/home/dev/.agent-worktrees/577")
    _worktree_logs(tmp_path, worktree)

    usage = chantier_usage(tmp_path, worktree)

    assert usage is not None
    owner = usage.owner_tokens()
    assert (owner.input, owner.cache_write, owner.cache_read, owner.output) == (
        3,
        100,
        450,
        30,
    )
    assert owner.messages == 2
    [agent] = usage.subagents
    assert agent.name == "Implement #578"
    assert usage.subagent_tokens().total == 345
    assert usage.total().total == owner.total + 345
    span = usage.span()
    assert span is not None and (span[1] - span[0]).total_seconds() == 2 * 3600 + 600


def test_a_worktree_that_never_ran_claude_has_no_usage(tmp_path: Path) -> None:
    assert chantier_usage(tmp_path, Path("/nowhere/1")) is None
