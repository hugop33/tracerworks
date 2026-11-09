"""`tracerworks mcp`: the door of a remote session (#2186, #2187, spec #2185).

Every test drives the server through its stdio interface — JSON-RPC lines in,
JSON-RPC lines out — against tracerworks's recorded world (`FakeRunner`), never
its insides. The claims: the closed list of tools, the read ones marked read
only and the acting ones not; each tool's output is tracerworks's own (a refusal
word for word, its exit code); the screen and the logs are capped; a message's
text travels on stdin; no tool reaches `--force`, `--over-cap`, `provider`,
`quota` or `clean`; every program runs as an argument list; a bad message
never stops the server.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import TMUX, FakeRunner, recorded_settings, recorded_world
from tests.tracerworks.history_world import history_world
from tests.tracerworks.test_message import (
    HOSTILE,
    Clock,
    dynamic_panes,
    make_env,
    pane,
    world_with_paste,
)
from tests.tracerworks.test_snapshot import github_world
from tests.tracerworks.test_status import NOW
from tracerworks import cli
from tracerworks.cli import EXIT_OK, Env, run

READ_TOOLS = {
    "status",
    "snapshot",
    "history",
    "usage",
    "relauncher_log",
    "serve_log",
    "worker_screen",
    "machine",
    "pr_checks",
}
ACTION_TOOLS = {
    "message",
    "start",
    "start_next",
    "pause",
    "resume",
    "stop",
    "set_aside",
    "self_update",
    "restart_serve",
    "run_workflow",
}


def env_for(fake: FakeRunner, tmp_path: Path) -> Env:
    return Env(
        recorded_settings(tmp_path),
        runner=fake,
        now=lambda: NOW,
        sleep=lambda _: None,
        stdin=io.BytesIO(),
        out=io.StringIO(),
        err=io.StringIO(),
    )


def session(env: Env, *messages: dict[str, Any] | bytes) -> list[dict[str, Any]]:
    """Feed the server these lines on stdin; its answers, one per line."""
    raw = b"".join(
        (m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n"
        for m in messages
    )
    out = io.StringIO()
    env.stdin, env.out = io.BytesIO(raw), out
    assert run(env, ["mcp"]) == EXIT_OK
    return [json.loads(line) for line in out.getvalue().splitlines()]


def request(
    ident: int, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(env: Env, tool: str, **arguments: Any) -> dict[str, Any]:
    [answer] = session(
        env, request(1, "tools/call", {"name": tool, "arguments": arguments})
    )
    assert "result" in answer, answer
    result: dict[str, Any] = answer["result"]
    return result


def text_of(result: dict[str, Any]) -> str:
    [content] = result["content"]
    assert content["type"] == "text"
    text: str = content["text"]
    return text


def direct(env: Env, argv: list[str]) -> tuple[int, str]:
    """The same command run as the CLI runs it: its exit code and its text."""
    out, err = io.StringIO(), io.StringIO()
    code = run(replace(env, out=out, err=err), argv)
    return code, "\n".join(
        p.rstrip("\n") for p in (out.getvalue(), err.getvalue()) if p
    )


# --- the protocol ------------------------------------------------------------


def test_initialize_ping_and_the_closed_list_of_tools(tmp_path: Path) -> None:
    env = env_for(recorded_world(), tmp_path)
    init, pong, listed, old = session(
        env,
        request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request(2, "ping"),
        request(3, "tools/list"),
        request(4, "initialize", {"protocolVersion": "1999-01-01"}),
    )
    assert init["id"] == 1
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert pong == {"jsonrpc": "2.0", "id": 2, "result": {}}
    tools = listed["result"]["tools"]
    assert len(tools) == len(READ_TOOLS) + len(ACTION_TOOLS) == 19
    hints = {t["name"]: t["annotations"]["readOnlyHint"] for t in tools}
    assert {name for name, hint in hints.items() if hint is True} == READ_TOOLS
    assert {name for name, hint in hints.items() if hint is False} == ACTION_TOOLS
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    assert old["result"]["protocolVersion"] == "2025-06-18"  # the newest spoken


def test_a_bad_message_is_answered_and_the_server_goes_on(tmp_path: Path) -> None:
    env = env_for(recorded_world(), tmp_path)
    answers = session(
        env,
        b"{not json",
        b"[1, 2]",
        request(1, "resources/list"),
        request(2, "tools/call", {"name": "clean", "arguments": {}}),
        request(3, "tools/call", {"name": "history", "arguments": {}}),
        request(4, "tools/call", {"name": "history", "arguments": {"spec": "1; rm"}}),
        request(5, "tools/call", {"name": "history", "arguments": {"spec": True}}),
        request(6, "tools/call", {"name": "status", "arguments": {"force": True}}),
        request(7, "tools/call", {"name": "history", "arguments": {"spec": 0}}),
        request(8, "ping"),
    )
    codes = [(a["id"], a.get("error", {}).get("code")) for a in answers]
    assert codes == [
        (None, -32700),
        (None, -32600),
        (1, -32601),
        (2, -32602),
        (3, -32602),
        (4, -32602),
        (5, -32602),
        (6, -32602),
        (7, -32602),
        (8, None),
    ]
    assert "unknown tool: clean" in answers[3]["error"]["message"]
    assert "unknown argument(s): force" in answers[7]["error"]["message"]


# --- tracerworks's own commands -------------------------------------------------


def test_status_is_tracerworks_status_with_its_exit_code(tmp_path: Path) -> None:
    env = env_for(recorded_world(), tmp_path)
    code, expected = direct(env, ["status"])
    assert code == 0 and "#501" in expected
    result = call(env, "status")
    assert result["isError"] is False
    assert text_of(result) == f"{expected}\n[exit 0]"


def test_history_is_tracerworks_history(tmp_path: Path) -> None:
    env = env_for(recorded_world(), tmp_path)
    code, expected = direct(env, ["history", "10"])
    assert code == 0 and expected.startswith("#10: no state recorded yet")
    assert text_of(call(env, "history", spec=10)) == f"{expected}\n[exit 0]"


def test_a_refusal_comes_back_word_for_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    env = env_for(recorded_world(), tmp_path)
    code, refusal = direct(env, ["usage", "777"])
    assert code == 1 and refusal.startswith("tracerworks: refused — no Claude session")
    result = call(env, "usage", spec=777)
    assert result["isError"] is True
    assert text_of(result) == f"{refusal}\n[exit 1]"


def test_snapshot_is_tracerworks_snapshot(tmp_path: Path) -> None:
    """The dashboard's JSON: what `snapshot` prints, nothing in between —
    a tool's own failure would be an error result, not a server crash."""
    env = env_for(history_world(github_world(recorded_world())), tmp_path)
    code, expected = direct(env, ["snapshot"])
    assert code == 0 and json.loads(expected)["workers"]
    result = call(env, "snapshot")
    assert result["isError"] is False
    assert text_of(result) == f"{expected}\n[exit 0]"


def test_a_tool_that_breaks_is_an_error_result_not_a_dead_server(
    tmp_path: Path,
) -> None:
    """A world tracerworks cannot read (here: GitHub answers nothing the fake
    knows) fails that call alone; the next request is still answered."""
    env = env_for(recorded_world(), tmp_path)
    broken, pong = session(
        env,
        request(1, "tools/call", {"name": "snapshot", "arguments": {}}),
        request(2, "ping"),
    )
    assert broken["result"]["isError"] is True
    assert "unexpected command" in text_of(broken["result"])
    assert pong["result"] == {}


# --- the worker's screen -----------------------------------------------------


def test_worker_screen_reads_the_exact_window_without_escapes(tmp_path: Path) -> None:
    fake = recorded_world()
    screen = "\n".join(f"line {i}" for i in range(300)) + "\n\n\n"
    fake.answer(*TMUX, "capture-pane", "-p", "-S", "-200", "-t", "=widgets:=worker-510",
                stdout=screen)  # fmt: skip
    fake.answer(*TMUX, "capture-pane", "-p", "-S", "-3", "-t", "=widgets:=worker-510",
                stdout="a\nb\nc\nd\n")  # fmt: skip
    env = env_for(fake, tmp_path)
    capped = call(env, "worker_screen", spec=510, lines=5000)
    lines = text_of(capped).splitlines()
    assert lines[0] == "line 100" and lines[-2] == "line 299"
    assert lines[-1] == "[exit 0]" and len(lines) == 201
    assert text_of(call(env, "worker_screen", spec=510, lines=3)) == "b\nc\nd\n[exit 0]"
    captures = fake.ran(*TMUX, "capture-pane")
    assert captures and all("-e" not in c.argv for c in captures)


@pytest.mark.parametrize("spec", [520, 50])  # stopped; a prefix of worker-501
def test_worker_screen_refuses_a_spec_without_its_window(
    tmp_path: Path, spec: int
) -> None:
    fake = recorded_world()
    env = env_for(fake, tmp_path)
    result = call(env, "worker_screen", spec=spec)
    assert result["isError"] is True
    assert text_of(result) == (
        f"no window worker-{spec}: the worker is not running\n[exit 1]"
    )
    assert not fake.ran(*TMUX, "capture-pane")


# --- the logs ----------------------------------------------------------------


def test_relauncher_log_gives_its_last_lines_capped(tmp_path: Path) -> None:
    env = env_for(recorded_world(), tmp_path)
    log = tmp_path / "next.log"
    log.write_text("".join(f"pass {i}\n" for i in range(500)), encoding="utf-8")
    env.settings = replace(env.settings, relauncher_log=log)
    lines = text_of(call(env, "relauncher_log", lines=1000)).splitlines()
    assert len(lines) == 201 and lines[0] == "pass 300" and lines[-2] == "pass 499"
    default = text_of(call(env, "relauncher_log")).splitlines()
    assert len(default) == 51 and default[0] == "pass 450"


def test_an_unreadable_log_is_an_error_result(tmp_path: Path) -> None:
    env = env_for(recorded_world(), tmp_path)
    env.settings = replace(env.settings, relauncher_log=tmp_path / "absent.log")
    result = call(env, "relauncher_log")
    assert result["isError"] is True
    assert text_of(result).startswith("relauncher log unreadable:")


def test_serve_log_reads_the_dashboard_units_journal(tmp_path: Path) -> None:
    fake = recorded_world()
    argv = (
        "journalctl", "--user", "--unit", "tracerworks-serve-fixture.service",
        "--lines", "200", "--no-pager", "--output", "short-iso",
    )  # fmt: skip
    fake.answer(*argv, stdout="11:00 serve: base schema 26 > 25, exit 2\n")
    env = env_for(fake, tmp_path)
    result = call(env, "serve_log", lines=999)
    assert text_of(result) == "11:00 serve: base schema 26 > 25, exit 2\n[exit 0]"
    assert [c.argv for c in fake.ran("journalctl")] == [argv]


# --- the machine and the checks ----------------------------------------------

DF = (
    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
    "/dev/sda1 100000000 77000000 23000000 77% /\n"
)
SYSTEMCTL = (
    "Id=serve-fixture.service\nLoadState=loaded\nActiveState=active\n"
    "SubState=running\n\n"
    "Id=next-fixture.timer\nLoadState=loaded\nActiveState=active\nSubState=waiting\n\n"
    "Id=gone-fixture.service\nLoadState=not-found\nActiveState=inactive\n"
    "SubState=dead\n"
)
PS = "  4242 2306048  7384 claude\n   101  512000    59 node\ngarbage line\n"


def test_machine_reads_memory_disk_health_units_and_processes(tmp_path: Path) -> None:
    fake = recorded_world()
    env = env_for(fake, tmp_path)
    units = ("serve-fixture.service", "next-fixture.timer", "gone-fixture.service")
    health = tmp_path / "health.log"
    health.write_text("10:45 mem_dispo=8128MB\n11:00 mem_dispo=8214MB\n")
    env.settings = replace(env.settings, mcp_units=units, health_log=health)
    fake.answer("df", "-Pk", str(env.settings.worktree_root), stdout=DF)
    fake.answer(
        "systemctl", "--user", "show", "--property",
        "Id,LoadState,ActiveState,SubState", "--", *units, stdout=SYSTEMCTL,
    )  # fmt: skip
    fake.answer(
        "ps", "--user", str(os.getuid()), "--sort=-rss",
        "--format", "pid=,rss=,etimes=,comm=", stdout=PS,
    )  # fmt: skip
    result = call(env, "machine")
    assert result["isError"] is False
    assert text_of(result).splitlines() == [
        "memory available: 2252 MB",
        f"disk: 77 % used, 21.9 GB available ({env.settings.worktree_root})",
        "health log, last line: 11:00 mem_dispo=8214MB",
        "units:",
        "  serve-fixture.service: active (running)",
        "  next-fixture.timer: active (waiting)",
        "  gone-fixture.service: not-found",
        "largest processes (resident memory, top 10):",
        "      4242    2252 MB  2h03  claude",
        "       101     500 MB  0h00  node",
        "[exit 0]",
    ]


def test_machine_says_what_it_cannot_read(tmp_path: Path) -> None:
    fake = recorded_world()
    env = env_for(fake, tmp_path)
    env.settings = replace(
        env.settings,
        meminfo=tmp_path / "no-meminfo",
        health_log=tmp_path / "no-health.log",
    )
    fake.answer("df", "-Pk", prefix=True, returncode=1)
    fake.answer("ps", prefix=True, returncode=1, stdout="ps: boom")
    text = text_of(call(env, "machine"))
    assert "memory available: unreadable" in text
    assert "disk: unreadable" in text
    assert "health log unreadable:" in text
    assert "units: none configured ([mcp] units)" in text
    assert "processes unreadable: ps: boom" in text


@pytest.mark.parametrize(
    ("code", "is_error"), [(0, False), (8, False), (1, False), (4, True)]
)
def test_pr_checks_are_gh_pr_checks_on_the_project_repo(
    tmp_path: Path, code: int, is_error: bool
) -> None:
    fake = recorded_world()
    argv = ("gh", "pr", "checks", "812", "--repo", "owner/repo")
    fake.answer(*argv, stdout="checks (fast)\tpass\t1m\n", returncode=code)
    env = env_for(fake, tmp_path)
    result = call(env, "pr_checks", pr=812)
    assert result["isError"] is is_error
    assert text_of(result).startswith("checks (fast)\tpass\t1m\n")
    assert text_of(result).endswith(f"[exit {code}]")
    assert [c.argv for c in fake.ran("gh", "pr", "checks")] == [argv]


def test_no_tool_runs_a_shell(tmp_path: Path) -> None:
    """Every program the door runs is an argument list (#514): none is a
    shell given a line to parse."""
    fake = recorded_world()
    fake.answer("df", "-Pk", prefix=True, returncode=1)
    fake.answer("ps", prefix=True, returncode=1)
    fake.answer("journalctl", prefix=True)
    fake.answer("gh", "pr", "checks", prefix=True)
    env = env_for(fake, tmp_path)
    for tool, arguments in [
        ("status", {}),
        ("worker_screen", {"spec": 501}),
        ("serve_log", {}),
        ("machine", {}),
        ("pr_checks", {"pr": 1}),
    ]:
        call(env, tool, **arguments)
    assert fake.calls
    assert not [
        c for c in fake.calls if Path(c.argv[0]).name in {"sh", "bash", "dash", "zsh"}
    ]


# --- the acting tools --------------------------------------------------------

#: What may never be reached through the door, whatever a tool is given.
FORBIDDEN_COMMANDS = {"provider", "quota", "clean", "db-backup", "alert-test", "serve"}
FORBIDDEN_FLAGS = ("force", "over_cap", "over_limit", "wait_idle", "file")


def recording(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, bytes]]:
    """Every tracerworks command the door runs, as tracerworks's own parser read
    its argument list, with the stdin it was given; `mcp` itself runs."""
    real = cli.dispatch
    seen: list[tuple[Any, bytes]] = []

    def dispatch(env: Env, args: Any) -> int:
        if args.command == "mcp":
            return real(env, args)
        seen.append((args, env.stdin.read()))
        env.say(f"{args.command} done")
        return EXIT_OK

    monkeypatch.setattr(cli, "dispatch", dispatch)
    return seen


@pytest.mark.parametrize(
    ("tool", "arguments", "command", "spec", "stdin"),
    [
        ("message", {"spec": 510, "text": "Pousse ton travail é."}, "message", 510,
         "Pousse ton travail é.".encode()),
        ("start", {"spec": 2185}, "start", 2185, b""),
        ("start_next", {}, "start-next", None, b""),
        ("pause", {"spec": 501}, "pause", 501, b""),
        ("resume", {"spec": 501}, "resume", 501, b""),
        ("stop", {"spec": 501}, "stop", 501, b""),
        ("set_aside", {"spec": 501}, "set-aside", 501, b""),
        ("self_update", {}, "self-update", None, b""),
        ("status", {}, "status", None, b""),
        ("history", {"spec": 9}, "history", 9, b""),
        ("usage", {"spec": 9}, "usage", 9, b""),
        ("snapshot", {}, "snapshot", None, b""),
    ],
)  # fmt: skip
def test_each_tool_runs_its_tracerworks_command_and_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    command: str,
    spec: int | None,
    stdin: bytes,
) -> None:
    seen = recording(monkeypatch)
    result = call(env_for(recorded_world(), tmp_path), tool, **arguments)
    assert text_of(result) == f"{command} done\n[exit 0]"
    [(args, given)] = seen
    assert args.command == command
    assert getattr(args, "spec", None) == spec
    assert given == stdin
    assert not [flag for flag in FORBIDDEN_FLAGS if getattr(args, flag, None)]


def test_a_hostile_message_reaches_the_worker_on_stdin_only(tmp_path: Path) -> None:
    """#514: quotes, `$(…)` and backticks are bytes on stdin, never words."""
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [None])
    env, _, _ = make_env(fake, tmp_path, Clock())
    result = call(env, "message", spec=510, text=HOSTILE)
    assert result["isError"] is False
    assert "message received by worker-510" in text_of(result)
    [load] = fake.ran(*TMUX, "load-buffer")
    assert load.stdin is not None
    assert load.stdin.decode("utf-8").endswith(HOSTILE.strip())
    for made in fake.calls:
        assert not any("pwned" in arg or "rm -rf" in arg for arg in made.argv)


@pytest.mark.parametrize(
    ("tool", "arguments", "argv", "stdin"),
    [
        ("message", {"spec": 520, "text": "hello"}, ["message", "520"], b"hello"),
        ("stop", {"spec": 999}, ["stop", "999"], b""),
        ("pause", {"spec": 520}, ["pause", "520"], b""),
    ],
)
def test_a_refusal_of_an_action_comes_back_word_for_word(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, Any],
    argv: list[str],
    stdin: bytes,
) -> None:
    env = env_for(recorded_world(), tmp_path)
    code, refusal = direct(replace(env, stdin=io.BytesIO(stdin)), argv)
    assert code == 1 and refusal.startswith("tracerworks: refused — ")
    result = call(env, tool, **arguments)
    assert result["isError"] is True
    assert text_of(result) == f"{refusal}\n[exit 1]"


def test_pause_sends_escape_to_the_exact_window(tmp_path: Path) -> None:
    fake = recorded_world()
    fake.answer(*TMUX, "send-keys", prefix=True)
    result = call(env_for(fake, tmp_path), "pause", spec=501)
    assert text_of(result).startswith("worker-501 interrupted (Escape)")
    [keys] = fake.ran(*TMUX, "send-keys")
    assert keys.argv == (*TMUX, "send-keys", "-t", "=widgets:=worker-501", "Escape")


def test_restart_serve_restarts_the_dashboard_unit(tmp_path: Path) -> None:
    fake = recorded_world()
    argv = ("systemctl", "--user", "restart", "tracerworks-serve-fixture.service")
    fake.answer(*argv)
    result = call(env_for(fake, tmp_path), "restart_serve")
    assert result == {"content": [{"type": "text", "text": "[exit 0]"}],
                      "isError": False}  # fmt: skip
    assert [c.argv for c in fake.ran("systemctl")] == [argv]


def test_run_workflow_runs_a_listed_workflow_on_the_base_branch(
    tmp_path: Path,
) -> None:
    fake = recorded_world()
    env = env_for(fake, tmp_path)
    env.settings = replace(env.settings, mcp_workflows=("deploy.yml", "ci.yml"))
    argv = ("gh", "workflow", "run", "deploy.yml", "--repo", "owner/repo",
            "--ref", env.settings.base_branch)  # fmt: skip
    fake.answer(*argv, stdout="Created workflow_dispatch event for deploy.yml\n")
    result = call(env, "run_workflow", name="deploy.yml")
    assert result["isError"] is False
    assert [c.argv for c in fake.ran("gh", "workflow")] == [argv]
    assert env.settings.base_branch == "main"


@pytest.mark.parametrize(
    "name", ["evil.yml", "deploy.yml --ref feature", "--ref=feature", "../ci.yml"]
)
def test_run_workflow_refuses_a_workflow_off_the_list(
    tmp_path: Path, name: str
) -> None:
    fake = recorded_world()
    env = env_for(fake, tmp_path)
    env.settings = replace(env.settings, mcp_workflows=("deploy.yml", "ci.yml"))
    result = call(env, "run_workflow", name=name)
    assert result["isError"] is True
    assert text_of(result).startswith(f"workflow {name!r} is not one the door runs;")
    assert not fake.ran("gh", "workflow")


def test_run_workflow_takes_no_other_ref(tmp_path: Path) -> None:
    fake = recorded_world()
    env = env_for(fake, tmp_path)
    env.settings = replace(env.settings, mcp_workflows=("deploy.yml",))
    [answer] = session(
        env,
        request(1, "tools/call", {"name": "run_workflow",
                                  "arguments": {"name": "deploy.yml", "ref": "dev"}}),
    )  # fmt: skip
    assert answer["error"]["code"] == -32602
    assert not fake.ran("gh", "workflow")


HOSTILE_VALUES: list[Any] = [
    "--force", "--over-cap", "provider claude", "quota 10", "clean", "-1",
    "2185 --force", 0, -1, True, 2.5, None, ["--force"], {"force": True},
]  # fmt: skip


def test_no_argument_of_any_tool_reaches_a_forbidden_flag_or_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tool, every argument it names plus `force`, `over_cap`, `ref`,
    given each hostile value: tracerworks never parses a forbidden flag or
    command, and no program receives one."""
    seen = recording(monkeypatch)
    fake = recorded_world()
    fake.answer("gh", prefix=True)
    fake.answer("systemctl", prefix=True)
    fake.answer("journalctl", prefix=True)
    fake.answer("df", prefix=True, returncode=1)
    fake.answer("ps", prefix=True, returncode=1)
    env = env_for(fake, tmp_path)
    env.settings = replace(env.settings, mcp_workflows=("deploy.yml",))
    [listed] = session(env, request(1, "tools/list"))
    valid = {"spec": 501, "pr": 1, "lines": 5, "text": "hi", "name": "deploy.yml"}
    messages = [
        request(
            1,
            "tools/call",
            {
                "name": tool["name"],
                "arguments": {
                    **{k: valid[k] for k in tool["inputSchema"]["properties"]},
                    key: value,
                },
            },
        )
        for tool in listed["result"]["tools"]
        for key in [*tool["inputSchema"]["properties"], "force", "over_cap", "ref"]
        for value in HOSTILE_VALUES
    ] + [
        request(
            1,
            "tools/call",
            {
                "name": tool["name"],
                "arguments": {k: valid[k] for k in tool["inputSchema"]["properties"]},
            },
        )
        for tool in listed["result"]["tools"]
    ] + [
        request(0, "tools/call", {"name": name, "arguments": {}})
        for name in ["provider", "quota", "clean", "db-backup", "kill", "send_keys"]
    ]  # fmt: skip
    session(env, *messages)
    assert seen  # the door did run commands (`--force` as a message's text)
    for args, _ in seen:
        assert args.command not in FORBIDDEN_COMMANDS
        assert not [flag for flag in FORBIDDEN_FLAGS if getattr(args, flag, None)]
    for made in fake.calls:
        assert not {"--force", "--over-cap", "--over-limit"} & set(made.argv)
        assert (
            made.argv[:3] != ("gh", "workflow", "run") or made.argv[3] == "deploy.yml"
        )
