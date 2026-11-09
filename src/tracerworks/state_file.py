"""The state file (`[paths] state_file`) — the only file `tracerworks` writes.

It holds what the operator DECLARES (provider for the next workers, weekly
Claude usage) and the thresholds; never anything about the workers
themselves, which is rebuilt at each call. A missing file is the defaults.

The format is a two-level YAML subset (`key: value`, one level of nesting,
`#` comments), read and written here rather than through PyYAML so that the
tool keeps no dependency outside the standard library (see `__init__`). An
unknown key or a malformed value is refused loudly: a typo in `weekly_cap`
silently falling back to the default is precisely a cap that does not hold.

The dashboard's Settings tab (#2190) writes here too, through `assign`, as
the command line does: the TUNABLES, each within its bounds — checked on
writing as on reading; a value out of them read from the file is an error,
never a silent fallback. A key never written follows the code's default;
assigning `None` removes it, and the default comes back. Two writers at
once: the last wins, each starting again from the file read back (`update`).
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from tracerworks.alerts import FAMILIES
from tracerworks.providers import PROVIDERS


class StateFileError(ValueError):
    """The configuration file cannot be trusted as written."""


@dataclass(frozen=True)
class State:
    provider: str = "claude"
    claude_weekly_used: int | None = None  # None: never declared
    claude_weekly_cap: int = 80
    # The Codex route opens only after its spike (#571): installed,
    # authenticated, skills visible, one real chantier measured.
    codex_available: bool = False
    max_workers: int = 2
    min_available_memory_mb: int = 2500
    # While the runner runs a CI, a worker may still start above this (26/09).
    min_available_memory_mb_runner_busy: int = 3500
    # The queue paused from the Settings tab (#2192): `start-next` starts
    # nothing; `start <n>` still does; the workers running go on.
    queue_paused: bool = False
    # The month's included GitHub Actions minutes; None: the project file's
    # `[quotas] actions_included_minutes` (#2192).
    github_actions_included_minutes: int | None = None
    # The Telegram alerts (#2193): all of them, and the families switched off
    # (keys of `alerts.FAMILIES`) — each `alerts.<family>: false` in the file.
    alerts_telegram: bool = True
    alerts_off: frozenset[str] = frozenset()
    # The keys the file holds (`claude.weekly_cap`…): written back even at
    # their default value. Where a value came from, not what a state IS.
    written: frozenset[str] = field(default=frozenset(), compare=False)


@dataclass(frozen=True)
class Tunable:
    """A setting the dashboard's Settings tab shows and changes (#2190): its
    key in the file, its block in the tab, its label, its kind (a whole
    number or a switch), its unit and bounds (`maximum` None: no upper one).
    A whole number whose default is None is optional: absent, another
    default holds (the project file's, said by the caller)."""

    key: str
    block: str
    label: str
    unit: str = ""
    minimum: int | None = None
    maximum: int | None = None
    kind: str = "int"  # "int" | "bool"


# The tab's blocks, in their order: (key, label).
BLOCKS: tuple[tuple[str, str], ...] = (
    ("launches", "Launches"),
    ("quotas", "Quotas"),
    ("alerts", "Alerts"),
)
TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        "queue.paused",
        "launches",
        "Queue paused: start-next starts nothing, the workers running go on",
        kind="bool",
    ),
    Tunable("max_workers", "launches", "Workers at most", "", 1, 3),
    Tunable(
        "min_available_memory_mb",
        "launches",
        "Minimum available memory to start, runner free",
        "MB",
        1500,
    ),
    Tunable(
        "min_available_memory_mb_runner_busy",
        "launches",
        "Minimum available memory to start, runner busy",
        "MB",
        1500,
    ),
    Tunable(
        "claude.weekly_cap",
        "launches",
        "Claude weekly quota above which nothing starts",
        "%",
        50,
        100,
    ),
    Tunable(
        "github.actions_included_minutes",
        "quotas",
        "GitHub Actions minutes included in the month",
        "min",
        0,
        20000,
    ),
    Tunable(
        "alerts.telegram",
        "alerts",
        "Telegram: off, no alert is sent (all still listed in Measures)",
        kind="bool",
    ),
    # the workers' token has none: it guards a secret (#2193)
    *(
        Tunable(f"alerts.{family.key}", "alerts", family.label, kind="bool")
        for family in FAMILIES
    ),
)
_TUNED = frozenset(t.key for t in TUNABLES)


_LINE = re.compile(r"^(?P<indent> *)(?P<key>[a-z_]+):(?: +(?P<value>.*))?$")

# key path → (field, parser)
_INT = "int"
_BOOL = "bool"
_PROVIDER = "provider"
_OPTIONAL_INT = "optional_int"
_FAMILY = "family"  # a switch kept as membership of `alerts_off`
_SCHEMA: dict[str, tuple[str, str]] = {
    "provider": ("provider", _PROVIDER),
    "claude.weekly_used": ("claude_weekly_used", _OPTIONAL_INT),
    "claude.weekly_cap": ("claude_weekly_cap", _INT),
    "codex.available": ("codex_available", _BOOL),
    "max_workers": ("max_workers", _INT),
    "min_available_memory_mb": ("min_available_memory_mb", _INT),
    "min_available_memory_mb_runner_busy": (
        "min_available_memory_mb_runner_busy",
        _INT,
    ),
    "queue.paused": ("queue_paused", _BOOL),
    "github.actions_included_minutes": (
        "github_actions_included_minutes",
        _OPTIONAL_INT,
    ),
    "alerts.telegram": ("alerts_telegram", _BOOL),
    **{f"alerts.{f.key}": ("alerts_off", _FAMILY) for f in FAMILIES},
}
_SECTIONS = {"claude", "codex", "queue", "github", "alerts"}
_COMMENTS = {
    "provider": "claude | codex — for the NEXT workers",
    "claude.weekly_used": "declared: tracerworks quota <pct>",
    "codex.available": "opened once the Codex route is validated",
}


def _parse_value(kind: str, raw: str, where: str) -> object:
    if kind == _PROVIDER:
        if raw not in PROVIDERS:
            raise StateFileError(
                f"{where}: provider must be one of {sorted(PROVIDERS)}"
            )
        return raw
    if kind in (_BOOL, _FAMILY):
        if raw not in ("true", "false"):
            raise StateFileError(f"{where}: expected true or false, got {raw!r}")
        return raw == "true"
    if kind == _OPTIONAL_INT and raw in ("", "null", "~"):
        return None
    if not re.fullmatch(r"[0-9]+", raw):
        raise StateFileError(f"{where}: expected a non-negative integer, got {raw!r}")
    return int(raw)


def parse(text: str, source: str = "state.yaml") -> State:
    values: dict[str, object] = {}
    written: set[str] = set()
    section: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{source}:{number}"
        stripped = re.sub(r"\s+#.*$|^\s*#.*$", "", line).rstrip()
        if not stripped:
            continue
        match = _LINE.match(stripped)
        if not match:
            raise StateFileError(f"{where}: cannot read {line!r}")
        indent, key, value = match["indent"], match["key"], match["value"]
        if indent == "":
            if value is None:
                if key not in _SECTIONS:
                    raise StateFileError(f"{where}: unknown section {key!r}")
                section = key
                continue
            section = None
            path = key
        elif indent == "  " and section is not None:
            path = f"{section}.{key}"
        else:
            raise StateFileError(f"{where}: unexpected indentation")
        if path not in _SCHEMA:
            raise StateFileError(f"{where}: unknown key {path!r}")
        name, kind = _SCHEMA[path]
        read = _parse_value(kind, (value or "").strip(), f"{where} ({path})")
        if kind == _FAMILY:
            family = path.split(".", 1)[1]
            off = set(values.get(name, frozenset()))  # type: ignore[call-overload]
            values[name] = frozenset(off - {family} if read else off | {family})
        else:
            values[name] = read
        if not _at_default(path, value_of(_partial(values), path)):
            written.add(path)
    # each value checked per key above
    state = replace(State(), **values, written=frozenset(written))  # type: ignore[arg-type]
    _check(state, source)
    return state


def value_of(state: State, key: str) -> object:
    name, kind = _SCHEMA[key]
    if kind == _FAMILY:
        return key.split(".", 1)[1] not in state.alerts_off
    return getattr(state, name)


def default_of(key: str) -> object:
    return value_of(State(), key)


def _at_default(key: str, value: object) -> bool:
    """A setting of the tab at its code default IS the default: not kept as
    set, so a PR that changes the default still applies (#2190, story 22) —
    the file written before #2190 held every key at its value."""
    return key in _TUNED and value == default_of(key)


def _partial(values: Mapping[str, object]) -> State:
    return replace(State(), **values)  # type: ignore[arg-type]  # checked per key


def bounds_of(tunable: Tunable) -> str:
    if tunable.maximum is None:
        return f"at least {tunable.minimum}"
    return f"from {tunable.minimum} to {tunable.maximum}"


def out_of_bounds(tunable: Tunable, value: object) -> str | None:
    """Why `value` cannot be this setting, or None. None is the value of an
    optional setting left to its other default."""
    if tunable.kind == "bool":
        if isinstance(value, bool):
            return None
        return f"{tunable.key} must be true or false, not {value!r}"
    if value is None and default_of(tunable.key) is None:
        return None
    if (
        type(value) is int
        and (tunable.minimum is None or value >= tunable.minimum)
        and (tunable.maximum is None or value <= tunable.maximum)
    ):
        return None
    return f"{tunable.key} must be a whole number {bounds_of(tunable)}, not {value!r}"


def _check(state: State, source: str) -> None:
    for tunable in TUNABLES:
        problem = out_of_bounds(tunable, value_of(state, tunable.key))
        if problem is not None:
            raise StateFileError(f"{source}: {problem}" if source else problem)
    if state.claude_weekly_used is not None and state.claude_weekly_used > 100:
        raise StateFileError(f"{source}: claude.weekly_used must be within 0..100")


def _checked_value(key: str, kind: str, value: object) -> object:
    if kind == _PROVIDER and value not in PROVIDERS:
        raise StateFileError(f"provider must be one of {sorted(PROVIDERS)}")
    if kind in (_BOOL, _FAMILY) and not isinstance(value, bool):
        raise StateFileError(f"{key} must be true or false, not {value!r}")
    if kind in (_INT, _OPTIONAL_INT) and (type(value) is not int or value < 0):
        raise StateFileError(
            f"{key} must be a non-negative whole number, not {value!r}"
        )
    return value


def assign(state: State, changes: Mapping[str, object]) -> State:
    """`state` with `changes` (key → value; None removes the key, and its
    default comes back) — or StateFileError naming the first value refused."""
    values: dict[str, object] = {}
    written = set(state.written)
    off = set(state.alerts_off)
    for key, value in changes.items():
        if key not in _SCHEMA:
            raise StateFileError(f"unknown setting {key!r}")
        name, kind = _SCHEMA[key]
        if kind == _FAMILY:
            on = True if value is None else _checked_value(key, kind, value)
            family = key.split(".", 1)[1]
            off = off - {family} if on else off | {family}
            values[name] = frozenset(off)
            (written.discard if on else written.add)(key)
            continue
        if value is None:
            values[name] = default_of(key)
            written.discard(key)
            continue
        values[name] = _checked_value(key, kind, value)
        (written.discard if _at_default(key, value) else written.add)(key)
    # each value checked per key above
    changed = replace(state, **values, written=frozenset(written))  # type: ignore[arg-type]
    _check(changed, "")
    return changed


def _raw(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render(state: State) -> str:
    """The keys the file held, and any other away from its default: a key
    never touched stays out, and follows the code's default."""
    kept = [
        key
        for key in _SCHEMA
        if key in state.written or value_of(state, key) != default_of(key)
    ]
    lines = ["# Written by tracerworks"]
    section = ""
    for key in sorted(kept, key=lambda k: ("." in k, k)):
        head, _, tail = key.rpartition(".")
        if head and head != section:
            lines.append(f"{head}:")
            section = head
        line = f"{'  ' if head else ''}{tail}: {_raw(value_of(state, key))}"
        comment = _COMMENTS.get(key)
        lines.append(f"{line}  # {comment}" if comment else line)
    return "\n".join(lines) + "\n"


def load(path: Path) -> State:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    return parse(text, str(path))


def save(path: Path, state: State) -> None:
    """Write atomically — a file beside it, then renamed over it — and only
    what `parse` would accept back: a failed write leaves the last one."""
    text = render(state)
    parse(text, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(path: Path, change: Callable[[State], State]) -> tuple[State, State]:
    """Read the file again, change it, write it: (before, after). What the
    command line and the dashboard write, so that neither loses the other's
    keys."""
    before = load(path)
    after = change(before)
    save(path, after)
    return before, after
