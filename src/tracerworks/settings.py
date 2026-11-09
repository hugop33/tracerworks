"""The project `tracerworks` drives, read from ONE configuration file per project.

Nothing specific to a project lives in the code — no repository, label, tmux
session, worktree root, branch prefix, runner name, operator or primer rule:
the tool must stay extractable into its own repository by copying its folder.
Everything of that kind is in the project file (TOML, see `tracerworks.toml` at the
root of the repository that uses it), found by `--config`, else
`$TRACERWORKS_CONFIG`, else `~/.config/tracerworks/project.toml`.

Every key is required and an unknown key is refused: a typo silently falling
back to a default is a worker started in the wrong place.

Two MACHINE overrides stay in the environment, for the real tests only:
`TRACERWORKS_TMUX_SOCKET` (a separate tmux server, `tmux -L`) and
`TRACERWORKS_MEMINFO`.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tracerworks.conventions import Conventions
from tracerworks.level import EFFORTS, LevelRules
from tracerworks.natures import Natures


class ConfigError(ValueError):
    """The project file is missing, unreadable, or not what the tool expects."""


@dataclass(frozen=True)
class Waits:
    """How long each wait may last before someone is told (#1730)."""

    #: a worker waiting for a human (a prompt, a question, a login, a
    #: limit): past this, the human label goes on its spec, with a comment
    human_minutes: int
    #: a chantier parked with a red or conflicting pull request, no resume
    parked_red_minutes: int
    #: a worker that ended its turn waiting for memory is told to go on once
    #: this much is available again
    memory_resume_mb: int
    #: a worker at rest in development whose turn ended this long ago on a
    #: demand of permission is nudged (#1789)
    permission_ask_minutes: int = 20
    #: what such a demand says (regular expressions, any case), in its end
    permission_ask: tuple[str, ...] = ()
    #: what a decision of the operator's says: the nudge waits for him
    decision: tuple[str, ...] = ()
    #: an open pull request whose head has no CI alive nor told for this
    #: long sees its last run re-run (#1790)
    no_ci_minutes: int = 10
    #: a pull request in conflict with the base for this long: its worker
    #: woken, or a comment and an alert (#1791)
    conflict_minutes: int = 10
    #: a pull request's red CI nobody holds (no worker at work, none parked,
    #: no machine's re-run) goes in the banner past this long (#2057)
    red_unowned_minutes: int = 15
    #: the memory available under the alert threshold for this long goes in
    #: the banner, with its three largest processes (#2096)
    low_memory_minutes: int = 15


@dataclass(frozen=True)
class MergeQueue:
    """The merge queue's names (#1714, spec #1708): what keeps a pull request
    out of it (no label puts one in, #1801), what tracerworks alone puts on while
    it tests one, the status it posts before it merges — and how to read the
    CI's own decisions."""

    testing_label: str  # tracerworks's alone: the CI runs everything
    status_context: str  # the commit status posted on the head before a merge
    #: the check the CI skips when a change runs nothing: skipped on a pull
    #: request, its change is « documentation only »
    nothing_check: str
    #: the event of the night's run of `ci_workflow` on the base branch: the
    #: queue starts no full suite while one runs
    night_event: str
    #: the label that keeps a pull request out of the queue, on the path of
    #: before (a production emergency, #1801): its auto-merge left alone, the
    #: CI's selection of before
    off_label: str
    #: a head without its own green light lane enters on an earlier head's,
    #: for the same code diff (#1844): false switches the rule off
    reuse: bool = True
    #: what is not code for that diff, besides `docs/**` and `*.md` (git glob
    #: pathspecs from the repository's root)
    reuse_ignore: tuple[str, ...] = ()
    #: the CI's steps whose failure is the code's, never a machine's (#2080):
    #: a failed job whose failed step's name starts with one of them is a
    #: code red — the worker at once, no rerun (the queue and the light lanes)
    code_steps: tuple[str, ...] = ()
    #: passes in a row where `gh pr merge` fails for a reason other than
    #: « not mergeable » (network, rights) before the pull request in test
    #: leaves with it (#2156): never a queue frozen on one call
    merge_error_passes: int = 3
    #: a full suite red on at most this many pytest tests, none in a file the
    #: pull request modifies, is rerun once in the queue like a machine's red,
    #: and a green rerun opens each test's bug (#2130, spec #2127); 0, the
    #: file saying nothing: the rule off
    flaky_max_tests: int = 0
    #: A TEST THE FACTORY RECOGNISES FLAKY OPENS ITS BUG (#2224): recognised
    #: (`quality.flaky_tests`' two rules) this many times within
    #: `flaky_bug_days`, it opens one `bug` in its name; a new failure
    #: comments it; `flaky_bug_close_days` without a failure close it
    flaky_bug_times: int = 2
    flaky_bug_days: int = 7
    flaky_bug_close_days: int = 14


@dataclass(frozen=True)
class Install:
    """A folder each worktree installs for itself (#1709): `run`, in the
    folder of `lock`, whenever the worktree's lock is not the one the folder
    was last installed from. Paths from the repository's root."""

    path: str  # `front/node_modules`
    lock: str  # `front/package-lock.json`
    run: str  # a shell command: `npm ci …`


@dataclass(frozen=True)
class Settings:
    config_file: Path
    # [github]
    github_repo: str  # owner/name
    ready_label: str  # the label of a spec ready for a worker
    launch_label: str  # the label that puts a ready spec in start-next's queue
    priority_label: str  # queued specs carrying it go first
    #: the operator's pick (#1875): first in its group on the dashboard, read
    #: by the sessions — never an order of the queue; only the operator puts it
    star_label: str
    progress_label: str  # the ticket a worker declares it is on
    human_label: str  # a ticket whose worker waits for the operator
    parent_keywords: tuple[str, ...]  # « Parent : #n » in a ticket body
    blocker_keywords: tuple[str, ...]  # « Depends on : #a, #b »
    runner_name: str  # the self-hosted runners on the machine, comma-separated
    deploy_workflow: str  # its successful runs are the deployments (dashboard)
    #: the spec's labels its pull request carries (#1297): named in the
    #: primer, put back by `clean` when missing; none: nothing asked
    carried_labels: tuple[str, ...]
    #: the operator's GitHub login (#1529), not `operator`, the name shown:
    #: trusted with the known workers' logins — their comments are instructions
    operator_login: str
    worker_logins: tuple[
        str, ...
    ]  # the workers' own logins; none: they act as the operator
    # [natures]
    natures: Natures  # the label of each nature of an issue (dashboard, #857)
    # [git]
    repo_dir: Path  # the main clone, where `git worktree` is run from
    remote: str
    base_branch: str
    # [workers]
    tmux_session: str
    conventions: Conventions
    operator: str  # who the messages come from, as the workers read it
    primer_rules: tuple[str, ...]  # `{branch}` is replaced by the branch
    # folders of the main clone a worktree borrows (`front/node_modules`, #984):
    # linked into it at launch, and an additional directory of a Claude worker
    shared_dirs: tuple[str, ...]
    #: folders each worktree installs for itself (#1709), at launch and
    #: resume, when its lock changed: never borrowed, never stale
    installs: tuple[Install, ...]
    #: the repository's hooks folder (#1703), set as `core.hooksPath` of each
    #: worktree prepared — its own, never the main clone's: none, no hook
    hooks_dir: str | None
    #: the `systemd --user` slice each worker's scope goes in (#1724): stopping
    #: the worker stops the scope, every process it started included. None (a
    #: Mac, no systemd): the launch is a plain `exec`
    worker_slice: str | None
    #: each worker's scope's memory cap, in GB, its swap included (#2095): a
    #: runaway test is killed by the kernel inside its scope, never a runner
    worker_memory_max_gb: int
    #: what a worker's test runs (#2096), each a program and the words right
    #: after it: one older than `worker_test_max_minutes` is stopped
    worker_test_commands: tuple[str, ...]
    worker_test_max_minutes: int
    #: a worker at rest waiting on its own background job is not parked
    #: (#2175) — up to this, then parked as before
    worker_background_max_minutes: int
    #: the permission rules a worker is denied, at launch and at resume
    #: (#1529): never empty — reviewed like code, installed by self-update
    disallowed_tools: tuple[str, ...]
    #: the workers' fine-grained GitHub token (#1529), mode 600, outside the
    #: repository: theirs when it is there, else the machine's, said
    worker_token_file: Path
    #: a tool name the worktree's session guard refuses whatever it is
    #: (#1529): its hook is tried on it before a launch or a resume
    guard_probe_tool: str
    #: variables taken out of a worker's environment at launch and resume
    #: (#1529): the operator's escape of the guard, which the tool never names
    stripped_env: tuple[str, ...]
    #: at most this many chantiers in the factory, in dev to the merge
    #: queue (#1823); the queue starts nothing above it
    in_flight: int
    #: the model and the effort a worker starts with (#2251): the project's
    #: default, the models allowed, and the spec's labels that choose them
    level: LevelRules
    # [install]
    source_dir: str  # where the tool lives in the repository
    config_in_repo: str  # this file, in the repository, installed with it
    # [paths]
    state_file: Path
    cache_dir: Path  # the primers handed to workers
    install_dir: Path  # copies of the tool, one per commit of the base branch
    bin_dir: Path  # where the `tracerworks` launcher is written
    relauncher_log: Path  # the queue relauncher's journal: chantiers launched
    relauncher_timer: str  # its `systemd --user` timer: when it runs next (#869)
    # [serve] — the dashboard (#806)
    serve_host: str  # a LOCAL interface: the proxy in front terminates HTTPS
    serve_port: int
    serve_secret_file: Path  # the password, outside the repository, mode 600
    serve_service: str  # its `systemd --user` unit, written by self-update
    # [quotas] — read by the dashboard and the admission cap (#822, #823)
    claude_quota_file: Path  # what the workers' status line last saw
    claude_statusline: bool  # launch Claude workers with that status line
    billing_token_file: Path  # a read-only GitHub token (« Plan: read »), mode 600
    actions_included_minutes: int  # the plan's monthly Actions minutes
    #: the repository variable set past 90 % of them (#1296); None: no net
    budget_variable: str | None
    # [history] — the base of the factory's history (#836)
    history_db: Path  # SQLite, outside the repository, owner only
    #: the one-time backfill starts that day (UTC); None: with the project (#851)
    history_since: date | None
    health_log: Path  # the VPS health journal: imported once; its load, every 5 min
    #: the VPS cleaner's log (#1726): its last pass is shown; None, unknown
    reaper_log: Path | None
    ci_workflow: str  # the CI workflow's file: its runs are a pull request's CI
    backup_dir: Path  # the nightly copies of the base, 7 days (#842)
    #: the projections' slopes: over how many rolling days (#864)
    actions_slope_days: int
    claude_week_slope_days: int
    #: each slope halves an instant's weight that far back (the operator, 05/10)
    actions_half_life_hours: int
    claude_week_half_life_hours: int
    # [merge_queue] (#1714)
    merge_queue: MergeQueue
    # [waits] (#1730)
    waits: Waits
    # [alerts] — Telegram (#841)
    alerts_chat_id: str  # the chat the bot writes to
    alerts_token_file: Path  # the bot's token, outside the repository, mode 600
    # [mcp] — the door of a remote session (`tracerworks mcp`, #2185)
    #: the `systemd --user` units its `machine` tool shows; none: none shown
    mcp_units: tuple[str, ...] = ()
    #: the workflows its `run_workflow` may run, on the base branch only
    mcp_workflows: tuple[str, ...] = ()
    # machine overrides (environment)
    tmux_socket: str | None = None  # `tmux -L <socket>`; None = the default server
    meminfo: Path = Path("/proc/meminfo")
    proc: Path = Path("/proc")  # `TRACERWORKS_PROC` for the tests
    # where `systemd --user` units live (`TRACERWORKS_SYSTEMD_DIR` for the tests)
    systemd_dir: Path = Path.home() / ".config" / "systemd" / "user"

    @property
    def trusted_logins(self) -> tuple[str, ...]:
        """Whose comments and issues are instructions (#1529); the rest is data."""
        return (self.operator_login, *self.worker_logins)

    @property
    def worktree_root(self) -> Path:
        return self.conventions.worktree_root

    @property
    def base_ref(self) -> str:
        return f"{self.remote}/{self.base_branch}"


_SCHEMA: dict[str, dict[str, type]] = {
    "github": {
        "repo": str,
        "ready_label": str,
        "launch_label": str,
        "priority_label": str,
        "star_label": str,
        "progress_label": str,
        "human_label": str,
        "parent_keywords": list,
        "blocker_keywords": list,
        "runner": str,
        "deploy_workflow": str,
        "carried_labels": list,
        "operator_login": str,
        "worker_logins": list,
    },
    "natures": {"spec": str, "ticket": str, "bug": str, "task": str, "idea": str},
    "git": {"repo_dir": str, "remote": str, "base_branch": str},
    "workers": {
        "tmux_session": str,
        "window_prefix": str,
        "worktree_root": str,
        "branch_prefix": str,
        "operator": str,
        "primer_rules": list,
        "shared_dirs": list,
        "installs": Install,
        "hooks_dir": str,
        "systemd_slice": str,
        "memory_max": int,
        "test_commands": list,
        "test_max_minutes": int,
        "background_max_minutes": int,
        "scratch_dir": str,
        "scope_prefix": str,
        "disallowed_tools": list,
        "github_token_file": str,
        "guard_probe_tool": str,
        "stripped_env": list,
        "in_flight": int,
        "model": str,
        "effort": str,
        "models": list,
        "model_label_prefix": str,
        "effort_label_prefix": str,
    },
    "install": {"source_dir": str, "config_in_repo": str},
    "paths": {
        "state_file": str,
        "cache_dir": str,
        "install_dir": str,
        "bin_dir": str,
        "relauncher_log": str,
        "relauncher_timer": str,
    },
    "serve": {"host": str, "port": int, "secret_file": str, "service": str},
    "quotas": {
        "claude_file": str,
        "claude_statusline": bool,
        "billing_token_file": str,
        "actions_included_minutes": int,
        "budget_variable": str,
    },
    "history": {
        "db": str,
        "since": str,
        "health_log": str,
        "reaper_log": str,
        "ci_workflow": str,
        "backup_dir": str,
        "actions_slope_days": int,
        "actions_half_life_hours": int,
        "claude_week_slope_days": int,
        "claude_week_half_life_hours": int,
    },
    "merge_queue": {
        "testing_label": str,
        "status_context": str,
        "nothing_check": str,
        "night_event": str,
        "off_label": str,
        "reuse": bool,
        "reuse_ignore": list,
        "code_steps": list,
        "merge_error_passes": int,
        "flaky_max_tests": int,
        "flaky_bug_times": int,
        "flaky_bug_days": int,
        "flaky_bug_close_days": int,
    },
    "waits": {
        "human_minutes": int,
        "parked_red_minutes": int,
        "memory_resume_mb": int,
        "permission_ask_minutes": int,
        "permission_ask": list,
        "decision": list,
        "no_ci_minutes": int,
        "conflict_minutes": int,
        "red_unowned_minutes": int,
        "low_memory_minutes": int,
    },
    "alerts": {"telegram_chat_id": str, "telegram_token_file": str},
    "mcp": {"units": list, "workflows": list},
}

#: Sections a file may leave out whole (#2185: a project without a remote
#: session's door).
_OPTIONAL_SECTIONS = {"mcp"}


def config_path(explicit: str | None, env: Mapping[str, str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if env.get("TRACERWORKS_CONFIG"):
        return Path(env["TRACERWORKS_CONFIG"]).expanduser()
    home = Path(env.get("HOME", str(Path.home())))
    return home / ".config" / "tracerworks" / "project.toml"


#: The projections' slopes' windows when the file leaves them out (#864, the operator
#: 01/10): both over a whole week, the latest days weighing more — each
#: instant half as much 48 h further back (the operator, 05/10: the uniform 2 days fell
#: to nothing after one quiet Claude day; the uniform week kept billing the
#: month for the CI the VPS took over).
ACTIONS_SLOPE_DAYS = 7
ACTIONS_HALF_LIFE_HOURS = 48
CLAUDE_WEEK_SLOPE_DAYS = 7
CLAUDE_WEEK_HALF_LIFE_HOURS = 48

#: The chantiers in flight when the file says nothing (#1823, the operator 25/10).
IN_FLIGHT = 6

#: A worker's memory cap when the file says nothing (#2095), in GB: peaks of
#: 1.8 to 2.3 GB measured on 29/10.
WORKER_MEMORY_MAX_GB = 3
#: A worker's test's limit when the file says nothing (#2096): the longest
#: integration slice takes about 27 min, on a runner.
WORKER_TEST_MAX_MINUTES = 30
#: How long a worker at rest waits on its background job before its park,
#: when the file says nothing (#2175): the 31/10 replay took 17 min.
WORKER_BACKGROUND_MAX_MINUTES = 60


def _passes(value: int, source: Path) -> int:
    """`[merge_queue] merge_error_passes`: 0 would send back every pull
    request without even trying its merge."""
    if value < 1:
        raise ConfigError(
            f"{source}: [merge_queue] merge_error_passes must be at least 1,"
            f" not {value}"
        )
    return value


def _not_negative(value: int, source: Path) -> int:
    """`[merge_queue] flaky_max_tests`: 0 switches the rule off, below is
    a typo."""
    if value < 0:
        raise ConfigError(
            f"{source}: [merge_queue] flaky_max_tests must be 0 or more, not {value}"
        )
    return value


def _positive(value: int, key: str, source: Path) -> int:
    """`[merge_queue] flaky_bug_*` (#2224): a count or a number of days, at
    least one — zero would open a bug for every red, or close it at once."""
    if value < 1:
        raise ConfigError(
            f"{source}: [merge_queue] {key} must be at least 1, not {value}"
        )
    return value


def _at_least_one(value: int, source: Path) -> int:
    """`[workers] in_flight`: 0 would stop every launch without a word."""
    if value < 1:
        raise ConfigError(
            f"{source}: [workers] in_flight must be at least 1, not {value}"
        )
    return value


#: Lists that are required but may be empty (#1529: no worker has its own
#: GitHub login yet).
_MAY_BE_EMPTY = {
    ("github", "worker_logins"),
    ("workers", "stripped_env"),
    ("merge_queue", "reuse_ignore"),
    ("merge_queue", "code_steps"),
    ("mcp", "units"),
    ("mcp", "workflows"),
}

#: Keys a file may leave out (#851: without `since`, the project's whole history).
_OPTIONAL = {
    ("history", "since"),
    ("history", "reaper_log"),
    ("workers", "shared_dirs"),
    ("workers", "installs"),
    ("workers", "hooks_dir"),
    ("workers", "systemd_slice"),
    ("workers", "memory_max"),
    ("workers", "test_max_minutes"),
    ("workers", "background_max_minutes"),
    ("workers", "scratch_dir"),
    ("workers", "scope_prefix"),
    ("workers", "in_flight"),
    ("github", "carried_labels"),
    ("quotas", "budget_variable"),
    ("history", "actions_slope_days"),
    ("history", "actions_half_life_hours"),
    ("history", "claude_week_slope_days"),
    ("history", "claude_week_half_life_hours"),
    ("waits", "permission_ask_minutes"),
    ("waits", "permission_ask"),
    ("waits", "decision"),
    ("waits", "no_ci_minutes"),
    ("waits", "conflict_minutes"),
    ("waits", "red_unowned_minutes"),
    ("waits", "low_memory_minutes"),
    ("merge_queue", "reuse"),
    ("merge_queue", "reuse_ignore"),
    ("merge_queue", "code_steps"),
    ("merge_queue", "merge_error_passes"),
    ("merge_queue", "flaky_max_tests"),
    ("merge_queue", "flaky_bug_times"),
    ("merge_queue", "flaky_bug_days"),
    ("merge_queue", "flaky_bug_close_days"),
    ("mcp", "units"),
    ("mcp", "workflows"),
}


def _checked(data: dict[str, Any], source: Path) -> dict[str, dict[str, Any]]:
    unknown = set(data) - set(_SCHEMA)
    if unknown:
        raise ConfigError(f"{source}: unknown section(s) {sorted(unknown)}")
    for section, keys in _SCHEMA.items():
        table = data.get(section)
        if table is None and section in _OPTIONAL_SECTIONS:
            table = data[section] = {}
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: missing section [{section}]")
        extra = set(table) - set(keys)
        if extra:
            raise ConfigError(
                f"{source}: unknown key(s) in [{section}]: {sorted(extra)}"
            )
        for key, kind in keys.items():
            value = table.get(key)
            if value is None and (section, key) in _OPTIONAL:
                continue
            if kind is Install:
                continue  # its own rules, below: tables, not strings
            if kind is bool:
                valid = isinstance(value, bool)
                expected = "true or false"
            elif kind is str:
                valid = isinstance(value, str) and bool(value.strip())
                expected = "a non-empty string"
            elif kind is int:
                valid = (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 < value < 65536
                )
                expected = "a whole number from 1 to 65535"
            else:
                empty_ok = (section, key) in _MAY_BE_EMPTY
                valid = (
                    isinstance(value, list)
                    and (bool(value) or empty_ok)
                    and all(isinstance(v, str) and v.strip() for v in value)
                )
                expected = (
                    "a list of strings" if empty_ok else "a non-empty list of strings"
                )
            if not valid:
                raise ConfigError(f"{source}: [{section}] {key} must be {expected}")
    if any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)
        for v in data["workers"].get("stripped_env", [])
    ):
        raise ConfigError(
            f"{source}: [workers] stripped_env must be environment variable names"
        )
    shared = data["workers"].get("shared_dirs", [])
    if not all(_inside(d) for d in shared):
        raise ConfigError(
            f"{source}: [workers] shared_dirs must be paths inside the repository"
        )
    _check_installs(data["workers"].get("installs", []), shared, source)
    hooks = data["workers"].get("hooks_dir")
    if hooks is not None and not _inside(hooks):
        raise ConfigError(
            f"{source}: [workers] hooks_dir must be a path inside the repository"
        )
    slice_name = data["workers"].get("systemd_slice")
    if slice_name is not None and not re.fullmatch(
        r"[A-Za-z0-9_-]+\.slice", slice_name
    ):
        raise ConfigError(
            f"{source}: [workers] systemd_slice must be a slice name (x.slice)"
        )
    scratch = data["workers"].get("scratch_dir")
    if scratch is not None and (
        Path(scratch).name != scratch or scratch in (".", "..")
    ):
        raise ConfigError(
            f"{source}: [workers] scratch_dir must be one folder name of the worktree"
        )
    if not all(re.fullmatch(UNIT_NAME, u) for u in data["mcp"].get("units", [])):
        raise ConfigError(f"{source}: [mcp] units must be systemd unit names")
    if not all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml", w)
        for w in data["mcp"].get("workflows", [])
    ):
        raise ConfigError(f"{source}: [mcp] workflows must be workflow file names")
    return data


def _waits(table: dict[str, Any], source: Path) -> Waits:
    """Its patterns are regular expressions: one that does not compile is
    said at load, never at the pass that reads a worker."""
    patterns = {
        key: tuple(table.get(key) or ()) for key in ("permission_ask", "decision")
    }
    for key, values in patterns.items():
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ConfigError(
                    f"{source}: [waits] {key}: {value!r} is no regular expression"
                    f" ({exc})"
                ) from exc
    return Waits(**{**table, **patterns})


#: A `systemd` unit's name with its type: never an option, never a pattern.
UNIT_NAME = (
    r"[A-Za-z0-9][A-Za-z0-9@._:-]*\.(?:service|timer|slice|scope|socket|target|path)"
)


def _inside(value: str) -> bool:
    return not (Path(value).is_absolute() or ".." in Path(value).parts) and bool(
        Path(value).parts
    )


def _check_installs(installs: Any, shared: list[str], source: Path) -> None:
    if not isinstance(installs, list) or not all(
        isinstance(i, dict)
        and set(i) == {"path", "lock", "run"}
        and all(isinstance(v, str) and v.strip() for v in i.values())
        for i in installs
    ):
        raise ConfigError(
            f"{source}: [workers] installs must be tables of path, lock and run"
        )
    if not all(_inside(i["path"]) and _inside(i["lock"]) for i in installs):
        raise ConfigError(
            f"{source}: [workers] installs must be paths inside the repository"
        )
    if {Path(i["path"]) for i in installs} & {Path(d) for d in shared}:
        raise ConfigError(
            f"{source}: [workers] installs and shared_dirs must not name the"
            " same folder"
        )


#: A model's alias as `claude --model` takes it: never an option.
MODEL_NAME = r"[A-Za-z0-9][A-Za-z0-9._\[\]-]*"


def _level(workers: dict[str, Any], source: Path) -> LevelRules:
    """`[workers] model`, `effort`, `models` and the two label prefixes
    (#2251): a default outside its list is a worker started at a level
    nobody chose — refused at load, like any other key."""
    rules = LevelRules(
        model=workers["model"],
        effort=workers["effort"],
        models=tuple(workers["models"]),
        model_prefix=workers["model_label_prefix"],
        effort_prefix=workers["effort_label_prefix"],
    )
    if not all(re.fullmatch(MODEL_NAME, m) for m in rules.models):
        raise ConfigError(f"{source}: [workers] models must be model aliases")
    if rules.model not in rules.models:
        raise ConfigError(
            f"{source}: [workers] model must be one of models {list(rules.models)},"
            f" not {rules.model!r}"
        )
    if rules.effort not in EFFORTS:
        raise ConfigError(
            f"{source}: [workers] effort must be one of {list(EFFORTS)},"
            f" not {rules.effort!r}"
        )
    model, effort = rules.model_prefix, rules.effort_prefix
    if model.startswith(effort) or effort.startswith(model):
        raise ConfigError(
            f"{source}: [workers] model_label_prefix and effort_label_prefix"
            " must not start one another"
        )
    return rules


def _path(value: str) -> Path:
    return Path(value).expanduser()


def parse(data: dict[str, Any], source: Path, env: Mapping[str, str]) -> Settings:
    checked = _checked(data, source)
    github, git, workers = checked["github"], checked["git"], checked["workers"]
    install, paths, serve = checked["install"], checked["paths"], checked["serve"]
    quotas, history = checked["quotas"], checked["history"]
    try:
        since = None if "since" not in history else date.fromisoformat(history["since"])
    except ValueError as exc:
        raise ConfigError(
            f"{source}: [history] since must be a date (YYYY-MM-DD)"
        ) from exc
    return Settings(
        config_file=source,
        github_repo=github["repo"],
        ready_label=github["ready_label"],
        launch_label=github["launch_label"],
        priority_label=github["priority_label"],
        star_label=github["star_label"],
        progress_label=github["progress_label"],
        human_label=github["human_label"],
        parent_keywords=tuple(github["parent_keywords"]),
        blocker_keywords=tuple(github["blocker_keywords"]),
        runner_name=github["runner"],
        deploy_workflow=github["deploy_workflow"],
        carried_labels=tuple(github.get("carried_labels", ())),
        operator_login=github["operator_login"],
        worker_logins=tuple(github["worker_logins"]),
        natures=Natures(**checked["natures"]),
        repo_dir=_path(git["repo_dir"]),
        remote=git["remote"],
        base_branch=git["base_branch"],
        tmux_session=workers["tmux_session"],
        conventions=Conventions(
            window_prefix=workers["window_prefix"],
            branch_prefix=workers["branch_prefix"],
            worktree_root=_path(workers["worktree_root"]),
            **(
                {"scratch_dir": workers["scratch_dir"]}
                if "scratch_dir" in workers
                else {}
            ),
            **(
                {"scope_prefix": workers["scope_prefix"]}
                if "scope_prefix" in workers
                else {}
            ),
        ),
        operator=workers["operator"],
        primer_rules=tuple(workers["primer_rules"]),
        shared_dirs=tuple(workers.get("shared_dirs", ())),
        installs=tuple(Install(**i) for i in workers.get("installs", ())),
        hooks_dir=workers.get("hooks_dir"),
        worker_slice=workers.get("systemd_slice"),
        worker_memory_max_gb=workers.get("memory_max", WORKER_MEMORY_MAX_GB),
        worker_test_commands=tuple(workers["test_commands"]),
        worker_test_max_minutes=workers.get(
            "test_max_minutes", WORKER_TEST_MAX_MINUTES
        ),
        worker_background_max_minutes=workers.get(
            "background_max_minutes", WORKER_BACKGROUND_MAX_MINUTES
        ),
        disallowed_tools=tuple(workers["disallowed_tools"]),
        worker_token_file=_path(workers["github_token_file"]),
        guard_probe_tool=workers["guard_probe_tool"],
        stripped_env=tuple(workers["stripped_env"]),
        in_flight=_at_least_one(workers.get("in_flight", IN_FLIGHT), source),
        level=_level(workers, source),
        source_dir=install["source_dir"],
        config_in_repo=install["config_in_repo"],
        state_file=_path(paths["state_file"]),
        cache_dir=_path(paths["cache_dir"]),
        install_dir=_path(paths["install_dir"]),
        bin_dir=_path(paths["bin_dir"]),
        relauncher_log=_path(paths["relauncher_log"]),
        relauncher_timer=paths["relauncher_timer"],
        serve_host=serve["host"],
        serve_port=serve["port"],
        serve_secret_file=_path(serve["secret_file"]),
        serve_service=serve["service"],
        claude_quota_file=_path(quotas["claude_file"]),
        claude_statusline=quotas["claude_statusline"],
        billing_token_file=_path(quotas["billing_token_file"]),
        actions_included_minutes=quotas["actions_included_minutes"],
        budget_variable=quotas.get("budget_variable"),
        history_db=_path(history["db"]),
        history_since=since,
        health_log=_path(history["health_log"]),
        reaper_log=_path(history["reaper_log"]) if "reaper_log" in history else None,
        ci_workflow=history["ci_workflow"],
        backup_dir=_path(history["backup_dir"]),
        actions_slope_days=history.get("actions_slope_days", ACTIONS_SLOPE_DAYS),
        actions_half_life_hours=history.get(
            "actions_half_life_hours", ACTIONS_HALF_LIFE_HOURS
        ),
        claude_week_slope_days=history.get(
            "claude_week_slope_days", CLAUDE_WEEK_SLOPE_DAYS
        ),
        claude_week_half_life_hours=history.get(
            "claude_week_half_life_hours", CLAUDE_WEEK_HALF_LIFE_HOURS
        ),
        merge_queue=MergeQueue(
            **{
                **checked["merge_queue"],
                "merge_error_passes": _passes(
                    checked["merge_queue"].get("merge_error_passes", 3), source
                ),
                "reuse_ignore": tuple(checked["merge_queue"].get("reuse_ignore") or ()),
                "code_steps": tuple(checked["merge_queue"].get("code_steps") or ()),
                "flaky_max_tests": _not_negative(
                    checked["merge_queue"].get("flaky_max_tests", 0), source
                ),
                **{
                    key: _positive(
                        checked["merge_queue"].get(key, default), key, source
                    )
                    for key, default in (
                        ("flaky_bug_times", 2),
                        ("flaky_bug_days", 7),
                        ("flaky_bug_close_days", 14),
                    )
                },
            }
        ),
        waits=_waits(checked["waits"], source),
        alerts_chat_id=checked["alerts"]["telegram_chat_id"],
        alerts_token_file=_path(checked["alerts"]["telegram_token_file"]),
        mcp_units=tuple(checked["mcp"].get("units", ())),
        mcp_workflows=tuple(checked["mcp"].get("workflows", ())),
        tmux_socket=env.get("TRACERWORKS_TMUX_SOCKET") or None,
        meminfo=Path(env.get("TRACERWORKS_MEMINFO", "/proc/meminfo")),
        proc=Path(env.get("TRACERWORKS_PROC", "/proc")),
        systemd_dir=_path(env.get("TRACERWORKS_SYSTEMD_DIR", "~/.config/systemd/user")),
    )


def load(explicit: str | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    path = config_path(explicit, env)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{path}: no project configuration (--config, $TRACERWORKS_CONFIG)"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse(data, path, env)
