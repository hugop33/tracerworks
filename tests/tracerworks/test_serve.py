"""`tracerworks serve`: the dashboard, read only, behind a password (#808).

The request function is tested without a socket — clock and secret injected:
the claim is the HTTP answer to a given request, at a given moment. One test
goes through a real socket, to prove the standard-library wrapper hands the
function what the browser sent.
"""

from __future__ import annotations

import base64
import json
import stat
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.fakes import FakeRunner
from tracerworks import store
from tracerworks.process import Result
from tracerworks.serve import (
    BRAKE_SECONDS,
    FRESH_FLOOR,
    IDLE_AFTER,
    IDLE_TTL,
    Dashboard,
    Response,
    SecretError,
    SnapshotCache,
    Star,
    make_server,
    read_secret,
)

SECRET = "correct horse battery staple"
PAGE = b"<!doctype html><title>factory</title>"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Builds:
    """A snapshot builder that counts how often it is asked."""

    def __init__(self) -> None:
        self.count = 0
        self.fail = False

    def __call__(self) -> dict[str, Any]:
        self.count += 1
        if self.fail:
            raise RuntimeError("tmux is gone")
        return {"workers": [], "build": self.count}


def basic(password: str, user: str = "sam") -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def builds() -> Builds:
    return Builds()


@pytest.fixture
def dashboard(clock: Clock, builds: Builds, tmp_path: Path) -> Dashboard:
    cache = SnapshotCache(
        builds, clock, store=tmp_path / "d.json", spawn=lambda job: job()
    )
    return Dashboard(SECRET, PAGE, cache, clock)


GOOD = basic(SECRET)
BAD = basic("guess")


# --- authentication -------------------------------------------------------------------


def test_without_a_password_the_browser_is_asked_for_one(dashboard: Dashboard) -> None:
    response = dashboard.handle("GET", "/", None)
    assert response.status == 401
    assert response.header("WWW-Authenticate").startswith("Basic ")
    assert PAGE not in response.body


def test_the_right_password_gets_the_page_and_the_json(dashboard: Dashboard) -> None:
    page = dashboard.handle("GET", "/", GOOD)
    assert (page.status, page.body) == (200, PAGE)
    assert page.header("Content-Type").startswith("text/html")
    data = dashboard.handle("GET", "/snapshot.json?t=1", GOOD)
    assert data.status == 200
    assert data.header("Content-Type").startswith("application/json")
    assert json.loads(data.body)["build"] == 1


def test_a_wrong_password_brakes_every_attempt_for_ten_seconds(
    dashboard: Dashboard, clock: Clock
) -> None:
    assert dashboard.handle("GET", "/", BAD).status == 401
    clock.now += 1
    refused = dashboard.handle("GET", "/", GOOD)  # even the right one
    assert refused.status == 429
    assert refused.header("Retry-After") == "9"
    assert dashboard.handle("GET", "/snapshot.json", BAD).status == 429
    clock.now += BRAKE_SECONDS - 1  # 10 s after the failure
    assert dashboard.handle("GET", "/", GOOD).status == 200


def test_a_braked_attempt_does_not_extend_the_brake(
    dashboard: Dashboard, clock: Clock
) -> None:
    dashboard.handle("GET", "/", BAD)
    clock.now += 5
    assert dashboard.handle("GET", "/", BAD).status == 429
    clock.now += 5
    assert dashboard.handle("GET", "/", GOOD).status == 200


def test_the_right_password_is_never_braked_on_its_own(
    dashboard: Dashboard, clock: Clock
) -> None:
    for _ in range(20):
        assert dashboard.handle("GET", "/snapshot.json", GOOD).status == 200
        clock.now += 0.1


def test_the_user_name_does_not_matter_and_garbage_is_a_failure(
    dashboard: Dashboard, clock: Clock
) -> None:
    assert dashboard.handle("GET", "/", basic(SECRET, user="")).status == 200
    assert dashboard.handle("GET", "/", "Basic !!!not-base64").status == 401
    clock.now += BRAKE_SECONDS
    assert dashboard.handle("GET", "/", "Bearer x").status == 401


# --- the surface ----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD"])
def test_nothing_but_get(dashboard: Dashboard, method: str) -> None:
    response = dashboard.handle(method, "/", GOOD)
    assert response.status == 405
    assert response.header("Allow") == "GET"


def test_an_unknown_path_is_not_found(dashboard: Dashboard) -> None:
    assert dashboard.handle("GET", "/start-next", GOOD).status == 404


def test_every_answer_forbids_caching_and_framing(dashboard: Dashboard) -> None:
    for response in (
        dashboard.handle("GET", "/", None),
        dashboard.handle("GET", "/", GOOD),
    ):
        assert response.header("Cache-Control") == "no-store"
        assert "frame-ancestors 'none'" in response.header("Content-Security-Policy")


# --- the snapshot: served at once, rebuilt behind (#820) ------------------------------


class Spawner:
    """The background thread, held: a build runs only when the test says so."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def __call__(self, job: Callable[[], None]) -> None:
        self.pending.append(job)

    def run(self) -> None:
        jobs, self.pending = self.pending, []
        for job in jobs:
            job()


@pytest.fixture
def spawner() -> Spawner:
    return Spawner()


@pytest.fixture
def cache(
    builds: Builds, clock: Clock, spawner: Spawner, tmp_path: Path
) -> SnapshotCache:
    return SnapshotCache(builds, clock, store=tmp_path / "snapshot.json", spawn=spawner)


def served(cache: SnapshotCache, clock: Clock) -> Any:
    return json.loads(
        Dashboard(SECRET, PAGE, cache, clock).handle("GET", "/snapshot.json", GOOD).body
    )


def test_the_first_request_starts_a_build_and_does_not_wait_for_it(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    first = served(cache, clock)
    assert first == {
        "built_at": None,
        "building": True,
        "build_error": None,
        "served_at": clock.now,
    }
    assert builds.count == 0 and len(spawner.pending) == 1
    spawner.run()
    assert served(cache, clock) == {
        "workers": [],
        "build": 1,
        "built_at": clock.now,
        "building": False,
        "build_error": None,
        "served_at": clock.now,
    }


def test_a_stale_snapshot_is_served_at_once_while_the_next_one_builds(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    served(cache, clock)
    spawner.run()
    built = clock.now
    clock.now += 59
    assert served(cache, clock)["building"] is False
    assert not spawner.pending  # fresh enough: no build
    clock.now += 1
    stale = served(cache, clock)
    assert (stale["build"], stale["built_at"], stale["building"]) == (1, built, True)
    assert served(cache, clock)["building"] is True
    assert len(spawner.pending) == 1  # one build at a time, however many requests
    spawner.run()
    fresh = served(cache, clock)
    assert (fresh["build"], fresh["built_at"], fresh["building"]) == (
        2,
        clock.now,
        False,
    )
    assert builds.count == 2


def test_a_failed_rebuild_keeps_the_last_snapshot_and_says_the_failure(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    """Never the old snapshot passed off as fresh: its age and the failure
    are said. And never a rebuild per refresh while GitHub or tmux fails."""
    served(cache, clock)
    spawner.run()
    built = clock.now
    builds.fail = True
    clock.now += 60
    served(cache, clock)
    spawner.run()
    after = served(cache, clock)
    assert (after["build"], after["built_at"]) == (1, built)
    assert after["build_error"] == "tmux is gone"
    clock.now += 59
    served(cache, clock)
    assert not spawner.pending
    builds.fail = False
    clock.now += 1
    served(cache, clock)
    spawner.run()
    assert served(cache, clock)["build_error"] is None


def test_without_any_snapshot_a_failure_is_an_error(
    dashboard: Dashboard,
    cache: SnapshotCache,
    clock: Clock,
    spawner: Spawner,
    builds: Builds,
) -> None:
    builds.fail = True
    served(cache, clock)
    spawner.run()
    failed = Dashboard(SECRET, PAGE, cache, clock).handle("GET", "/snapshot.json", GOOD)
    assert failed.status == 503
    assert b"tmux is gone" in failed.body


def test_the_last_snapshot_survives_a_restart_of_the_service(
    cache: SnapshotCache,
    clock: Clock,
    spawner: Spawner,
    builds: Builds,
    tmp_path: Path,
) -> None:
    served(cache, clock)
    spawner.run()
    built = clock.now
    store = tmp_path / "snapshot.json"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    clock.now += 3600
    restarted = SnapshotCache(builds, clock, store=store, spawn=spawner)
    at_once = served(restarted, clock)
    assert (at_once["build"], at_once["built_at"], at_once["building"]) == (
        1,
        built,
        True,
    )


def test_a_restart_within_the_minute_does_not_rebuild(
    cache: SnapshotCache,
    clock: Clock,
    spawner: Spawner,
    builds: Builds,
    tmp_path: Path,
) -> None:
    served(cache, clock)
    spawner.run()
    clock.now += 30
    restarted = SnapshotCache(
        builds, clock, store=tmp_path / "snapshot.json", spawn=spawner
    )
    assert served(restarted, clock)["building"] is False
    assert not spawner.pending


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"built_at": "x"}'])
def test_an_unreadable_store_is_ignored(
    clock: Clock, spawner: Spawner, builds: Builds, tmp_path: Path, content: str
) -> None:
    store = tmp_path / "snapshot.json"
    store.write_text(content, encoding="utf-8")
    cache = SnapshotCache(builds, clock, store=store, spawn=spawner)
    assert served(cache, clock)["built_at"] is None


def test_fresh_asks_a_rebuild_at_most_once_per_ten_seconds(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    """The Refresh button (#821): a newer snapshot on demand, however many
    clicks — never more than one rebuild per `FRESH_FLOOR`."""
    handle = Dashboard(SECRET, PAGE, cache, clock).handle
    handle("GET", "/snapshot.json", GOOD)
    spawner.run()
    clock.now += 3
    handle("GET", "/snapshot.json?fresh=1", GOOD)
    assert not spawner.pending  # 3 s after the last build: not heard
    clock.now += FRESH_FLOOR
    handle("GET", "/snapshot.json", GOOD)
    assert not spawner.pending  # an ordinary refresh, 13 s old: nothing
    asked_at = clock.now
    for _ in range(5):  # clicked five times, a second apart
        asked = json.loads(handle("GET", "/snapshot.json?fresh=1", GOOD).body)
        clock.now += 1
    assert asked["building"] is True
    assert len(spawner.pending) == 1
    spawner.run()
    assert builds.count == 2
    clock.now = asked_at + FRESH_FLOOR - 1
    handle("GET", "/snapshot.json?fresh=1", GOOD)
    assert not spawner.pending  # 9 s after the rebuild it started
    clock.now += 1
    handle("GET", "/snapshot.json?t=1&fresh=1", GOOD)
    assert len(spawner.pending) == 1


def test_fresh_while_building_starts_no_second_build(
    cache: SnapshotCache, clock: Clock, spawner: Spawner
) -> None:
    handle = Dashboard(SECRET, PAGE, cache, clock).handle
    handle("GET", "/snapshot.json", GOOD)
    clock.now += FRESH_FLOOR
    handle("GET", "/snapshot.json?fresh=1", GOOD)
    assert len(spawner.pending) == 1


def test_the_server_rebuilds_every_minute_while_someone_watches(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    """The operator, 30/09: arriving on the page shows a state less than a minute
    old — every 60 s while the page was asked in the last `IDLE_AFTER`."""
    served(cache, clock)
    spawner.run()
    clock.now += 59
    cache.tick()
    assert not spawner.pending
    clock.now += 1
    cache.tick()
    spawner.run()
    assert builds.count == 2
    assert served(cache, clock)["built_at"] == clock.now


def test_with_nobody_watching_the_snapshot_slows_to_idle_ttl(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    """#2196: nobody on the page, a full snapshot every `IDLE_TTL` — still
    collected, never stopped (#30/09), at a fraction of the cost."""
    assert IDLE_TTL == 10 * 60
    cache.warm()
    spawner.run()
    for _ in range(9):  # nine minutes: nothing asked of GitHub
        clock.now += 60
        cache.tick()
        assert not spawner.pending
    clock.now += 60
    cache.tick()
    spawner.run()
    assert builds.count == 2


def test_the_first_request_after_idle_rebuilds_at_once_then_every_minute(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    """The page arrives on a snapshot up to `IDLE_TTL` old: it is served at
    once, a build starts with it, and the minute is back while it watches —
    until `IDLE_AFTER` without a request."""
    cache.warm()
    spawner.run()
    clock.now += 61
    assert served(cache, clock)["building"] is True  # the old one, at once
    spawner.run()
    assert builds.count == 2
    clock.now += 60
    cache.tick()  # its page polls; the minute holds without it
    spawner.run()
    assert builds.count == 3
    clock.now += IDLE_AFTER - 61  # its last request, a second short of it
    cache.tick()
    spawner.run()
    assert builds.count == 4  # still watched: the minute's build
    clock.now += 60
    cache.tick()
    assert not spawner.pending  # nobody since: the idle pace again


def test_keep_fresh_ticks_until_stopped(clock: Clock, tmp_path: Path) -> None:
    ticks = threading.Event()
    stop = threading.Event()
    cache = SnapshotCache(lambda: {"workers": []}, clock, store=tmp_path / "s.json")
    cache.tick = ticks.set  # type: ignore[method-assign]
    thread = threading.Thread(target=cache.keep_fresh, args=(stop, 0.01))
    thread.start()
    try:
        assert ticks.wait(5)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_warm_starts_the_first_build_before_any_visitor(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    cache.warm()
    spawner.run()
    assert served(cache, clock)["build"] == 1
    assert builds.count == 1


def test_the_real_thread_answers_while_a_build_is_stuck(
    clock: Clock, tmp_path: Path
) -> None:
    """The default spawn is a real thread: a request does not wait for it."""
    release = threading.Event()

    def slow() -> dict[str, Any]:
        release.wait(5)
        return {"workers": []}

    cache = SnapshotCache(slow, clock, store=tmp_path / "s.json")
    try:
        assert served(cache, clock)["building"] is True
    finally:
        release.set()


# --- the secret -----------------------------------------------------------------------


def test_the_secret_is_read_from_a_private_file(tmp_path: Path) -> None:
    secret = tmp_path / "dashboard.secret"
    secret.write_text(f"{SECRET}\n", encoding="utf-8")
    secret.chmod(0o600)
    assert read_secret(secret) == SECRET


@pytest.mark.parametrize(
    ("content", "mode", "message"),
    [
        (SECRET, 0o644, "0600"),
        ("   \n", 0o600, "empty"),
        (None, 0o600, "no secret"),
    ],
)
def test_a_missing_empty_or_readable_secret_refuses_to_serve(
    tmp_path: Path, content: str | None, mode: int, message: str
) -> None:
    secret = tmp_path / "dashboard.secret"
    if content is not None:
        secret.write_text(content, encoding="utf-8")
        secret.chmod(mode)
    with pytest.raises(SecretError, match=message):
        read_secret(secret)


# --- through a real socket ------------------------------------------------------------


def test_the_wrapper_serves_the_function_on_a_local_socket(
    dashboard: Dashboard,
) -> None:
    server = make_server(dashboard, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url, timeout=5)
        assert denied.value.code == 401
        request = urllib.request.Request(url, headers={"Authorization": GOOD})
        with urllib.request.urlopen(request, timeout=5) as answer:
            assert answer.status == 200
            assert answer.read() == PAGE
        post = urllib.request.Request(url, data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(post, timeout=5)
        assert refused.value.code == 405
    finally:
        server.shutdown()
        server.server_close()


def test_serve_refuses_to_start_without_its_secret(tmp_path: Path) -> None:
    import io
    from dataclasses import replace

    from tests.tracerworks.fakes import FakeRunner, recorded_settings
    from tracerworks.cli import EXIT_REFUSED, Env, run

    settings = replace(
        recorded_settings(tmp_path), serve_secret_file=tmp_path / "absent.secret"
    )
    err = io.StringIO()
    env = Env(settings, runner=FakeRunner(), out=io.StringIO(), err=err)
    assert run(env, ["serve"]) == EXIT_REFUSED
    assert "no secret" in err.getvalue()


def test_serve_crashing_is_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit restarts on any exit but 1 (a refusal): a crash must not be 1."""
    import io
    from dataclasses import replace

    import tracerworks.cli as cli
    from tests.tracerworks.fakes import FakeRunner, recorded_settings
    from tracerworks.cli import EXIT_BROKEN, Env, run

    secret = tmp_path / "s"
    secret.write_text(SECRET, encoding="utf-8")
    secret.chmod(0o600)

    def boom(*_: object) -> None:
        raise RuntimeError("socket gone")

    monkeypatch.setattr(cli, "make_server", boom)
    settings = replace(recorded_settings(tmp_path), serve_secret_file=secret)
    err = io.StringIO()
    env = Env(settings, runner=FakeRunner(), out=io.StringIO(), err=err)
    assert run(env, ["serve"]) == EXIT_BROKEN
    assert "socket gone" in err.getvalue()


def test_a_tab_reads_its_data_behind_the_password(
    clock: Clock, builds: Builds, tmp_path: Path
) -> None:
    """#839: `/trends.json` is the history base shaped for the Tendances tab."""

    def broken() -> dict[str, Any]:
        raise RuntimeError("disk I/O error")

    cache = SnapshotCache(builds, clock, store=tmp_path / "d.json", spawn=lambda j: j())
    history = {
        "trends": lambda _: {"error": None, "vps": []},
        "quality": lambda _: broken(),
        "delivered": lambda query: {"error": None, "asked": query},
    }
    handle = Dashboard(SECRET, PAGE, cache, clock, history).handle
    assert handle("GET", "/trends.json", None).status == 401
    answer = handle("GET", "/trends.json", GOOD)
    assert answer.status == 200
    assert json.loads(answer.body) == {"error": None, "vps": []}
    assert answer.header("Cache-Control") == "no-store"
    failed = handle("GET", "/quality.json", GOOD)
    assert failed.status == 503 and b"disk I/O error" in failed.body
    # a panel's data carries its query (#860)
    asked = handle("GET", "/delivered.json?pr=832", GOOD)
    assert json.loads(asked.body) == {"error": None, "asked": {"pr": "832"}}
    assert handle("GET", "/delivered.json?pr=832", None).status == 401
    assert handle("GET", "/nothing.json", GOOD).status == 404
    assert handle("GET", "/trends", GOOD).status == 404


# --- the star, the dashboard's only write (#1877) -------------------------------------

STAR_REPO = "owner/repo"
ACTION = {"X-Agentctl-Action": "star", "Content-Type": "application/json"}
HOST = "factory.example"


def gh_issue_state(fake: FakeRunner, number: int, state: str, *labels: str) -> None:
    fake.answer(
        "gh", "issue", "view", str(number), "-R", STAR_REPO, "--json", "state,labels",
        stdout=json.dumps({"state": state, "labels": [{"name": n} for n in labels]}),
    )  # fmt: skip


def gh_edit(number: int, flag: str) -> tuple[str, ...]:
    return ("gh", "issue", "edit", str(number), "-R", STAR_REPO, flag, "starred")


class StarWorld:
    def __init__(self, cache: SnapshotCache, clock: Clock) -> None:
        self.fake = FakeRunner()
        self.journal: list[str] = []
        self.dashboard = Dashboard(
            SECRET,
            PAGE,
            cache,
            clock,
            star=Star(self.fake, STAR_REPO, "starred"),
            log=self.journal.append,
        )

    def post(
        self,
        payload: object = None,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        auth: str | None = GOOD,
        path: str = "/star",
    ) -> Response:
        raw = body if body is not None else json.dumps(payload).encode()
        sent = ACTION if headers is None else headers
        return self.dashboard.handle("POST", path, auth, sent, raw)

    def edits(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.fake.ran("gh", "issue", "edit")]


@pytest.fixture
def star(cache: SnapshotCache, clock: Clock) -> StarWorld:
    return StarWorld(cache, clock)


def test_a_click_puts_the_star_through_gh_and_says_it(
    star: StarWorld, spawner: Spawner
) -> None:
    gh_issue_state(star.fake, 42, "OPEN", "bug")
    star.fake.answer(*gh_edit(42, "--add-label"))
    answer = star.post({"number": 42, "is_starred": True})
    assert answer.status == 200
    assert json.loads(answer.body) == {
        "number": 42,
        "is_starred": True,
        "outcome": "put",
    }
    assert star.edits() == [gh_edit(42, "--add-label")]
    assert star.journal == ["star #42 on: put"]
    assert len(spawner.pending) == 1, "the snapshot is rebuilt after the write"


def test_a_click_removes_it_too(star: StarWorld, clock: Clock) -> None:
    gh_issue_state(star.fake, 42, "OPEN", "bug", "starred")
    star.fake.answer(*gh_edit(42, "--remove-label"))
    assert star.post({"number": 42, "is_starred": False}).status == 200
    assert star.edits() == [gh_edit(42, "--remove-label")]
    assert star.journal == ["star #42 off: removed"]


@pytest.mark.parametrize(("labels", "starred"), [(("starred",), True), ((), False)])
def test_a_star_already_as_asked_is_200_and_writes_nothing(
    star: StarWorld, spawner: Spawner, labels: tuple[str, ...], starred: bool
) -> None:
    gh_issue_state(star.fake, 42, "OPEN", *labels)
    answer = star.post({"number": 42, "is_starred": starred})
    assert answer.status == 200
    assert json.loads(answer.body)["outcome"] == "unchanged"
    assert star.edits() == []
    assert not spawner.pending


def test_a_closed_issue_is_409_and_an_absent_one_404(
    star: StarWorld, clock: Clock
) -> None:
    gh_issue_state(star.fake, 42, "CLOSED", "bug")
    assert star.post({"number": 42, "is_starred": True}).status == 409
    clock.now += 1
    star.fake.on("gh", "issue", "view", "43", prefix=True)(
        Result(1, "", "GraphQL: Could not resolve to an issue with the number of 43.")
    )
    assert star.post({"number": 43, "is_starred": True}).status == 404
    assert star.edits() == []
    assert star.journal == ["star #42 on: closed", "star #43 on: absent"]


def test_a_failing_gh_is_502_with_its_reason_and_journaled(
    star: StarWorld, spawner: Spawner
) -> None:
    gh_issue_state(star.fake, 42, "OPEN")
    star.fake.on(*gh_edit(42, "--add-label"))(Result(1, "", "HTTP 503: unavailable"))
    answer = star.post({"number": 42, "is_starred": True})
    assert answer.status == 502
    assert b"HTTP 503: unavailable" in answer.body
    assert star.journal[0].startswith("star #42 on: failed")
    assert not spawner.pending


def test_the_star_shares_the_password_and_its_brake(
    star: StarWorld, clock: Clock
) -> None:
    assert star.post({"number": 42, "is_starred": True}, auth=None).status == 401
    assert star.post({"number": 42, "is_starred": True}, auth=BAD).status == 401
    clock.now += 1
    assert star.post({"number": 42, "is_starred": True}).status == 429, "braked"
    assert star.dashboard.handle("GET", "/", GOOD).status == 429, "the same brake"
    assert star.fake.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json"},  # no action header: a plain form
        {**ACTION, "X-Agentctl-Action": "launch"},
        {**ACTION, "Origin": "https://evil.example", "Host": HOST},
        {**ACTION, "Origin": f"https://{HOST}"},  # no Host to compare with
    ],
)
def test_a_cross_site_post_is_403(star: StarWorld, headers: dict[str, str]) -> None:
    assert star.post({"number": 42, "is_starred": True}, headers=headers).status == 403
    assert star.fake.calls == []


def test_the_dashboard_s_own_origin_is_accepted(star: StarWorld) -> None:
    gh_issue_state(star.fake, 42, "OPEN", "starred")
    headers = {**ACTION, "origin": f"https://{HOST}", "host": HOST}
    assert star.post({"number": 42, "is_starred": True}, headers=headers).status == 200


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        (
            {**ACTION, "Content-Type": "text/plain"},
            b'{"number": 42, "is_starred": true}',
        ),
        (ACTION, b"not json"),
        (ACTION, b'{"number": "42", "is_starred": true}'),
        (ACTION, b'{"number": true, "is_starred": true}'),
        (ACTION, b'{"number": 0, "is_starred": true}'),
        (ACTION, b'{"number": 42, "is_starred": "yes"}'),
        (ACTION, b'{"number": 42}'),
        (ACTION, b'{"number": 42, "is_starred": true, "label": "to-launch"}'),
        (ACTION, b"[42, true]"),
        (ACTION, b'{"number": 42, "is_starred": true}' + b" " * 1024),
    ],
)
def test_a_body_other_than_the_star_is_400(
    star: StarWorld, headers: dict[str, str], body: bytes
) -> None:
    assert star.post(headers=headers, body=body).status == 400
    assert star.fake.calls == []


def test_one_write_a_second_at_most(star: StarWorld, clock: Clock) -> None:
    gh_issue_state(star.fake, 42, "OPEN", "starred")
    assert star.post({"number": 42, "is_starred": True}).status == 200
    clock.now += 0.5
    refused = star.post({"number": 42, "is_starred": True})
    assert (refused.status, refused.header("Retry-After")) == (429, "1")
    clock.now += 0.5
    assert star.post({"number": 42, "is_starred": True}).status == 200
    assert len(star.fake.ran("gh", "issue", "view")) == 2


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/star"), ("PUT", "/star"), ("POST", "/"), ("POST", "/snapshot.json"),
     ("POST", "/start-next"), ("DELETE", "/star"), ("OPTIONS", "/star")],
)  # fmt: skip
def test_no_other_route_or_method_writes(
    star: StarWorld, method: str, path: str
) -> None:
    raw = b'{"number": 42, "is_starred": true}'
    answer = star.dashboard.handle(method, path, GOOD, ACTION, raw)
    assert answer.status == 405
    assert star.fake.calls == []


def test_no_get_ever_runs_gh_issue_edit(star: StarWorld) -> None:
    """Counter-proof: reading the dashboard writes nothing."""
    for path in ("/", "/snapshot.json", "/snapshot.json?fresh=1", "/star?number=42"):
        star.dashboard.handle("GET", path, GOOD, ACTION)
    assert star.edits() == []


def test_a_rebuild_asked_during_a_build_follows_it(
    cache: SnapshotCache, clock: Clock, spawner: Spawner, builds: Builds
) -> None:
    """A build under way read GitHub before the star: another one follows,
    whatever the Refresh floor."""
    cache.get()
    assert len(spawner.pending) == 1
    cache.renew()
    assert len(spawner.pending) == 1, "one build at a time"
    spawner.run()
    assert len(spawner.pending) == 1, "the build asked during the first one"
    spawner.run()
    assert builds.count == 2
    cache.renew()  # 0 s after: the floor does not apply to a write
    assert len(spawner.pending) == 1


def test_a_rebuild_asked_during_a_failing_build_still_follows_it(
    cache: SnapshotCache, spawner: Spawner, builds: Builds
) -> None:
    cache.get()
    cache.renew()
    builds.fail = True
    spawner.run()
    assert len(spawner.pending) == 1, "the star's rebuild is not lost"
    builds.fail = False
    spawner.run()
    assert json.loads(cache.get())["build_error"] is None


def test_the_wrapper_hands_the_star_its_headers_and_body(
    star: StarWorld,
) -> None:
    gh_issue_state(star.fake, 42, "OPEN", "starred")
    server = make_server(star.dashboard, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/star"
        request = urllib.request.Request(
            url,
            data=b'{"number": 42, "is_starred": true}',
            method="POST",
            headers={"Authorization": GOOD, **ACTION},
        )
        with urllib.request.urlopen(request, timeout=5) as answer:
            assert json.loads(answer.read())["outcome"] == "unchanged"
        big = urllib.request.Request(
            url,
            data=b" " * 5000,
            method="POST",
            headers={"Authorization": GOOD, **ACTION},
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(big, timeout=5)
        assert refused.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


class _Server:
    """`make_server`'s stand-in: serves until `shutdown`, as the real one."""

    def __init__(self) -> None:
        self.down = threading.Event()

    def serve_forever(self) -> None:
        assert self.down.wait(5), "never shut down"

    def shutdown(self) -> None:
        self.down.set()

    def server_close(self) -> None:
        pass


def _serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> tuple[int, str]:
    """`serve` on a history base at schema `version` (#2156): its exit, its
    journal."""
    import io
    import sqlite3
    from contextlib import closing
    from dataclasses import replace

    import tracerworks.cli as cli
    from tests.tracerworks.fakes import FakeRunner, recorded_settings
    from tracerworks.cli import Env, run

    secret = tmp_path / "s"
    secret.write_text(SECRET, encoding="utf-8")
    secret.chmod(0o600)
    settings = replace(recorded_settings(tmp_path), serve_secret_file=secret)
    settings.history_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.history_db)) as db:
        db.execute(f"PRAGMA user_version = {version}")
    server = _Server()
    monkeypatch.setattr(cli, "make_server", lambda *_: server)
    monkeypatch.setattr(cli, "BASE_WATCH_S", 0.01)
    if version <= store.SCHEMA_VERSION:
        threading.Timer(0.3, server.shutdown).start()  # stopped by hand
    err = io.StringIO()
    env = Env(settings, runner=FakeRunner(), out=io.StringIO(), err=err)
    return run(env, ["serve"]), err.getvalue()


def test_serve_on_a_newer_base_exits_2_for_systemd_to_restart_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 30/10 (#2154): tracerworks installed by hand, `serve` kept the old
    code and read nothing. Exit 2 — not 1, a refusal — restarts it."""
    from tracerworks.cli import EXIT_NEWER_BASE

    code, journal = _serving(tmp_path, monkeypatch, store.SCHEMA_VERSION + 1)
    assert code == EXIT_NEWER_BASE == 2
    assert f"schema {store.SCHEMA_VERSION + 1}" in journal
    assert "restart" in journal


def test_serve_on_the_same_base_keeps_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracerworks.cli import EXIT_OK

    code, journal = _serving(tmp_path, monkeypatch, store.SCHEMA_VERSION)
    assert code == EXIT_OK
    assert "restart" not in journal


def test_a_base_made_newer_while_serving_stops_it(tmp_path: Path) -> None:
    """The watch reads the version only: it never migrates, never creates."""
    import sqlite3
    from contextlib import closing

    from tracerworks.serve import watch_base

    path = tmp_path / "factory.sqlite3"
    stop = threading.Event()
    seen: list[int] = []
    watcher = threading.Thread(
        target=watch_base, args=(path, stop, seen.append, 0.01), daemon=True
    )
    watcher.start()
    stop.wait(0.05)
    assert not path.exists() and seen == [], "an absent base is not created"
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION}")
    stop.wait(0.05)
    assert seen == []
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 2}")
    watcher.join(2)
    assert seen == [store.SCHEMA_VERSION + 2]
    assert not watcher.is_alive(), "it stops once it said it"
