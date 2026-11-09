"""`tracerworks serve`: the factory dashboard, for the operator alone (#806).

Two layers. `Dashboard.handle` is the whole behaviour — a request in, an
answer out, clock and secret injected, no socket — so every rule below is
tested as a plain call. `make_server` wraps it in the standard library's HTTP
server, and does nothing else.

- GET, plus TWO actions: the operator's star (#1877) and the Settings tab
  (#2190). Any other method, and any other route for POST, is 405: no click
  can launch, stop or close anything in the factory.
  `POST /star` (`{"number": n, "is_starred": bool}`) puts or removes the
  star label on an OPEN issue, through `gh`, under the same password and the
  same brake as the GETs, and:
  - a CSRF guard: the `X-Agentctl-Action: star` header — a third-party page
    cannot send it without a CORS preflight, which is never granted — and an
    `Origin`, when sent, of the host served; JSON only, 1 KB at most;
  - one write per second at most (429), one journal line per write, and
    the snapshot rebuilt after it.
  `POST /settings` (#2190: `{"<key>": <int> | null, …}`, the keys changed
  only; null is back to the default) writes the operator's state file under
  the same guards — its own action header, 2 KB at most — through the state
  module the command line uses: a value out of its bounds is 400 with the
  reason and nothing written; one journal line per setting changed (old →
  new). `GET /settings.json` says each setting's value in force, default and
  bounds, read from the file on each request.
- HTTP Basic, over HTTPS terminated by the proxy in front; the password is
  compared in constant time, the user name is ignored. The secret lives
  outside the repository, in a file only its owner may read.
- A GLOBAL brake: after one failed attempt, every attempt — the right password
  included, whatever the address — is refused (429) for `BRAKE_SECONDS`.
  Guessing is then one try per ten seconds. A request that sends no
  credentials is not an attempt: it only gets the browser's challenge.
- The snapshot is served at once, and rebuilt in the background every
  `SNAPSHOT_TTL` while someone watches — every `IDLE_TTL` when nobody asked
  the page for `IDLE_AFTER` (#2196), never stopped; however many requests
  arrive, GitHub is not asked more often, and nobody waits for it (#820).
  The history's minute (alerts, samples) is the recorder's, not this.
  `?fresh=1` (the Refresh button) asks for a rebuild now, heard once per
  `FRESH_FLOOR` (#821).
- The tabs' data (`/trends.json`, #839) is read from the history base on
  each request: a local file, a few small queries.
- A BASE NEWER THAN ITSELF STOPS IT (#2156, 30/10: tracerworks installed by
  hand, `serve` kept the old code at schema 21 before a base at 25 and read
  nothing). `watch_base` reads the base's schema version — never creating,
  never migrating it — every `BASE_WATCH_S`; newer than this code's, `serve`
  exits 2 and its unit restarts it on the version installed, however it was
  installed.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from tracerworks import state_file, store
from tracerworks.github import StarOutcome, star_issue
from tracerworks.process import CommandError, Runner

BRAKE_SECONDS = 10.0
SNAPSHOT_TTL = 60.0
#: Nobody has asked the page for `IDLE_AFTER`: a snapshot every `IDLE_TTL`
#: only (#2196) — the first request brings the minute back. Measured on
#: 01/11: a snapshot costs 3.15 s of CPU, the history's minute 0.70 s; every
#: 10 min, serve at rest takes 1.0 s a minute, 1.7 % of a core.
IDLE_AFTER = 300
IDLE_TTL = 600
FRESH_FLOOR = 10.0  # the Refresh button: one rebuild per 10 s, however many clicks
STAR_ROUTE = "/star"
ACTION_HEADER = "X-Agentctl-Action"  # the CSRF guard's header (#1877)
STAR_ACTION = (ACTION_HEADER, "star")
STAR_BODY_MAX = 1024
SETTINGS_ROUTE = "/settings"
SETTINGS_ACTION = (ACTION_HEADER, "settings")
SETTINGS_BODY_MAX = 2048
BODY_MAX = max(STAR_BODY_MAX, SETTINGS_BODY_MAX)
WRITE_FLOOR = 1.0  # one write (a star, settings) per second at most
PAGE_FILE = Path(__file__).with_name("dashboard.html")

_COMMON = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
        " connect-src 'self'; img-src 'self' data:; frame-ancestors 'none';"
        " base-uri 'none'; form-action 'none'",
    ),
)


class SecretError(ValueError):
    """The password file is absent, empty, or readable by others."""


def read_secret(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SecretError(f"{path}: no secret (write it, then chmod 600)") from exc
    if stat.S_IMODE(mode) & 0o077:
        raise SecretError(f"{path}: must be readable by its owner only (chmod 0600)")
    if not text:
        raise SecretError(f"{path}: the secret is empty")
    return text


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str:
        return next((v for k, v in self.headers if k.lower() == name.lower()), "")


def _response(
    status: int, body: bytes, content_type: str, *extra: tuple[str, str]
) -> Response:
    return Response(status, (("Content-Type", content_type), *_COMMON, *extra), body)


def _text(status: int, text: str, *extra: tuple[str, str]) -> Response:
    return _response(status, text.encode(), "text/plain; charset=utf-8", *extra)


def _in_a_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="snapshot-build", daemon=True).start()


class SnapshotCache:
    """The last snapshot, served AT ONCE; rebuilt behind the request (#820).

    A request never waits for GitHub, git or tmux: it gets the last snapshot
    there is, with its age (`built_at`) and whether a newer one is on its way
    (`building`). A request that finds it `ttl` seconds old starts ONE build in
    the background — one at a time, however many requests arrive.

    Nobody asked for `idle_after` seconds, the snapshot is rebuilt every
    `idle_ttl` only (#2196): a full snapshot costs seconds of CPU, mostly in
    `gh`. The first request finds it older than `ttl` and starts a build.

    A failed build keeps the last snapshot, said as such (`build_error`), and
    is not retried before `ttl`: a failing GitHub is not asked on every
    refresh. With no snapshot at all, the failure is the answer.

    The last good snapshot is also written to `store` (outside the
    repository, owner only), so a restarted service serves it at once.

    After each build, ok or not, `release` hands the heap's free pages back
    (#2195): a build parses megabytes of `gh` answers, and the allocator
    would otherwise keep their high-water mark for good.
    """

    def __init__(
        self,
        build: Callable[[], dict[str, Any]],
        clock: Callable[[], float],
        ttl: float = SNAPSHOT_TTL,
        store: Path | None = None,
        spawn: Callable[[Callable[[], None]], None] = _in_a_thread,
        release: Callable[[], None] = lambda: None,
        idle_ttl: float = IDLE_TTL,
        idle_after: float = IDLE_AFTER,
    ) -> None:
        self._build = build
        self._release = release
        self._idle_ttl = idle_ttl
        self._idle_after = idle_after
        self._asked_at: float | None = None  # the last request; None: none yet
        self._clock = clock
        self._ttl = ttl
        self._store = store
        self._spawn = spawn
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._built_at: float | None = None
        self._tried_at: float | None = None
        self._building = False
        self._again = False  # a rebuild asked while one was under way
        self._error: str | None = None
        self._load()

    def get(self, fresh: bool = False) -> bytes:
        """The last snapshot as JSON; `RuntimeError` when there is none and
        the last build failed. `fresh` asks for a newer one (the Refresh
        button), heard at most once per `FRESH_FLOOR`."""
        with self._lock:
            self._asked_at = self._clock()
        self._start_if(self._asked if fresh else self._stale)
        with self._lock:
            if self._data is None and self._error is not None and not self._building:
                raise RuntimeError(self._error)
            body = {
                **(self._data or {}),
                "built_at": self._built_at,
                "building": self._building,
                "build_error": self._error,
                # the server's clock: the page counts the age from it, not
                # from the phone's
                "served_at": self._clock(),
            }
        return json.dumps(body, ensure_ascii=False).encode()

    def warm(self) -> None:
        """Start the first build now, before any visitor asks."""
        self._start_if(self._stale)

    def tick(self) -> None:
        """A build if the last one started `ttl` ago (`idle_ttl` with nobody
        watching) — asked every second by `keep_fresh`."""
        self._start_if(self._stale)

    def keep_fresh(self, stop: threading.Event, every: float = 1.0) -> None:
        """Collect the state permanently (the operator, 30/09): every `ttl` while
        someone watches, every `idle_ttl` otherwise (#2196)."""
        while not stop.wait(every):
            self.tick()

    def renew(self) -> None:
        """A rebuild now, whatever the floor — after a star is written
        (#1877): a build under way read GitHub before it, so another one
        follows it."""
        with self._lock:
            if self._building:
                self._again = True
                return
            self._building = True
            self._tried_at = self._clock()
        self._spawn(self._rebuild)

    def _stale(self, now: float) -> bool:
        """Called under the lock."""
        watched = self._asked_at is not None and now - self._asked_at < self._idle_after
        ttl = self._ttl if watched else self._idle_ttl
        return self._tried_at is None or now - self._tried_at >= ttl

    def _asked(self, now: float) -> bool:
        return self._tried_at is None or now - self._tried_at >= FRESH_FLOOR

    def _start_if(self, due: Callable[[float], bool]) -> None:
        with self._lock:
            if self._building or not due(self._clock()):
                return
            self._building = True
            self._tried_at = self._clock()
        self._spawn(self._rebuild)

    def _rebuild(self) -> None:
        try:
            data = self._build()
        except Exception as exc:  # said in the answer, never hidden
            with self._lock:
                self._error = str(exc) or type(exc).__name__
            self._release()
            self._next()
            return
        built_at = self._clock()
        self._save(data, built_at)  # before `building` drops: one writer at a time
        with self._lock:
            self._data, self._built_at, self._error = data, built_at, None
        del data
        self._release()  # the build's high-water mark, given back (#2195)
        self._next()

    def _next(self) -> None:
        """The build done, ok or not: the one asked meanwhile (`renew`), if
        any, starts — `building` never drops in between."""
        with self._lock:
            again, self._again = self._again, False
            self._building = again
            if again:
                self._tried_at = self._clock()
        if again:
            self._spawn(self._rebuild)

    def _load(self) -> None:
        if self._store is None:
            return
        try:
            saved = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        built_at, data = saved.get("built_at"), saved.get("snapshot")
        if not isinstance(built_at, int | float) or not isinstance(data, dict):
            return
        self._data = data
        self._built_at = self._tried_at = float(built_at)

    def _save(self, data: dict[str, Any], built_at: float) -> None:
        if self._store is None:
            return
        body = json.dumps({"built_at": built_at, "snapshot": data}, ensure_ascii=False)
        partial = self._store.with_name(self._store.name + ".partial")
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(body)
            partial.replace(self._store)
        except OSError:
            pass  # the copy on disk is a comfort; the snapshot in memory is served


@dataclass(frozen=True)
class Star:
    """Where the dashboard's only write goes (#1877): `gh`, the repository,
    the star label of the project file."""

    run: Runner
    repo: str
    label: str

    def put(self, number: int, on: bool) -> StarOutcome:
        return star_issue(self.run, self.repo, number, self.label, on)


def schema_of(path: Path) -> int | None:
    """The history base's schema version, read only — never created, never
    migrated; None when it is absent or unreadable."""
    if not path.exists():
        return None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            return store.version(db)
    except sqlite3.Error:
        return None


def watch_base(
    path: Path, stop: threading.Event, newer: Callable[[int], None], every: float
) -> None:
    """Until `stop`: a base at a schema newer than this code's is handed to
    `newer`, once, and the watch ends."""
    while True:
        version = schema_of(path)
        if version is not None and version > store.SCHEMA_VERSION:
            newer(version)
            return
        if stop.wait(every):
            return


def _to_journal(line: str) -> None:
    print(line, file=sys.stderr, flush=True)  # the service's journal


class Dashboard:
    def __init__(
        self,
        secret: str,
        page: bytes,
        snapshot: SnapshotCache,
        clock: Callable[[], float],
        history: dict[str, Callable[[dict[str, str]], dict[str, Any]]] | None = None,
        star: Star | None = None,
        log: Callable[[str], None] = _to_journal,
        state: Path | None = None,
        defaults: Mapping[str, object] | None = None,
    ) -> None:
        self._secret = secret.encode()
        self._history = history or {}
        self._page = page
        self._snapshot = snapshot
        self._clock = clock
        self._star = star
        self._log = log
        self._state = state
        self._defaults = dict(defaults or {})  # an optional setting's, from the project
        self._lock = threading.Lock()
        self._writing = threading.Lock()  # one state file write at a time
        self._braked_until = 0.0
        self._written_at: dict[str, float] = {}  # per action

    def handle(
        self,
        method: str,
        path: str,
        authorization: str | None,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        route, _, query = path.partition("?")
        if route == STAR_ROUTE and self._star is not None:
            if method != "POST":
                return _text(405, "POST only\n", ("Allow", "POST"))
            return self._post_star(self._star, authorization, headers or {}, body)
        if route == SETTINGS_ROUTE and self._state is not None:
            if method != "POST":
                return _text(405, "POST only\n", ("Allow", "POST"))
            return self._post_settings(self._state, authorization, headers or {}, body)
        if method != "GET":
            return _text(405, "GET only\n", ("Allow", "GET"))
        denied = self._authenticate(authorization)
        if denied is not None:
            return denied
        if route == "/":
            return _response(200, self._page, "text/html; charset=utf-8")
        if route == "/snapshot.json":
            try:
                body = self._snapshot.get(fresh="fresh=1" in query.split("&"))
            except Exception as exc:  # said, never hidden
                return _text(503, f"snapshot unavailable: {exc}\n")
            return _response(200, body, "application/json; charset=utf-8")
        if route == SETTINGS_ROUTE + ".json" and self._state is not None:
            try:
                current = state_file.load(self._state)
            except (OSError, state_file.StateFileError) as exc:  # said, never hidden
                return _text(503, f"settings unreadable: {exc}\n")
            return _json(200, settings_view(current, self._defaults))
        name = route.removeprefix("/").removesuffix(".json")
        if route.endswith(".json") and name in self._history:
            # the history base, shaped for a tab (#839) or a panel (#860, with
            # its query): read on each request, a few small queries on a local file
            try:
                data = self._history[name](dict(parse_qsl(query)))
            except Exception as exc:  # said, never hidden
                return _text(503, f"{name} unavailable: {exc}\n")
            body = json.dumps(data, ensure_ascii=False).encode()
            return _response(200, body, "application/json; charset=utf-8")
        return _text(404, "not found\n")

    def _post_star(
        self,
        star: Star,
        authorization: str | None,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Response:
        """`POST /star`: the password and its brake, the CSRF guard, the body,
        the write floor — then the star, on an open issue only."""
        denied = self._authenticate(authorization)
        if denied is not None:
            return denied
        if not _same_site(headers, STAR_ACTION):
            return _text(403, "not from the dashboard\n")
        asked = _star_body(headers, body)
        if isinstance(asked, str):
            return _text(400, f"{asked}\n")
        number, starred = asked
        braked = self._write_floor("star", "one star a second")
        if braked is not None:
            return braked
        try:
            outcome = star.put(number, starred)
        except (CommandError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc).strip() or type(exc).__name__
            self._log(f"star #{number} {_side(starred)}: failed — {reason}")
            return _text(502, f"GitHub refused: {reason}\n")
        self._log(f"star #{number} {_side(starred)}: {outcome}")
        if outcome == "absent":
            return _text(404, f"no issue #{number}\n")
        if outcome == "closed":
            return _text(409, f"#{number} is closed\n")
        if outcome != "unchanged":
            self._snapshot.renew()
        answer = {"number": number, "is_starred": starred, "outcome": outcome}
        return _response(
            200, json.dumps(answer).encode(), "application/json; charset=utf-8"
        )

    def _write_floor(self, action: str, said: str) -> Response | None:
        """429 within `WRITE_FLOOR` of the last write of the same action (a
        star, settings): a star never holds a switch back."""
        with self._lock:
            now = self._clock()
            last = self._written_at.get(action)
            if last is not None and now - last < WRITE_FLOOR:
                return _text(429, f"{said}\n", ("Retry-After", "1"))
            self._written_at[action] = now
        return None

    def _post_settings(
        self,
        path: Path,
        authorization: str | None,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Response:
        """`POST /settings`: the star's guards, then the keys changed —
        written through the state module, or 400 and nothing written."""
        denied = self._authenticate(authorization)
        if denied is not None:
            return denied
        if not _same_site(headers, SETTINGS_ACTION):
            return _text(403, "not from the dashboard\n")
        asked = _settings_body(headers, body)
        if isinstance(asked, str):
            return _text(400, f"{asked}\n")
        with self._writing:
            try:
                current = state_file.load(path)
            except (OSError, state_file.StateFileError) as exc:
                return _text(503, f"settings unreadable: {exc}\n")
            try:
                changed = state_file.assign(current, asked)
            except state_file.StateFileError as exc:
                return _text(400, f"{exc}\n")
            # a refused value takes no write: the floor counts writes only
            braked = self._write_floor("settings", "one write a second")
            if braked is not None:
                return braked
            try:
                state_file.save(path, changed)
            except OSError as exc:
                self._log(f"settings: write failed — {exc}")
                return _text(503, f"settings not written: {exc}\n")
        said = changes_said(current, changed, asked, self._defaults)
        for line in said:
            self._log(line)
        if said:  # the pause, the minutes: shown by the next snapshot, now
            self._snapshot.renew()
        return _json(200, settings_view(changed, self._defaults))

    def _authenticate(self, authorization: str | None) -> Response | None:
        challenge = ("WWW-Authenticate", 'Basic realm="tracerworks", charset="UTF-8"')
        if authorization is None:
            return _text(401, "password required\n", challenge)
        with self._lock:
            now = self._clock()
            if now < self._braked_until:
                wait = max(1, int(self._braked_until - now))
                return _text(429, "too many attempts\n", ("Retry-After", str(wait)))
            if hmac.compare_digest(_password(authorization), self._secret):
                return None
            self._braked_until = now + BRAKE_SECONDS
        return _text(401, "wrong password\n", challenge)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def _json(status: int, data: object) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode()
    return _response(status, body, "application/json; charset=utf-8")


def _same_site(headers: Mapping[str, str], action: tuple[str, str]) -> bool:
    """The CSRF guard: the action's own header — a custom header a cross-site
    page cannot send without a preflight, never granted — and an `Origin`,
    when the browser sends one, of the host served."""
    name, value = action
    if _header(headers, name) != value:
        return False
    origin = _header(headers, "Origin")
    if origin is None:
        return True
    host = _header(headers, "Host")
    return host is not None and urlsplit(origin).netloc.lower() == host.lower()


def _json_body(headers: Mapping[str, str], body: bytes, limit: int) -> object:
    """The JSON sent, or why it is refused — a `_Refused`."""
    kind = (_header(headers, "Content-Type") or "").split(";")[0].strip().lower()
    if kind != "application/json":
        return _Refused("JSON only")
    if len(body) > limit:
        return _Refused(f"body over {limit} bytes")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return _Refused("body is not JSON")


@dataclass(frozen=True)
class _Refused:
    reason: str


def _star_body(headers: Mapping[str, str], body: bytes) -> tuple[int, bool] | str:
    """The issue's number and the star asked, or why the body is refused."""
    data = _json_body(headers, body, STAR_BODY_MAX)
    if isinstance(data, _Refused):
        return data.reason
    if not isinstance(data, dict) or set(data) != {"number", "is_starred"}:
        return 'expected {"number": <int>, "is_starred": <bool>}'
    number, starred = data["number"], data["is_starred"]
    if type(number) is not int or not 0 < number < 10**9:
        return "number must be an issue number"
    if not isinstance(starred, bool):
        return "is_starred must be true or false"
    return number, starred


def _settings_body(headers: Mapping[str, str], body: bytes) -> dict[str, object] | str:
    """The settings asked (key → value, or null for the default), or
    why the body is refused. Bounds are the state module's to check."""
    data = _json_body(headers, body, SETTINGS_BODY_MAX)
    if isinstance(data, _Refused):
        return data.reason
    if not isinstance(data, dict) or not data:
        return 'expected {"<setting>": <value> | null, ...}'
    kinds = {t.key: t.kind for t in state_file.TUNABLES}
    for key, value in data.items():
        if key not in kinds:
            return f"{key!r} is not a setting of the dashboard"
        if value is None:
            continue
        if kinds[key] == "bool" and not isinstance(value, bool):
            return f"{key} must be true, false or null"
        if kinds[key] == "int" and type(value) is not int:
            return f"{key} must be a whole number or null"
    return data


def _default(key: str, defaults: Mapping[str, object]) -> object:
    """A setting's default: the code's, or for an optional one the
    project file's (`defaults`)."""
    return defaults.get(key, state_file.default_of(key))


def _in_force(
    state: state_file.State, key: str, defaults: Mapping[str, object]
) -> object:
    value = state_file.value_of(state, key)
    return _default(key, defaults) if value is None else value


def settings_view(
    state: state_file.State, defaults: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """What the Settings tab shows: per block, each setting's value in force,
    its default, its bounds, and whether the file sets it."""
    defaults = defaults or {}
    return {
        "blocks": [{"key": k, "label": label} for k, label in state_file.BLOCKS],
        "settings": [
            {
                "key": t.key,
                "block": t.block,
                "label": t.label,
                "kind": t.kind,
                "unit": t.unit,
                "value": _in_force(state, t.key, defaults),
                "default": _default(t.key, defaults),
                "minimum": t.minimum,
                "maximum": t.maximum,
                "set": t.key in state.written,
            }
            for t in state_file.TUNABLES
        ],
    }


def changes_said(
    before: state_file.State,
    after: state_file.State,
    asked: Mapping[str, object],
    defaults: Mapping[str, object] | None = None,
) -> list[str]:
    """One journal line per setting changed: key, old → new value in force."""
    defaults = defaults or {}
    lines = []
    for key in asked:
        old, new = _in_force(before, key, defaults), _in_force(after, key, defaults)
        if old == new and (key in before.written) == (key in after.written):
            continue
        default = "" if key in after.written else " (default)"
        lines.append(f"setting {key}: {_said(old)} → {_said(new)}{default}")
    return lines


def _said(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _side(starred: bool) -> str:
    """What was asked, in the journal: on or off (the outcome says the rest)."""
    return "on" if starred else "off"


def _password(authorization: str) -> bytes:
    """The password of a Basic header; b"" for anything else (never equal to
    a non-empty secret)."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "basic":
        return b""
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return b""
    return decoded.partition(b":")[2]


def make_server(dashboard: Dashboard, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tracerworks"
        sys_version = ""

        def _answer(self) -> None:
            body = b""
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                # never more than the limit and a byte: past it, refused
                body = self.rfile.read(max(0, min(length, BODY_MAX + 1)))
                if length > len(body):
                    self.close_connection = True  # the rest is left unread
            response = dashboard.handle(
                self.command,
                self.path,
                self.headers.get("Authorization"),
                dict(self.headers.items()),
                body,
            )
            self.send_response(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _answer
        do_OPTIONS = _answer

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Refusals only: the journal would otherwise fill with refreshes.
            if str(code).isdigit() and int(code) >= 400:
                super().log_request(code, size)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
