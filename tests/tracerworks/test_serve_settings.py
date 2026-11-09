"""`tracerworks serve`: the Settings tab's two routes (#2190).

`GET /settings.json` says each setting's value in force, default and bounds;
`POST /settings` writes the operator's state file, under the guards of the
star (#1877): password and its brake, action header, origin, JSON of 2 KB at
most, one write a second. Tested as plain calls to `Dashboard.handle`, the
state file a real one in a temporary folder.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.test_serve import BAD, GOOD, PAGE, SECRET, Builds, Clock
from tracerworks.serve import Dashboard, Response, SnapshotCache, make_server
from tracerworks.state_file import TUNABLES, load

ACTION = {"X-Agentctl-Action": "settings", "Content-Type": "application/json"}
HOST = "factory.example"


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.state = tmp_path / "config" / "state.yaml"
        self.journal: list[str] = []
        self.builds = Builds()
        cache = SnapshotCache(
            self.builds, self.clock, store=tmp_path / "d.json", spawn=lambda job: job()
        )
        self.dashboard = Dashboard(
            SECRET,
            PAGE,
            cache,
            self.clock,
            log=self.journal.append,
            state=self.state,
            defaults={"github.actions_included_minutes": 3000},
        )

    def post(
        self,
        payload: object = None,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        auth: str | None = GOOD,
    ) -> Response:
        raw = body if body is not None else json.dumps(payload).encode()
        sent = ACTION if headers is None else headers
        self.clock.now += 1  # past the write floor, unless a test says otherwise
        return self.dashboard.handle("POST", "/settings", auth, sent, raw)

    def get(self) -> dict[str, Any]:
        answer = self.dashboard.handle("GET", "/settings.json", GOOD)
        assert answer.status == 200, answer.body
        data: dict[str, Any] = json.loads(answer.body)
        return data


def by_key(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["key"]: s for s in data["settings"]}


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_get_says_value_default_and_bounds_of_each_setting(
    world: World,
) -> None:
    data = world.get()
    assert data["blocks"] == [
        {"key": "launches", "label": "Launches"},
        {"key": "quotas", "label": "Quotas"},
        {"key": "alerts", "label": "Alerts"},
    ]
    settings = by_key(data)
    assert settings["max_workers"] == {
        "key": "max_workers",
        "block": "launches",
        "label": "Workers at most",
        "kind": "int",
        "unit": "",
        "value": 2,
        "default": 2,
        "minimum": 1,
        "maximum": 3,
        "set": False,
    }
    memory, cap = settings["min_available_memory_mb"], settings["claude.weekly_cap"]
    assert (memory["minimum"], memory["maximum"]) == (1500, None)
    assert settings["min_available_memory_mb_runner_busy"]["default"] == 3500
    assert (cap["minimum"], cap["maximum"]) == (50, 100)
    assert list(settings) == [t.key for t in TUNABLES]
    paused = settings["queue.paused"]
    assert (paused["kind"], paused["value"], paused["default"]) == (
        "bool",
        False,
        False,
    )
    minutes = settings["github.actions_included_minutes"]
    assert (minutes["block"], minutes["minimum"], minutes["maximum"]) == (
        "quotas",
        0,
        20000,
    )
    # absent from the file: the project file's value, said as the default
    assert (minutes["value"], minutes["default"], minutes["set"]) == (3000, 3000, False)


def test_the_tab_s_minutes_win_over_the_project_file_s(world: World) -> None:
    assert world.post({"github.actions_included_minutes": 5000}).status == 200
    minutes = by_key(world.get())["github.actions_included_minutes"]
    assert (minutes["value"], minutes["default"], minutes["set"]) == (5000, 3000, True)
    assert world.post({"github.actions_included_minutes": None}).status == 200
    assert world.journal == [
        "setting github.actions_included_minutes: 3000 → 5000",
        "setting github.actions_included_minutes: 5000 → 3000 (default)",
    ]


def test_the_pause_is_a_switch_and_rebuilds_the_snapshot(world: World) -> None:
    builds = world.builds.count
    assert world.post({"queue.paused": True}).status == 200
    assert load(world.state).queue_paused is True
    assert world.journal == ["setting queue.paused: off → on"]
    assert world.builds.count == builds + 1, "the banner, at once"
    assert world.post({"queue.paused": 1}).status == 400
    assert world.post({"max_workers": True}).status == 400


def test_get_reads_the_file_on_each_request(world: World) -> None:
    world.state.parent.mkdir(parents=True)
    world.state.write_text("max_workers: 1\n")
    one = by_key(world.get())["max_workers"]
    assert (one["value"], one["set"]) == (1, True)


def test_a_broken_state_file_is_503_with_its_reason(world: World) -> None:
    world.state.parent.mkdir(parents=True)
    world.state.write_text("max_workers: 9\n")
    answer = world.dashboard.handle("GET", "/settings.json", GOOD)
    assert answer.status == 503
    assert b"max_workers must be a whole number from 1 to 3" in answer.body
    assert world.post({"max_workers": 2}).status == 503
    assert world.state.read_text() == "max_workers: 9\n"


def test_a_save_writes_the_file_journals_it_and_answers_the_new_values(
    world: World,
) -> None:
    answer = world.post({"max_workers": 1, "claude.weekly_cap": 90})
    assert answer.status == 200, answer.body
    shown = by_key(json.loads(answer.body))
    assert (shown["max_workers"]["value"], shown["max_workers"]["set"]) == (1, True)
    state = load(world.state)
    assert (state.max_workers, state.claude_weekly_cap) == (1, 90)
    assert world.journal == [
        "setting max_workers: 2 → 1",
        "setting claude.weekly_cap: 80 → 90",
    ]


def test_null_is_back_to_the_default_and_removes_the_key(world: World) -> None:
    assert world.post({"min_available_memory_mb": 4000}).status == 200
    assert world.post({"min_available_memory_mb": None}).status == 200
    assert "min_available_memory_mb" not in world.state.read_text()
    assert by_key(world.get())["min_available_memory_mb"]["set"] is False
    assert world.journal[-1] == "setting min_available_memory_mb: 4000 → 2500 (default)"


def test_a_value_as_it_was_writes_no_journal_line(world: World) -> None:
    assert world.post({"max_workers": 1}).status == 200
    assert world.post({"max_workers": 1}).status == 200
    assert world.journal == ["setting max_workers: 2 → 1"]


@pytest.mark.parametrize(
    ("payload", "said"),
    [
        ({"max_workers": 4}, b"max_workers must be a whole number from 1 to 3"),
        ({"min_available_memory_mb": 1000}, b"at least 1500"),
        ({"claude.weekly_cap": 101}, b"from 50 to 100"),
        ({"max_workers": 2, "claude.weekly_cap": 10}, b"claude.weekly_cap"),
    ],
)
def test_a_value_out_of_its_bounds_is_400_and_writes_nothing(
    world: World, payload: dict[str, int], said: bytes
) -> None:
    world.state.parent.mkdir(parents=True)
    world.state.write_text("max_workers: 1\n")
    answer = world.post(payload)
    assert answer.status == 400
    assert said in answer.body
    assert world.state.read_text() == "max_workers: 1\n"
    assert world.journal == []


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({**ACTION, "Content-Type": "text/plain"}, b'{"max_workers": 1}'),
        (ACTION, b"not json"),
        (ACTION, b"{}"),
        (ACTION, b"[1]"),
        (ACTION, b'{"max_workers": "1"}'),
        (ACTION, b'{"max_workers": 1.5}'),
        (ACTION, b'{"max_workers": true}'),
        (ACTION, b'{"provider": "codex"}'),  # its own door: `tracerworks provider`
        (ACTION, b'{"claude.weekly_used": 10}'),
        (ACTION, b'{"max_workers": 1}' + b" " * 2048),
    ],
)
def test_a_body_other_than_settings_is_400(
    world: World, headers: dict[str, str], body: bytes
) -> None:
    assert world.post(headers=headers, body=body).status == 400
    assert not world.state.exists()


def test_settings_share_the_password_and_its_brake(world: World) -> None:
    assert world.post({"max_workers": 1}, auth=None).status == 401
    assert world.post({"max_workers": 1}, auth=BAD).status == 401
    world.clock.now -= 1  # still within the brake
    assert world.post({"max_workers": 1}).status == 429
    assert world.dashboard.handle("GET", "/settings.json", GOOD).status == 429
    assert not world.state.exists()


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json"},
        {**ACTION, "X-Agentctl-Action": "star"},  # the star's header is not this one
        {**ACTION, "Origin": "https://evil.example", "Host": HOST},
        {**ACTION, "Origin": f"https://{HOST}"},
    ],
)
def test_a_cross_site_post_is_403(world: World, headers: dict[str, str]) -> None:
    assert world.post({"max_workers": 1}, headers=headers).status == 403
    assert not world.state.exists()


def test_the_dashboard_s_own_origin_is_accepted(world: World) -> None:
    headers = {**ACTION, "Origin": f"https://{HOST}", "Host": HOST}
    assert world.post({"max_workers": 1}, headers=headers).status == 200


def test_one_write_a_second_at_most(world: World) -> None:
    assert world.post({"max_workers": 1}).status == 200
    world.clock.now -= 0.5
    refused = world.post({"max_workers": 3})
    assert (refused.status, refused.header("Retry-After")) == (429, "1")
    assert load(world.state).max_workers == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/settings"), ("PUT", "/settings"), ("DELETE", "/settings"),
     ("POST", "/settings.json"), ("POST", "/star"), ("POST", "/start-next")],
)  # fmt: skip
def test_no_other_route_or_method_writes(world: World, method: str, path: str) -> None:
    answer = world.dashboard.handle(method, path, GOOD, ACTION, b'{"max_workers": 1}')
    assert answer.status == 405
    assert not world.state.exists()


def test_the_wrapper_reads_a_settings_body_up_to_its_limit(world: World) -> None:
    server = make_server(world.dashboard, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/settings"
        # over the star's 1 KB, under the settings' 2 KB
        body = b'{"max_workers": 1}' + b" " * 1500
        request = urllib.request.Request(
            url, data=body, method="POST", headers={"Authorization": GOOD, **ACTION}
        )
        with urllib.request.urlopen(request, timeout=5) as answer:
            assert by_key(json.loads(answer.read()))["max_workers"]["value"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_an_alert_family_is_switched_off_and_the_workers_token_is_not_one(
    world: World,
) -> None:
    alerts = [s for s in world.get()["settings"] if s["block"] == "alerts"]
    assert alerts[0]["key"] == "alerts.telegram"
    assert all(s["kind"] == "bool" and s["default"] is True for s in alerts)
    assert world.post({"alerts.disk": False}).status == 200
    assert load(world.state).alerts_off == {"disk"}
    assert world.journal == ["setting alerts.disk: on → off"]
    refused = world.post({"alerts.worker_token": False})
    assert refused.status == 400
    assert b"not a setting" in refused.body


def test_a_refused_value_takes_no_write_of_the_second(world: World) -> None:
    assert world.post({"max_workers": 9}).status == 400
    world.clock.now -= 0.5  # within the second of the refusal
    assert world.post({"max_workers": 1}).status == 200


def test_a_star_does_not_hold_a_setting_back(tmp_path: Path) -> None:
    from tests.tracerworks.fakes import FakeRunner
    from tracerworks.serve import Star

    world = World(tmp_path)
    fake = FakeRunner()
    fake.answer(
        "gh", "issue", "view", "42", "-R", "owner/repo", "--json", "state,labels",
        stdout=json.dumps({"state": "OPEN", "labels": [{"name": "starred"}]}),
    )  # fmt: skip
    world.dashboard._star = Star(fake, "owner/repo", "starred")
    star = {"X-Agentctl-Action": "star", "Content-Type": "application/json"}
    raw = b'{"number": 42, "is_starred": true}'
    assert world.dashboard.handle("POST", "/star", GOOD, star, raw).status == 200
    paused = b'{"queue.paused": true}'
    answer = world.dashboard.handle("POST", "/settings", GOOD, ACTION, paused)
    assert answer.status == 200
