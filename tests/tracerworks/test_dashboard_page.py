"""The dashboard page's own behaviour, under `node --test` (#820).

The page is one HTML file with no build: its update-in-place (`morph`) is
tested by `dashboard.test.mjs`, which reads it from the page itself. Node is
required, never skipped: a missing Node would be a green that proves nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.tracerworks.fakes import PACKAGE

SUITE = Path(__file__).with_name("dashboard.test.mjs")
PAGE = PACKAGE / "dashboard.html"
#: The suite reads the page it is told: the package's, installed or not.
NODE_ENV = {**os.environ, "DASHBOARD_PAGE": str(PAGE)}


def test_the_page_updates_in_place() -> None:
    node = shutil.which("node")
    assert node, "node is required to test the dashboard page"
    run = subprocess.run(
        [node, "--test", str(SUITE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=NODE_ENV,
    )
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]


@pytest.mark.parametrize("zone", ["America/New_York", "Asia/Tokyo"])
def test_hours_follow_the_browsers_time_zone(zone: str) -> None:
    """The operator, 01/10 (#869): an hour is shown in the zone of the device looking,
    never a fixed one. The suite under two zones: the next start's hour moves
    with it (the expected hours are written out in the suite)."""
    node = shutil.which("node")
    assert node, "node is required to test the dashboard page"
    run = subprocess.run(
        [
            node,
            "--test",
            "--test-reporter=tap",
            "--test-name-pattern",
            "the next start",
            str(SUITE),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**NODE_ENV, "TZ": zone},
    )
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
    assert "# pass 1" in run.stdout, run.stdout[-2000:]


def test_the_unlabeled_counter_is_gone() -> None:
    """#857: « Waiting on GitHub » lists everything by nature; what has none
    is the To sort group, not a counter of issues « without a label »."""
    page = PAGE.read_text(encoding="utf-8")
    assert "unlabeled" not in page.lower()
    assert "without a label" not in page
    for group in ("Specs ready", "Bugs", "Tasks", "Ideas", "To sort"):
        assert f"'{group}'" in page


def test_the_page_keeps_clear_of_the_iphone_status_bar() -> None:
    """Opened as an app on an iPhone (#823), the page runs under Apple's
    status bar: it must claim the whole screen and pad itself by the safe
    area, or the bar veils its first line."""
    page = PAGE.read_text(encoding="utf-8")
    assert "viewport-fit=cover" in page
    assert 'name="apple-mobile-web-app-status-bar-style"' in page
    assert 'name="theme-color" content="#121417"' in page  # the page's --bg
    assert "--bg:#121417" in page
    body = next(line for line in page.splitlines() if line.startswith("body{"))
    assert "env(safe-area-inset-top)" in body


def _rule(page: str, selector: str) -> str:
    """The declarations of the page's one rule for `selector`."""
    start = page.index("\n" + selector + "{") + len(selector) + 2
    return page[start : page.index("}", start)]


def test_the_page_does_not_bounce_when_there_is_nothing_to_scroll() -> None:
    """The operator, 16/10, as on his other screens (13/10): a trackpad flick
    on a page
    that holds in the window no longer drags it. Only the bounce goes.

    On `html` only, whose value is the window's. `body` has `overflow-x`, so
    it is a scroll box of its own: `none` on it kept the wheel from reaching
    the window, and the Quality tab no longer scrolled (the operator, 16/10;
    measured in Chromium: 0 px of 1976 with it on body, 1976 without)."""
    page = PAGE.read_text(encoding="utf-8")
    assert _rule(page, "html") == "overscroll-behavior:none"
    body = _rule(page, "html,body")
    assert "overflow-x:hidden" in body and "overscroll-behavior" not in body


def test_the_tabs_have_no_grey_background() -> None:
    """The operator, 01/10 (#853): the grey goes. And (01/10, later) no line under the
    active tab: it takes the PAGE's own background, which hides the row's
    rule beneath it without painting any grey."""
    page = PAGE.read_text(encoding="utf-8")
    for selector in (".tabs", ".tab", ".tab:hover"):
        assert "background" not in _rule(page, selector), selector
    assert _rule(page, ".tab.on") == (
        "color:var(--fg);border-color:var(--line);background:var(--bg)"
    )
    assert "html,body{margin:0;background:var(--bg)" in page  # the page's own


def ids_written(page: str) -> list[str]:
    """Every `id="…"` the page writes, in markup and in its JS templates.

    A dynamic id keeps its `${…}` as written: `id="g-${n}"` reads `g-${n}`.
    The scan follows `${…}` to its closing brace, so a quote inside the
    expression (`id="g-${esc(k, "x")}"`) does not end the value early.
    """
    found: list[str] = []
    for start in _ID_ATTR.finditer(page):
        quote, i, depth, value = start.group(1), start.end(), 0, []
        while i < len(page) and (depth or page[i] != quote):
            if page.startswith("${", i):
                depth += 1
                value.append("${")
                i += 2
                continue
            if depth and page[i] == "{":
                depth += 1
            if depth and page[i] == "}":
                depth -= 1
            value.append(page[i])
            i += 1
        found.append("".join(value))
    return found


def ids_held_twice(page: str) -> list[str]:
    """What breaks « one id, one element » in the page as written (#868).

    - a fixed id written twice (#865: two `id="tip"`, `$('#tip')` took the
      wrong one);
    - a dynamic id with no fixed prefix (`id="${k}"`): nothing says whose it
      is, so nothing keeps it from another's;
    - two dynamic ids whose prefixes overlap (`g-${a}` and `g-x-${b}` both
      make `g-x-1`), or a fixed id one of them can make (`g-x` beside
      `g-${a}`);
    - an id set from JS (`.id =`, `setAttribute('id'`), which this scan
      cannot read.
    """
    faults: list[str] = []
    written = ids_written(page)
    fixed = [i for i in written if "${" not in i]
    prefixes = [i[: i.index("${")] for i in written if "${" in i]
    faults += sorted({f"{i} (twice)" for i in fixed if fixed.count(i) > 1})
    faults += [f"{p}${{…}} (no prefix of its own)" for p in prefixes if not p]
    named = [p for p in prefixes if p]
    for n, mine in enumerate(named):
        faults += [
            f"{mine}${{…}} and {other}${{…}} (overlap)"
            for other in named[n + 1 :]
            if mine.startswith(other) or other.startswith(mine)
        ]
        faults += [
            f"{i} and {mine}${{…}} (overlap)" for i in fixed if i.startswith(mine)
        ]
    faults += [f"{m.group(0)} (set from JS)" for m in _ID_FROM_JS.finditer(page)]
    return faults


_ID_ATTR = re.compile(r"(?<![\w.$\[-])id=([\"'])")
_ID_FROM_JS = re.compile(r"(?<!dataset)\.id\s*=(?!=)|setAttribute\(\s*['\"]id['\"]")


def test_every_id_is_held_once() -> None:
    """#868, after #865: no two elements of the page share an id — the
    fixed ones and those its templates make."""
    page = PAGE.read_text(encoding="utf-8")
    assert set(ids_written(page)) >= {
        "app",
        "tip",
        "panel",
        "live",
    }
    assert ids_held_twice(page) == []
    assert '<div id="tip" role="tooltip" hidden></div>' in page


@pytest.mark.parametrize(
    ("page", "fault"),
    [
        ('<div id="tip"></div><svg><marker id="tip"></svg>', "tip (twice)"),
        ("`<marker id='tip'>` + `<i id='tip'>`", "tip (twice)"),
        ('`<i id="${k}">`', "${…} (no prefix of its own)"),
        ('`<i id="g-${a}">` `<b id="g-${b}">`', "g-${…} and g-${…} (overlap)"),
        ('`<i id="g-${a}">` `<b id="g-x-${b}">`', "g-${…} and g-x-${…} (overlap)"),
        ('<p id="g-x"></p>`<i id="g-${a}">`', "g-x and g-${…} (overlap)"),
        ("el.id = 'tip';", ".id = (set from JS)"),
        ("el.setAttribute('id', k);", "setAttribute('id' (set from JS)"),
    ],
)
def test_the_id_guard_catches(page: str, fault: str) -> None:
    """The counter-proof: each way of writing a duplicate fails the guard."""
    assert fault in ids_held_twice(page)


def test_the_id_guard_reads_through_an_expression() -> None:
    """What is NOT a duplicate stays quiet: an expression with quotes and
    braces of its own, `data-id`, a selector, `dataset.id`, two prefixes."""
    nested = '`<i id="g-${f({a: 1}, "x")}">`'
    assert ids_written(nested) == ['g-${f({a: 1}, "x")}']
    assert ids_held_twice('$(\'[id="tip"]\'); el.dataset.id = k; <i id="tip">') == []
    page = '`<i id="g-${esc(k, "x")}">` `<b data-id="g" aria-id="g" id="h">`'
    assert ids_written(page) == ['g-${esc(k, "x")}', "h"]
    assert ids_held_twice(page) == []
    assert ids_held_twice('`<i id="g-${a}">` `<b id="h-${b}">`') == []
