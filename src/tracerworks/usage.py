"""What a chantier cost in tokens, read from Claude Code's own session logs.

Claude Code writes every session as JSON lines under
`<config>/projects/<slug of the working directory>/`: the worker's sessions at
the top, each subagent in `<session>/subagents/agent-*.jsonl` with a
`.meta.json` naming it. A worker runs in its chantier's worktree, so the slug
of that worktree IS the chantier: its cost is measurable after the fact, with
nothing recorded by `tracerworks` itself.

ONE MESSAGE IS COUNTED ONCE. The log repeats an assistant message once per
content block (measured on 26/09: 246 lines for 132 messages), each copy with
the same `usage`; summing lines would double the bill. The last copy of each
`message.id` wins.

The four counts are kept apart because they cost differently: fresh input,
cache writes, cache reads (cheap) and output.

WHAT THE SESSION REALLY RAN WITH (#2252): the model of its last answer
(`message.model` of an assistant line) and the effort of its last request
(`message.output_config.effort` of an `api-request-blob` line), as measured
in the logs on 03/11 — the worker's own session's, never a subagent's, which
follows its agent's definition.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Tokens:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    messages: int = 0

    def add(self, other: Tokens) -> None:
        self.input += other.input
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.messages += other.messages

    @property
    def total(self) -> int:
        """Everything the model processed, cache reads included."""
        return self.input + self.cache_write + self.cache_read + self.output

    @property
    def without_cache_reads(self) -> int:
        return self.input + self.cache_write + self.output


@dataclass
class Part:
    """One transcript: the worker's session, or one subagent."""

    name: str
    tokens: Tokens
    first: datetime | None
    last: datetime | None
    model: str | None = None  # of its last answer
    effort: str | None = None  # of its last request


@dataclass
class ChantierUsage:
    owner: list[Part] = field(default_factory=list)
    subagents: list[Part] = field(default_factory=list)

    def owner_tokens(self) -> Tokens:
        return _sum(p.tokens for p in self.owner)

    def subagent_tokens(self) -> Tokens:
        return _sum(p.tokens for p in self.subagents)

    def total(self) -> Tokens:
        return _sum(p.tokens for p in (*self.owner, *self.subagents))

    def seen(self) -> tuple[str | None, str | None]:
        """The model and the effort the worker's newest session last ran
        with — None for each its log does not say."""
        dated = [(p.last, p) for p in self.owner if p.last is not None]
        if not dated:
            return None, None
        _, newest = max(dated, key=lambda pair: pair[0])
        return newest.model, newest.effort

    def span(self) -> tuple[datetime, datetime] | None:
        moments = [
            m for p in (*self.owner, *self.subagents) for m in (p.first, p.last) if m
        ]
        return (min(moments), max(moments)) if moments else None


def _sum(parts: Iterable[Tokens]) -> Tokens:
    total = Tokens()
    for tokens in parts:
        total.add(tokens)
    return total


def project_dir(transcripts_root: Path, working_dir: Path) -> Path:
    """Claude Code's folder for a working directory: `/` and `.` become `-`."""
    slug = str(working_dir).replace("/", "-").replace(".", "-")
    return transcripts_root / slug


def read_transcript(path: Path, name: str) -> Part:
    by_id: dict[str, Tokens] = {}
    first: datetime | None = None
    last: datetime | None = None
    model: str | None = None
    effort: str | None = None
    # Line by line: a long session's log is never held in memory whole (the
    # dashboard reads each worker's every minute, on a machine without swap).
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line being written while we read
            stamp = _moment(record.get("timestamp"))
            if stamp is not None:
                first = stamp if first is None or stamp < first else first
                last = stamp if last is None or stamp > last else last
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            if record.get("type") == "api-request-blob":
                config = message.get("output_config")
                asked = config.get("effort") if isinstance(config, dict) else None
                if isinstance(asked, str) and asked:
                    effort = asked
                continue
            if record.get("type") != "assistant":
                continue
            said = message.get("model")
            # Claude Code's own messages (an API error) say « <synthetic> »
            if isinstance(said, str) and said and not said.startswith("<"):
                model = said
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            key = str(message.get("id") or f"line-{len(by_id)}")
            by_id[key] = Tokens(
                input=int(usage.get("input_tokens") or 0),
                cache_write=int(usage.get("cache_creation_input_tokens") or 0),
                cache_read=int(usage.get("cache_read_input_tokens") or 0),
                output=int(usage.get("output_tokens") or 0),
                messages=1,
            )
    return Part(name, _sum(by_id.values()), first, last, model, effort)


def chantier_usage(transcripts_root: Path, working_dir: Path) -> ChantierUsage | None:
    """None when the worktree never ran a Claude session."""
    folder = project_dir(transcripts_root, working_dir)
    if not folder.is_dir():
        return None
    usage = ChantierUsage()
    for session in sorted(folder.glob("*.jsonl")):
        usage.owner.append(read_transcript(session, f"session {session.stem[:8]}"))
        for agent in sorted((folder / session.stem / "subagents").glob("*.jsonl")):
            usage.subagents.append(read_transcript(agent, _describe(agent)))
    return usage


def _describe(agent: Path) -> str:
    meta = agent.with_suffix(".meta.json")
    try:
        description = json.loads(meta.read_text(encoding="utf-8")).get("description")
    except (OSError, json.JSONDecodeError):
        description = None
    return str(description) if description else agent.stem


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
