"""The project file: required, complete, strict (this project's own one is
proved in `tests/tracerworks_project`)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from tracerworks.cli import EXIT_BROKEN, main
from tracerworks.settings import ConfigError, config_path, parse

TEST_FILE = Path(__file__).parents[1] / "fixtures" / "tracerworks" / "project.toml"


def data() -> dict[str, Any]:
    return tomllib.loads(TEST_FILE.read_text(encoding="utf-8"))


def test_no_worker_login_yet_is_a_valid_empty_list() -> None:
    d = data()
    d["github"]["worker_logins"] = []
    assert parse(d, TEST_FILE, {}).trusted_logins == ("operator-login",)
    d["github"]["worker_logins"] = ["factory-bot"]
    assert parse(d, TEST_FILE, {}).trusted_logins == ("operator-login", "factory-bot")


def test_a_project_without_shared_folders_leaves_the_key_out() -> None:
    d = data()
    d["workers"].pop("shared_dirs", None)
    assert parse(d, TEST_FILE, {}).shared_dirs == ()


def test_a_worktree_install_is_read_and_may_be_left_out() -> None:
    d = data()
    d["workers"]["installs"] = [
        {"path": "web/node_modules", "lock": "web/package-lock.json", "run": "npm ci"}
    ]
    [web] = parse(d, TEST_FILE, {}).installs
    assert (web.path, web.lock, web.run) == (
        "web/node_modules",
        "web/package-lock.json",
        "npm ci",
    )
    d["workers"].pop("installs")
    assert parse(d, TEST_FILE, {}).installs == ()


def test_paths_expand_and_machine_overrides_come_from_the_environment() -> None:
    settings = parse(data(), TEST_FILE, {"TRACERWORKS_TMUX_SOCKET": "tracerworks-test"})
    assert settings.tmux_socket == "tracerworks-test"
    assert parse(data(), TEST_FILE, {}).tmux_socket is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("github"), "missing section [github]"),
        (lambda d: d.update(extra={}), "unknown section"),
        (lambda d: d["git"].update(branch="main"), "unknown key(s) in [git]"),
        (lambda d: d["git"].pop("remote"), "[git] remote must be a non-empty string"),
        (lambda d: d["workers"].update(tmux_session="  "), "tmux_session must be"),
        (lambda d: d["workers"].update(primer_rules=[]), "primer_rules must be"),
        (
            lambda d: d["workers"].update(shared_dirs=["/etc"]),
            "[workers] shared_dirs must be paths inside the repository",
        ),
        (
            lambda d: d["workers"].update(shared_dirs=["."]),
            "[workers] shared_dirs must be paths inside the repository",
        ),
        (
            lambda d: d["workers"].update(shared_dirs=["front/../../x"]),
            "[workers] shared_dirs must be paths inside the repository",
        ),
        (
            lambda d: d["workers"].update(installs=["npm ci"]),
            "[workers] installs must be tables of path, lock and run",
        ),
        (
            lambda d: d["workers"].update(
                installs=[{"path": "a/node_modules", "lock": "a/package-lock.json"}]
            ),
            "[workers] installs must be tables of path, lock and run",
        ),
        (
            lambda d: d["workers"].update(
                installs=[{"path": "/x", "lock": "a/package-lock.json", "run": "n"}]
            ),
            "[workers] installs must be paths inside the repository",
        ),
        (
            lambda d: d["workers"].update(
                installs=[
                    {
                        "path": "front/node_modules",
                        "lock": "front/package-lock.json",
                        "run": "npm ci",
                    }
                ]
            ),
            "[workers] installs and shared_dirs must not name the same folder",
        ),
        (
            lambda d: d["github"].update(parent_keywords=["ok", 3]),
            "parent_keywords must be",
        ),
        (
            lambda d: d["serve"].update(port="8787"),
            "[serve] port must be a whole number from 1 to 65535",
        ),
        (
            lambda d: d["serve"].update(port=True),
            "[serve] port must be a whole number from 1 to 65535",
        ),
        (
            lambda d: d["serve"].update(port=70000),
            "[serve] port must be a whole number from 1 to 65535",
        ),
        (
            lambda d: d["quotas"].update(claude_statusline="yes"),
            "[quotas] claude_statusline must be true or false",
        ),
        (
            lambda d: d["quotas"].update(actions_included_minutes=0),
            "[quotas] actions_included_minutes must be a whole number",
        ),
        (
            lambda d: d["history"].update(since="01/09/2026"),
            "[history] since must be a date (YYYY-MM-DD)",
        ),
        (lambda d: d["history"].pop("db"), "[history] db must be a non-empty string"),
        (lambda d: d.pop("natures"), "missing section [natures]"),
        # #1529: who is trusted, and what a worker is denied — all required
        (
            lambda d: d["github"].pop("operator_login"),
            "[github] operator_login must be a non-empty string",
        ),
        (
            lambda d: d["github"].pop("worker_logins"),
            "[github] worker_logins must be a list of strings",
        ),
        (
            lambda d: d["github"].update(worker_logins=[" "]),
            "[github] worker_logins must be a list of strings",
        ),
        (
            lambda d: d["workers"].pop("disallowed_tools"),
            "[workers] disallowed_tools must be a non-empty list of strings",
        ),
        (
            lambda d: d["workers"].pop("guard_probe_tool"),
            "[workers] guard_probe_tool must be a non-empty string",
        ),
        # #2251: the level a worker starts at, never one nobody chose
        (
            lambda d: d["workers"].update(effort="ultra"),
            "[workers] effort must be one of ['low', 'medium', 'high', 'xhigh',"
            " 'max'], not 'ultra'",
        ),
        (
            lambda d: d["workers"].update(model="huge"),
            "[workers] model must be one of models ['big', 'small'], not 'huge'",
        ),
        (
            lambda d: d["workers"].update(models=["--danger"]),
            "[workers] models must be model aliases",
        ),
        (
            lambda d: d["workers"].pop("effort"),
            "[workers] effort must be a non-empty string",
        ),
        (
            lambda d: d["workers"].update(effort_label_prefix="llm-x"),
            "[workers] model_label_prefix and effort_label_prefix must not start"
            " one another",
        ),
        (
            lambda d: d["workers"].pop("stripped_env"),
            "[workers] stripped_env must be a list of strings",
        ),
        (
            lambda d: d["workers"].update(stripped_env=["A=1"]),
            "[workers] stripped_env must be environment variable names",
        ),
        (
            lambda d: d["workers"].pop("github_token_file"),
            "[workers] github_token_file must be a non-empty string",
        ),
        (
            lambda d: d["workers"].update(disallowed_tools=[]),
            "[workers] disallowed_tools must be a non-empty list of strings",
        ),
        (
            lambda d: d["natures"].pop("idea"),
            "[natures] idea must be a non-empty string",
        ),
    ],
)
def test_an_incomplete_or_mistyped_file_is_refused(mutate: Any, message: str) -> None:
    broken = data()
    mutate(broken)
    with pytest.raises(ConfigError, match=re_escape(message)):
        parse(broken, TEST_FILE, {})


def re_escape(text: str) -> str:
    import re

    return re.escape(text)


def test_where_the_file_is_looked_for() -> None:
    assert config_path("/x/p.toml", {"TRACERWORKS_CONFIG": "/y"}) == Path("/x/p.toml")
    assert config_path(None, {"TRACERWORKS_CONFIG": "/y/p.toml"}) == Path("/y/p.toml")
    assert config_path(None, {"HOME": "/h"}) == Path(
        "/h/.config/tracerworks/project.toml"
    )


def test_no_project_file_is_a_loud_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "absent.toml"), "status"]) == EXIT_BROKEN
    assert "no project configuration" in capsys.readouterr().err


def test_without_since_the_history_starts_with_the_project() -> None:
    """#851: no date in the file — the backfill takes all GitHub has."""
    without = data()
    without["history"].pop("since")
    assert parse(without, TEST_FILE, {}).history_since is None
    assert parse(data(), TEST_FILE, {}).history_since is not None


def test_the_slopes_windows_are_set_apart_and_default_to_operator_s() -> None:
    """#864: Actions and the Claude week over a week, the latest days weighing
    more — half as much every 48 h back (the operator, 05/10). Each its own
    `[history]` key; a file that leaves them out gets those."""
    default = parse(data(), TEST_FILE, {})
    assert (
        default.actions_slope_days,
        default.actions_half_life_hours,
        default.claude_week_slope_days,
        default.claude_week_half_life_hours,
    ) == (7, 48, 7, 48)
    set_apart = data()
    set_apart["history"].update(
        actions_slope_days=10,
        actions_half_life_hours=6,
        claude_week_slope_days=3,
        claude_week_half_life_hours=12,
    )
    settings = parse(set_apart, TEST_FILE, {})
    assert (
        settings.actions_slope_days,
        settings.actions_half_life_hours,
        settings.claude_week_slope_days,
        settings.claude_week_half_life_hours,
    ) == (10, 6, 3, 12)
    broken = data()
    broken["history"].update(claude_week_slope_days=0)
    with pytest.raises(ConfigError, match="claude_week_slope_days must be"):
        parse(broken, TEST_FILE, {})
    broken = data()
    broken["history"].update(claude_week_half_life_hours=0)
    with pytest.raises(ConfigError, match="claude_week_half_life_hours must be"):
        parse(broken, TEST_FILE, {})


def test_a_worker_s_memory_cap_is_read_in_gb_and_defaults_to_3() -> None:
    """#2095: 3 GB a worker (peaks of 1.8 to 2.3 GB measured 29/10), under the
    6 GB of `agents.slice`."""
    d = data()
    d["workers"].pop("memory_max", None)
    assert parse(d, TEST_FILE, {}).worker_memory_max_gb == 3
    d["workers"]["memory_max"] = 4
    assert parse(d, TEST_FILE, {}).worker_memory_max_gb == 4
    d["workers"]["memory_max"] = "3G"
    with pytest.raises(ConfigError, match=re_escape("[workers] memory_max")):
        parse(d, TEST_FILE, {})


def test_a_project_without_a_scratch_folder_gets_the_default() -> None:
    d = data()
    d["workers"].pop("scratch_dir", None)
    assert parse(d, TEST_FILE, {}).conventions.scratch_dir == ".tmp"


def test_a_worker_s_scope_is_named_by_the_project_or_after_the_tool() -> None:
    """#2350: the scope's first word is the project's (`[workers]
    scope_prefix`), the tool's own name when the file leaves it out."""
    d = data()
    d["workers"].pop("scope_prefix", None)
    assert (
        parse(d, TEST_FILE, {}).conventions.unit_name(600) == "tracerworks-worker-600"
    )
    d["workers"]["scope_prefix"] = "shop-"
    assert parse(d, TEST_FILE, {}).conventions.unit_name(600) == "shop-worker-600"


@pytest.mark.parametrize("value", ["a/b", "/tmp", "..", ".", "../x"])
def test_the_scratch_folder_is_one_name_in_the_worktree(value: str) -> None:
    d = data()
    d["workers"]["scratch_dir"] = value
    with pytest.raises(ConfigError, match="scratch_dir"):
        parse(d, TEST_FILE, {})


def test_a_worker_s_background_wait_defaults_to_an_hour_and_is_set() -> None:
    """#2175: a worker at rest on its own background job is not parked, up to
    `[workers] background_max_minutes`."""
    d = data()
    d["workers"].pop("background_max_minutes", None)
    assert parse(d, TEST_FILE, {}).worker_background_max_minutes == 60
    d["workers"]["background_max_minutes"] = 20
    assert parse(d, TEST_FILE, {}).worker_background_max_minutes == 20


def test_a_worker_s_test_commands_are_required_and_their_limit_defaults_to_30() -> None:
    """#2096: what a worker's test runs is the project's to say; 30 min, the
    longest integration slice (27 min) runs on a runner, not in a worker."""
    d = data()
    d["workers"].pop("test_max_minutes", None)
    d["waits"].pop("low_memory_minutes", None)
    settings = parse(d, TEST_FILE, {})
    assert settings.worker_test_max_minutes == 30
    assert settings.waits.low_memory_minutes == 15
    d["workers"].pop("test_commands")
    with pytest.raises(ConfigError, match=re_escape("[workers] test_commands")):
        parse(d, TEST_FILE, {})


def test_the_remote_session_s_door_section_may_be_left_out() -> None:
    """`[mcp]` (#2185): a project without a remote session has none; its
    units are systemd names, never an option handed to `systemctl`."""
    d = data()
    d.pop("mcp", None)
    assert parse(d, TEST_FILE, {}).mcp_units == ()
    d["mcp"] = {"units": ["serve-x.service", "next-x.timer", "agents.slice"]}
    assert parse(d, TEST_FILE, {}).mcp_units == (
        "serve-x.service",
        "next-x.timer",
        "agents.slice",
    )


@pytest.mark.parametrize(
    "name", ["--ref", "deploy", "a b.yml", "../ci.yml", "x/ci.yml"]
)
def test_a_door_workflow_is_a_workflow_file_name(name: str) -> None:
    d = data()
    d["mcp"] = {"workflows": [name]}
    with pytest.raises(ConfigError, match=re_escape("[mcp] workflows")):
        parse(d, TEST_FILE, {})


@pytest.mark.parametrize("unit", ["--all", "serve", "*.service", "a b.service"])
def test_a_door_unit_is_a_systemd_unit_name(unit: str) -> None:
    d = data()
    d["mcp"] = {"units": [unit]}
    with pytest.raises(ConfigError, match=re_escape("[mcp] units")):
        parse(d, TEST_FILE, {})
