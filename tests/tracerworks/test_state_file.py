"""The operator's state file as the Settings tab writes it (#2190): bounds on
writing as on reading, a key never written follows the default, an atomic
write, and the command line that never loses what the tab set."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracerworks.state_file import (
    TUNABLES,
    State,
    StateFileError,
    assign,
    default_of,
    load,
    parse,
    render,
    save,
    update,
    value_of,
)


def keys(text: str) -> list[str]:
    return [
        line.split(":")[0].strip()
        for line in text.splitlines()
        if line and not line.startswith("#")
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_workers", 1),
        ("max_workers", 3),
        ("min_available_memory_mb", 1500),
        ("min_available_memory_mb_runner_busy", 6000),
        ("claude.weekly_cap", 50),
        ("claude.weekly_cap", 100),
    ],
)
def test_each_setting_round_trips_within_its_bounds(key: str, value: int) -> None:
    state = assign(State(), {key: value})
    read = parse(render(state))
    assert value_of(read, key) == value
    assert key in read.written


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_workers", 0),
        ("max_workers", 4),
        ("min_available_memory_mb", 1499),
        ("min_available_memory_mb_runner_busy", 0),
        ("claude.weekly_cap", 49),
        ("claude.weekly_cap", 101),
        ("max_workers", "2"),
        ("max_workers", True),
        ("max_workers", 2.0),
    ],
)
def test_a_value_out_of_its_bounds_is_refused_with_its_reason(
    key: str, value: object
) -> None:
    with pytest.raises(StateFileError, match=key.replace(".", r"\.")):
        assign(State(), {key: value})


@pytest.mark.parametrize(
    "text",
    [
        "max_workers: 4\n",
        "min_available_memory_mb: 1000\n",
        "min_available_memory_mb_runner_busy: 1499\n",
        "claude:\n  weekly_cap: 40\n",
    ],
)
def test_a_value_out_of_its_bounds_in_the_file_is_a_loud_error(text: str) -> None:
    with pytest.raises(StateFileError, match="must be a whole number"):
        parse(text)


def test_an_unknown_setting_is_refused() -> None:
    with pytest.raises(StateFileError, match="unknown setting"):
        assign(State(), {"max_worker": 2})


def test_a_key_never_written_stays_out_and_follows_the_default() -> None:
    assert keys(render(State())) == []
    state = assign(State(), {"max_workers": 1})
    assert keys(render(state)) == ["max_workers"]


def test_a_setting_at_its_default_value_is_the_default() -> None:
    """Story 22: it then follows a PR that changes the code's default."""
    state = assign(State(), {"max_workers": 1})
    back = assign(state, {"max_workers": default_of("max_workers")})
    assert keys(render(back)) == []
    assert "max_workers" not in back.written


def test_none_removes_the_key_and_the_default_comes_back() -> None:
    state = parse("max_workers: 1\nmin_available_memory_mb: 3000\n")
    back = assign(state, {"max_workers": None})
    assert back.max_workers == State().max_workers
    assert keys(render(back)) == ["min_available_memory_mb"]


def test_a_file_written_before_follows_the_defaults_again() -> None:
    """The file `tracerworks` wrote until #2190 held every key, the tab's
    settings at their defaults: those are the defaults, not set by the file;
    what the command line declared stays."""
    before = (
        "provider: claude\nclaude:\n  weekly_used: null\n  weekly_cap: 80\n"
        "codex:\n  available: false\nmax_workers: 2\n"
        "min_available_memory_mb: 2500\nmin_available_memory_mb_runner_busy: 3500\n"
    )
    read = parse(before)
    assert read.written == {"provider", "claude.weekly_used", "codex.available"}
    assert keys(render(read)) == [
        "provider",
        "claude",
        "weekly_used",
        "codex",
        "available",
    ]
    assert parse(render(read)) == read


def test_every_tunable_has_its_bounds_and_its_default_within_them() -> None:
    for tunable in TUNABLES:
        default = default_of(tunable.key)
        if tunable.kind == "bool":
            assert isinstance(default, bool), tunable.key
            continue
        assert tunable.minimum is not None, tunable.key
        if default is None:  # optional: the project file's default holds
            continue
        assert isinstance(default, int)
        assert default >= tunable.minimum
        assert tunable.maximum is None or default <= tunable.maximum


def test_the_queue_pause_round_trips_and_is_off_by_default() -> None:
    assert State().queue_paused is False
    paused = parse(render(assign(State(), {"queue.paused": True})))
    assert paused.queue_paused is True
    assert keys(render(paused)) == ["queue", "paused"]
    with pytest.raises(StateFileError, match=r"queue\.paused must be true or false"):
        assign(State(), {"queue.paused": 1})
    with pytest.raises(StateFileError, match="expected true or false"):
        parse("queue:\n  paused: yes\n")


@pytest.mark.parametrize("minutes", [0, 3000, 20000])
def test_the_included_minutes_round_trip_within_their_bounds(minutes: int) -> None:
    read = parse(render(assign(State(), {"github.actions_included_minutes": minutes})))
    assert read.github_actions_included_minutes == minutes


def test_the_included_minutes_are_optional_and_bounded() -> None:
    assert State().github_actions_included_minutes is None, "the project file's"
    with pytest.raises(StateFileError, match="from 0 to 20000"):
        assign(State(), {"github.actions_included_minutes": 20001})
    with pytest.raises(StateFileError, match="from 0 to 20000"):
        parse("github:\n  actions_included_minutes: 30000\n")
    back = assign(
        assign(State(), {"github.actions_included_minutes": 5000}),
        {"github.actions_included_minutes": None},
    )
    assert back.github_actions_included_minutes is None
    assert keys(render(back)) == []


def test_a_failed_write_leaves_the_last_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.yaml"
    save(path, assign(State(), {"max_workers": 1}))
    before = path.read_text()

    def broken(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        save(path, assign(State(), {"max_workers": 3}))
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["state.yaml"], "no stray file"


def test_update_starts_again_from_the_file_read_back(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    path.write_text("max_workers: 1\n")  # written by another hand meanwhile
    before, after = update(path, lambda s: assign(s, {"claude.weekly_cap": 90}))
    assert before.max_workers == 1
    assert (after.max_workers, after.claude_weekly_cap) == (1, 90)
    assert load(path) == after


def test_the_alert_switches_are_on_by_default_and_round_trip() -> None:
    assert (State().alerts_telegram, State().alerts_off) == (True, frozenset())
    state = assign(
        State(), {"alerts.telegram": False, "alerts.disk": False, "alerts.ci": False}
    )
    read = parse(render(state))
    assert (read.alerts_telegram, read.alerts_off) == (False, frozenset({"disk", "ci"}))
    assert value_of(read, "alerts.disk") is False
    assert value_of(read, "alerts.stray") is True
    back = assign(read, {"alerts.disk": None, "alerts.ci": True})
    assert back.alerts_off == frozenset()
    assert "disk" not in render(back) and "ci:" not in render(back), "on: the default"


def test_every_alert_family_has_its_switch_but_the_workers_token() -> None:
    from tracerworks.alerts import FAMILIES

    keys = {t.key for t in TUNABLES if t.block == "alerts"}
    assert keys == {"alerts.telegram"} | {f"alerts.{f.key}" for f in FAMILIES}
    with pytest.raises(StateFileError, match="unknown setting"):
        assign(State(), {"alerts.worker_token": False})
    with pytest.raises(StateFileError, match="unknown key"):
        parse("alerts:\n  worker_token: false\n")
    with pytest.raises(StateFileError, match="expected true or false"):
        parse("alerts:\n  disk: off\n")
