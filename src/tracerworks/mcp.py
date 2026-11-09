"""`tracerworks mcp`: a narrow door for a remote session, over MCP on stdio (#2185).

A remote session (Codex, driven from a phone) runs in a sandbox: no network,
no tmux socket, no `gh`, no history base. This server runs outside it, with
the operator's rights, and offers a CLOSED list of tools — nothing else of
the machine. Each tool goes through tracerworks's own commands (in the same
process) or a program with an argument list (`Runner`): never a shell line.

The protocol is JSON-RPC 2.0, one message per line (MCP's stdio transport):
`initialize`, `ping`, `tools/list`, `tools/call`; notifications are ignored.
Standard library only, like the rest of tracerworks. A refusal of tracerworks
comes back word for word, with its exit code; nothing is retried.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from tracerworks.disk import KB_PER_GB, KB_PER_MB, filesystem
from tracerworks.observe import available_memory_mb
from tracerworks.process import CommandError, Result, Runner
from tracerworks.runtime.tmux import Tmux
from tracerworks.settings import Settings

#: The versions of MCP this server speaks, the newest first: a client asking
#: another one is answered with the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "tracerworks"

#: The most lines a log or a screen tool returns; above, capped.
MAX_LINES = 200
DEFAULT_LINES = 50
#: The largest processes `machine` lists.
TOP_PROCESSES = 10
#: The longest message the door hands a worker, in characters.
MAX_TEXT = 20_000

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


@dataclass(frozen=True)
class Outcome:
    """What a tool hands back: the text a human reads, and its exit code."""

    code: int
    text: str
    #: exit codes that are an answer, not a failure (`gh pr checks`: 8 pending)
    answers: frozenset[int] = frozenset({0})

    @property
    def failed(self) -> bool:
        return self.code not in self.answers


#: One tracerworks command, run in this process: its argument list and its
#: stdin (a message's text: never an argument) → its outcome.
Command = Callable[[Sequence[str], bytes | None], Outcome]


@dataclass(frozen=True)
class Door:
    settings: Settings
    runner: Runner
    tmux: Tmux
    tracerworks: Command
    uid: int


class InvalidParams(ValueError):
    """A tool called with arguments outside its schema."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[[Door, Mapping[str, Any]], Outcome]
    #: argument name → (JSON schema, required)
    params: Mapping[str, tuple[dict[str, Any], bool]] = field(default_factory=dict)
    read_only: bool = True

    def listed(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": {k: v[0] for k, v in self.params.items()},
                "required": [k for k, v in self.params.items() if v[1]],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": self.read_only},
        }

    def checked(self, arguments: Any) -> Mapping[str, Any]:
        if not isinstance(arguments, dict):
            raise InvalidParams("arguments must be an object")
        unknown = set(arguments) - set(self.params)
        if unknown:
            raise InvalidParams(f"unknown argument(s): {', '.join(sorted(unknown))}")
        for name, (_, required) in self.params.items():
            if name not in arguments:
                if required:
                    raise InvalidParams(f"missing argument: {name}")
                continue
            value = arguments[name]
            if self.params[name][0]["type"] == "string":
                if not isinstance(value, str) or not value.strip():
                    raise InvalidParams(f"{name} must be a non-empty string")
                if len(value) > MAX_TEXT:
                    raise InvalidParams(f"{name} is longer than {MAX_TEXT} characters")
            elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise InvalidParams(f"{name} must be a whole number from 1 on")
        return arguments


SPEC = ({"type": "integer", "minimum": 1, "description": "number of the spec"}, True)
PULL = ({"type": "integer", "minimum": 1, "description": "number of the PR"}, True)
TEXT = (
    {
        "type": "string",
        "maxLength": MAX_TEXT,
        "description": "the message, as the worker will read it",
    },
    True,
)
WORKFLOW = (
    {
        "type": "string",
        "description": "the workflow's file, one of [mcp] workflows of the project",
    },
    True,
)
LINES = (
    {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_LINES,
        "description": f"how many last lines (default {DEFAULT_LINES},"
        f" at most {MAX_LINES})",
    },
    False,
)


def lines_of(arguments: Mapping[str, Any]) -> int:
    return min(int(arguments.get("lines", DEFAULT_LINES)), MAX_LINES)


def last_lines(text: str, count: int) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-count:])


def of_result(result: Result, answers: frozenset[int] = frozenset({0})) -> Outcome:
    text = "\n".join(
        part.rstrip("\n") for part in (result.stdout, result.stderr) if part
    )
    return Outcome(result.returncode, text, answers)


# The tools — read only.


def _plain(command: str) -> Callable[[Door, Mapping[str, Any]], Outcome]:
    """An tracerworks command without argument."""

    def act(door: Door, arguments: Mapping[str, Any]) -> Outcome:
        return door.tracerworks([command], None)

    return act


def _on_spec(command: str) -> Callable[[Door, Mapping[str, Any]], Outcome]:
    """An tracerworks command on one spec: its number, nothing else."""

    def act(door: Door, arguments: Mapping[str, Any]) -> Outcome:
        return door.tracerworks([command, str(arguments["spec"])], None)

    return act


def _relauncher_log(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    path = door.settings.relauncher_log
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Outcome(1, f"relauncher log unreadable: {exc}")
    return Outcome(0, last_lines(text, lines_of(arguments)))


def _serve_log(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    unit = f"{door.settings.serve_service}.service"
    return of_result(
        door.runner(
            [
                "journalctl", "--user", "--unit", unit, "--lines",
                str(lines_of(arguments)), "--no-pager", "--output", "short-iso",
            ]
        )
    )  # fmt: skip


def _worker_screen(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    window = door.settings.conventions.window_name(int(arguments["spec"]))
    names = {w.name for w in door.tmux.list_windows()}
    if window not in names:
        return Outcome(1, f"no window {window}: the worker is not running")
    count = lines_of(arguments)
    # the window's own history too, without its colours (no `-e`): plain text
    return Outcome(0, last_lines(door.tmux.capture(window, history=count), count))


def _machine(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    return Outcome(
        0,
        "\n".join(
            [
                *_memory(door.settings.meminfo),
                *_disk(door),
                *_health(door.settings.health_log),
                *_units(door),
                *_processes(door),
            ]
        ),
    )


def _memory(meminfo: Path) -> list[str]:
    mb = available_memory_mb(meminfo)
    return [f"memory available: {'unreadable' if mb is None else f'{mb} MB'}"]


def _disk(door: Door) -> list[str]:
    path = door.settings.worktree_root
    found = filesystem(door.runner, path)
    if found is None:
        return [f"disk: unreadable ({path})"]
    return [
        f"disk: {found.used_percent:.0f} % used,"
        f" {found.available_kb / KB_PER_GB:.1f} GB available ({path})"
    ]


def _health(log: Path) -> list[str]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"health log unreadable: {exc}"]
    return [f"health log, last line: {lines[-1] if lines else '(empty)'}"]


def _units(door: Door) -> list[str]:
    units = door.settings.mcp_units
    if not units:
        return ["units: none configured ([mcp] units)"]
    result = door.runner(
        [
            "systemctl", "--user", "show", "--property",
            "Id,LoadState,ActiveState,SubState", "--", *units,
        ]
    )  # fmt: skip
    if not result.ok:
        return [f"units unreadable: {(result.stderr or result.stdout).strip()}"]
    lines = ["units:"]
    for block in result.stdout.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        state = (
            props.get("LoadState", "?")
            if props.get("LoadState") != "loaded"
            else f"{props.get('ActiveState', '?')} ({props.get('SubState', '?')})"
        )
        lines.append(f"  {props.get('Id', '?')}: {state}")
    return lines


def _processes(door: Door) -> list[str]:
    result = door.runner(
        [
            "ps", "--user", str(door.uid), "--sort=-rss",
            "--format", "pid=,rss=,etimes=,comm=",
        ]
    )  # fmt: skip
    if not result.ok:
        return [f"processes unreadable: {(result.stderr or result.stdout).strip()}"]
    lines = [f"largest processes (resident memory, top {TOP_PROCESSES}):"]
    for row in result.stdout.splitlines()[:TOP_PROCESSES]:
        parts = row.split(None, 3)
        if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
            continue
        pid, rss, seconds, command = parts
        hours, rest = divmod(int(seconds), 3600)
        lines.append(
            f"  {pid:>8}  {int(rss) // KB_PER_MB:>6} MB  {hours}h{rest // 60:02d}"
            f"  {command}"
        )
    return lines


def _pr_checks(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    # gh: 1 when a check failed, 8 when some are pending — answers, not errors
    return of_result(
        door.runner(
            ["gh", "pr", "checks", str(arguments["pr"]), "--repo",
             door.settings.github_repo]
        ),
        frozenset({0, 1, 8}),
    )  # fmt: skip


# The tools — acting, each on the operator's approval (the client's setting).
# Every argument list is written here, whole: no tool reaches `--force`,
# `--over-cap`, `--over-limit`, `provider`, `quota` or `clean`.


def _message(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    text: str = arguments["text"]
    return door.tracerworks(["message", str(arguments["spec"])], text.encode("utf-8"))


def _restart_serve(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    unit = f"{door.settings.serve_service}.service"
    return of_result(door.runner(["systemctl", "--user", "restart", unit]))


def _run_workflow(door: Door, arguments: Mapping[str, Any]) -> Outcome:
    name: str = arguments["name"]
    allowed = door.settings.mcp_workflows
    if name not in allowed:
        return Outcome(
            1,
            f"workflow {name!r} is not one the door runs; it runs:"
            f" {', '.join(allowed) or 'none'} ([mcp] workflows)",
        )
    return of_result(
        door.runner(
            [
                "gh", "workflow", "run", name, "--repo", door.settings.github_repo,
                "--ref", door.settings.base_branch,
            ]
        )
    )  # fmt: skip


TOOLS: tuple[Tool, ...] = (
    Tool(
        "status",
        "The factory as `tracerworks status` reads it: workers and their state,"
        " sessions held, merge queue, runner, memory, provider, quota.",
        _plain("status"),
    ),
    Tool(
        "snapshot",
        "Everything the dashboard shows, as JSON (`tracerworks snapshot`).",
        _plain("snapshot"),
    ),
    Tool(
        "history",
        "A spec's history: each change of its state, and its cause.",
        _on_spec("history"),
        {"spec": SPEC},
    ),
    Tool(
        "usage",
        "The tokens a spec's worker spent, owner and subagents apart.",
        _on_spec("usage"),
        {"spec": SPEC},
    ),
    Tool(
        "relauncher_log",
        "The last lines of the queue relauncher's log (the timer that launches"
        " the next spec and resumes the workers).",
        _relauncher_log,
        {"lines": LINES},
    ),
    Tool(
        "serve_log",
        "The last lines of the dashboard's systemd log (`tracerworks serve`).",
        _serve_log,
        {"lines": LINES},
    ),
    Tool(
        "worker_screen",
        "The last lines of a worker's terminal (its tmux window), as plain text:"
        " whether it works, waits for a question or a permission, or rests.",
        _worker_screen,
        {"spec": SPEC, "lines": LINES},
    ),
    Tool(
        "machine",
        "The machine: available memory, disk, the last line of the health log,"
        " the state of the factory's systemd units and the largest processes.",
        _machine,
    ),
    Tool(
        "pr_checks",
        "The checks of a pull request (`gh pr checks`): exit 0 all green,"
        " 1 one failed, 8 some pending.",
        _pr_checks,
        {"pr": PULL},
    ),
    Tool(
        "message",
        "Send a worker a message (`tracerworks message`): pasted in its window,"
        " its reception verified. The text goes on stdin, never into a command.",
        _message,
        {"spec": SPEC, "text": TEXT},
        read_only=False,
    ),
    Tool(
        "start",
        "Launch the worker of a spec (`tracerworks start`), within the factory's"
        " limits: a refusal (sessions busy, low memory) comes back as is.",
        _on_spec("start"),
        {"spec": SPEC},
        read_only=False,
    ),
    Tool(
        "start_next",
        "Launch the next spec of the queue (`tracerworks start-next`).",
        _plain("start-next"),
        read_only=False,
    ),
    Tool(
        "pause",
        "Interrupt a worker (`tracerworks pause`: Escape in its window).",
        _on_spec("pause"),
        {"spec": SPEC},
        read_only=False,
    ),
    Tool(
        "resume",
        "Resume a paused worker (`tracerworks resume`).",
        _on_spec("resume"),
        {"spec": SPEC},
        read_only=False,
    ),
    Tool(
        "stop",
        "Close a worker once everything is pushed (`tracerworks stop`, never"
        " forced): with unpushed work, refused, and the worker asked to push.",
        _on_spec("stop"),
        {"spec": SPEC},
        read_only=False,
    ),
    Tool(
        "set_aside",
        "Close a worker's session and keep its work, resumed once a"
        " session is free (`tracerworks set-aside`).",
        _on_spec("set-aside"),
        {"spec": SPEC},
        read_only=False,
    ),
    Tool(
        "self_update",
        "Install tracerworks from the remote base branch and restart the"
        " dashboard on it (`tracerworks self-update`).",
        _plain("self-update"),
        read_only=False,
    ),
    Tool(
        "restart_serve",
        "Restart the dashboard's systemd unit (`tracerworks serve`).",
        _restart_serve,
        read_only=False,
    ),
    Tool(
        "run_workflow",
        "Run one of the project's workflows ([mcp] workflows: the deployment,"
        " the full CI) on the base branch, by `gh workflow run`.",
        _run_workflow,
        {"name": WORKFLOW},
        read_only=False,
    ),
)


class Server:
    """One JSON-RPC message in, at most one out; never stops on a bad one."""

    def __init__(self, door: Door, tools: Sequence[Tool] = TOOLS) -> None:
        self.door = door
        self.tools = {tool.name: tool for tool in tools}

    def serve(self, stdin: IO[bytes], out: IO[str]) -> int:
        for raw in stdin:
            if not raw.strip():
                continue
            reply = self.handle(raw)
            if reply is not None:
                out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                out.flush()
        return 0

    def handle(self, raw: bytes) -> dict[str, Any] | None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _error(None, PARSE_ERROR, f"parse error: {exc}")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        method = message.get("method")
        if "id" not in message:
            return None  # a notification (or a response): nothing to answer
        ident = message["id"]
        if not isinstance(method, str):
            return _error(ident, INVALID_REQUEST, "a request needs a method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(ident, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _result(ident, self.initialize(params))
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": [t.listed() for t in self.tools.values()]})
        if method == "tools/call":
            return self.call(ident, params)
        return _error(ident, METHOD_NOT_FOUND, f"unknown method: {method}")

    def initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }

    def call(self, ident: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(str(params.get("name")))
        if tool is None:
            return _error(ident, INVALID_PARAMS, f"unknown tool: {params.get('name')}")
        try:
            arguments = tool.checked(params.get("arguments") or {})
        except InvalidParams as exc:
            return _error(ident, INVALID_PARAMS, f"{tool.name}: {exc}")
        try:
            outcome = tool.run(self.door, arguments)
        except CommandError as exc:
            outcome = Outcome(3, str(exc))
        except Exception as exc:
            # the server outlives a tool's bug: that call fails, said, alone
            outcome = Outcome(3, f"{tool.name} failed: {type(exc).__name__}: {exc}")
        text = f"{outcome.text}\n[exit {outcome.code}]".lstrip("\n")
        return _result(
            ident,
            {"content": [{"type": "text", "text": text}], "isError": outcome.failed},
        )


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
