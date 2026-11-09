"""The factory snapshot, first slice: the Atelier and the VPS gauges (#807).

Everything the dashboard shows is built here, from the same readings as
`status` and `start-next` (tmux, git, `gh`), recorded. The claim under test is
the JSON a given world renders — never how it is computed.

The recorded world is `recorded_world()` (#501 working, #510 waiting, #520
stopped, #530 dead) plus its chantiers on GitHub:

- #501: #502 closed, #503 open and DECLARED (progress label), #504 to do; #999
  mentions #501 in its body but belongs to #900 — not a ticket of #501;
- #510: #511 carries needs-human (the worker asked), #512 blocked by #511;
- #520: #521 open, no label, nothing blocking — the DEDUCED head;
- #530: no ticket at all.
"""

from __future__ import annotations

import io
import json
import math
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.tracerworks.control_world import PR_FIELDS, PULLS, control_world
from tests.tracerworks.fakes import (
    ROOT,
    TMUX,
    FakeRunner,
    fixture,
    recorded_settings,
    recorded_world,
    target,
)
from tests.tracerworks.history_world import RELAUNCHER_LOG, history_world
from tests.tracerworks.test_usage import _worktree_logs
from tracerworks import machine_red, order, parking, store
from tracerworks.cli import EXIT_OK, Env, run
from tracerworks.control import Memo
from tracerworks.frontier import BodyRules, Queue, parse_issues
from tracerworks.observe import WorkerState
from tracerworks.settings import Waits
from tracerworks.snapshot import Red, build_snapshot, red_holder
from tracerworks.state_file import State

NOW = 1790413800.0
REPO = "owner/repo"
FIELDS = "number,title,body,labels,assignees,author,createdAt"
CLOSED_FIELDS = "number,title,body,labels,assignees,author,closedAt"
RULES = BodyRules(["Parent"], ["Dépend de", "Depends on", "Blocked by"])
QUEUE = Queue(frozenset({"ready-for-agent", "to-launch"}), "priority")


def issue(
    n: int, title: str, body: str = "", labels: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "number": n,
        "title": title,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "assignees": [],
        "author": {"login": "operator-login"},
    }


SPEC_501 = (
    "## Problem Statement\n\nSam répond à la main <b>partout</b>.\n\n"
    + "## Solution\n\n"
    + "Une console. " * 200
)
OPEN = [
    issue(501, "Spec — Console opérateur", SPEC_501, ("ready-for-agent",)),
    issue(503, "Console (2/3) — écran", "Parent : #501", ("in-progress",)),
    issue(504, "Console (3/3) — docs", "Parent : #501. Dépend de : #503"),
    issue(510, "Spec — Classification", labels=("ready-for-agent",)),
    issue(511, "Classif (1/2) — modèle", "Parent : #510", ("needs-human",)),
    issue(512, "Classif (2/2) — écran", "Parent : #510. Blocked by: #511"),
    issue(520, "Spec — Tableau de bord", labels=("ready-for-agent",)),
    issue(521, "Tableau (1/1) — tout", "Parent : #520"),
    issue(530, "Spec — Mort", labels=("ready-for-agent",)),
]
CLOSED = [
    {
        **issue(502, "Console (1/3) — API", "Parent : #501"),
        "closedAt": "2026-09-25T20:00:00Z",
    },
    issue(999, "Autre chose", "Parent : #900. Voir aussi #501"),
    {
        **issue(611, "Relance (1/2) — job", "Parent : #610"),
        "closedAt": "2026-09-20T20:00:00Z",
    },
]
#: Specs without a worker, and issues nobody labelled.
WAITING_AND_QUEUE = [
    {
        **issue(
            600,
            "Spec — Écrite d'avance",
            "## Problem Statement\n\nPlus tard.",
            ("ready-for-agent",),
        ),
        "createdAt": "2026-09-26T01:00:00Z",
    },
    {
        **issue(605, "Spec — Plus ancienne", labels=("ready-for-agent", "to-launch")),
        "createdAt": "2026-09-01T01:00:00Z",
    },
    issue(610, "Spec — Relance", labels=("ready-for-agent", "to-launch")),
    {
        **issue(612, "Relance (2/2) — écran", "Parent : #610", ("ready-for-agent",)),
        "createdAt": "2026-09-26T02:00:00Z",
    },
    issue(620, "Spec — Urgente", labels=("ready-for-agent", "to-launch", "priority")),
    issue(700, "Idée : thème clair"),
    issue(701, "Bug : date décalée"),
]


TIMER = "tracerworks-next-fixture.timer"  # the fixture project's relauncher
NEXT_PASS_US = 1790414100_000000  # NOW + 5 min, in microseconds


def timer_answer(fake: FakeRunner, stdout: str, returncode: int = 0) -> None:
    fake.answer(
        *("systemctl", "--user", "list-timers", TIMER, "--all", "--output=json"),
        stdout=stdout,
        returncode=returncode,
    )


def closed_list(fake: FakeRunner, **answer: Any) -> None:
    fake.answer(
        "gh",
        "issue",
        "list",
        "-R",
        REPO,
        "--state",
        "closed",
        "--limit",
        "2000",
        "--json",
        CLOSED_FIELDS,
        **answer,
    )


def merged_answer(fake: FakeRunner, branch: str, number: int | None) -> None:
    fake.answer(
        "gh", "pr", "list", "--repo", REPO, "--head", branch,
        "--state", "merged", "--json", "number", "--limit", "1",
        stdout=json.dumps([] if number is None else [{"number": number}]),
    )  # fmt: skip


def github_world(fake: FakeRunner) -> FakeRunner:
    fake.answer(
        "gh",
        "issue",
        "list",
        "-R",
        REPO,
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        FIELDS,
        stdout=json.dumps(OPEN + WAITING_AND_QUEUE),
    )
    closed_list(fake, stdout=json.dumps(CLOSED))
    timer_answer(fake, json.dumps([{"unit": TIMER, "next": NEXT_PASS_US}]))
    control_world(fake)
    history_world(fake)
    # The waiting worker's branch has no merged pull request (#830).
    merged_answer(fake, "agent/510-classification", None)
    # Native dependencies of a head candidate: none.
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/issues/511",
        "--jq",
        ".issue_dependencies_summary.blocked_by // 0",
        stdout="0\n",
    )
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/issues/521",
        "--jq",
        ".issue_dependencies_summary.blocked_by // 0",
        stdout="0\n",
    )
    # ...and of the queue's #610: none — the next pass has its chantier (#869)
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/issues/612",
        "--jq",
        ".issue_dependencies_summary.blocked_by // 0",
        stdout="0\n",
    )
    fake.answer(
        "gh",
        "issue",
        "view",
        "511",
        "-R",
        REPO,
        "--json",
        "comments",
        stdout=json.dumps(
            {
                "comments": [
                    {"body": "Démarrage.", "author": {"login": "operator-login"}},
                    {
                        "body": "\n  Question : garder X ou Y ?\nDétails plus bas.",
                        "author": {"login": "operator-login"},
                    },
                ]
            }
        ),
    )
    fake.answer(
        "df",
        "-Pk",
        str(ROOT),
        stdout=(
            "Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
            "/dev/sda1        100000000  60000000  40000000      60% /\n"
        ),
    )
    return fake


RUNNERS = "vps-1, vps-2, vps-3"


def snapshot(
    fake: FakeRunner,
    tmp_path: Path,
    memo: Memo | None = None,
    log: str | None = RELAUNCHER_LOG,
    quotas: dict[str, Any] | None = None,
    billing: Any = None,
    cpu: tuple[float, float] | None = None,
    state: State | None = None,
) -> dict[str, Any]:
    journal = tmp_path / "next.log"
    if log is not None:
        journal.write_text(log, encoding="utf-8")
    settings = replace(
        recorded_settings(tmp_path),
        runner_name=RUNNERS,
        relauncher_log=journal,
        **(quotas or {}),
    )
    env = Env(settings, runner=fake, now=lambda: NOW)
    state = state or State(claude_weekly_used=42)
    return build_snapshot(
        fake,
        settings,
        env.tmux,
        state,
        NOW,
        transcripts=tmp_path / "projects",
        memo=memo,
        billing=billing,
        cpu=cpu,
    )


def workers_of(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {w["spec"]: w for w in data["workers"]}


@pytest.fixture
def world(tmp_path: Path) -> dict[int, dict[str, Any]]:
    _worktree_logs(tmp_path / "projects", ROOT / "501")
    return workers_of(snapshot(github_world(recorded_world()), tmp_path))


def test_a_declared_ticket_is_the_current_one(
    world: dict[int, dict[str, Any]],
) -> None:
    worker = world[501]
    assert worker["title"] == "Spec — Console opérateur"
    assert worker["state"] == "active"
    assert worker["current_ticket"] == {
        "number": 503,
        "title": "Console (2/3) — écran",
        "declared": True,
    }


def test_the_tickets_are_in_order_and_closed_ones_count_as_done(
    world: dict[int, dict[str, Any]],
) -> None:
    worker = world[501]
    assert [(t["number"], t["status"]) for t in worker["tickets"]] == [
        (502, "done"),
        (503, "current"),
        (504, "todo"),
    ]
    assert (worker["done"], worker["total"]) == (1, 3)


def test_without_a_label_the_frontier_head_is_deduced(
    world: dict[int, dict[str, Any]],
) -> None:
    worker = world[520]
    assert worker["state"] == "stopped"
    assert worker["current_ticket"] == {
        "number": 521,
        "title": "Tableau (1/1) — tout",
        "declared": False,
    }
    assert (worker["done"], worker["total"]) == (0, 1)


def test_no_open_ticket_means_no_current_ticket(
    world: dict[int, dict[str, Any]],
) -> None:
    worker = world[530]
    assert worker["state"] == "dead"
    assert worker["current_ticket"] is None
    assert (worker["tickets"], worker["done"], worker["total"]) == ([], 0, 0)


def test_a_waiting_worker_whose_ticket_needs_a_human_is_blocked_with_the_reason(
    world: dict[int, dict[str, Any]],
) -> None:
    worker = world[510]
    assert worker["state"] == "blocked"
    assert worker["blocked_by"] == {
        "number": 511,
        "reason": "Question : garder X ou Y ?",
    }
    assert worker["current_ticket"] == {
        "number": 511,
        "title": "Classif (1/2) — modèle",
        "declared": False,
    }


def test_a_worker_on_a_permission_prompt_shows_what_it_asks(tmp_path: Path) -> None:
    """#984: never « idle » nor « blocked » by a label — its own state."""
    fake = github_world(recorded_world())
    fake.answer(
        *TMUX, "capture-pane", "-p", "-t", target("worker-510"),
        stdout=fixture("pane-claude-permission.txt"),
    )  # fmt: skip
    worker = workers_of(snapshot(fake, tmp_path))[510]
    assert worker["state"] == "asking"
    assert worker["asking"] == "permission: Bash command: rm node_modules"
    # its needs-human reason stays shown under the prompt
    assert worker["blocked_by"] == {
        "number": 511,
        "reason": "Question : garder X ou Y ?",
    }


def test_a_worker_says_since_when_what_it_cost_and_when_it_last_acted(
    world: dict[int, dict[str, Any]],
) -> None:
    worker = world[501]
    assert worker["provider"] == "claude"
    assert worker["branch"] == "agent/501-console-operateur"
    assert worker["started_at"] == 1790416800  # 2026-09-26T10:00:00Z
    assert worker["tokens"] == 3 + 100 + 450 + 30 + 345
    assert worker["last_activity"] == 1790413000
    assert world[510]["tokens"] is None  # never ran a Claude session


def test_the_vps_gauges(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path)
    assert data["generated_at"] == NOW
    assert data["vps"] == {
        "memory_available_mb": 2306048 // 1024,
        "memory_total_mb": 11710340 // 1024,
        "disk_available_kb": 40000000,
        "disk_total_kb": 100000000,
        "cleanup": None,
        "quota": {"provider": "claude", "used_percent": 42, "cap_percent": 80},
        "claude_quota": {
            "five_hour": None,
            "seven_day": None,
            "previous": {},
            "error": "no Claude status line received (status line not given to"
            " the workers: [quotas] claude_statusline = false)",
        },
        "actions_quota": {
            "used_minutes": None,
            "included_minutes": 2000,
            "used_percent": None,
            "resets_at": datetime(2026, 10, 1, tzinfo=UTC).timestamp(),
            "read_at": NOW,
            "error": "no billing token (/nonexistent/github-billing.token)",
            "projected_minutes": None,
            "slope": None,
        },
        # the CPU is a difference between two minutes: only serve measures it
        "cpu_percent": None,
        "cpu_at": None,
    }
    assert data["github"] == {"issues": "ok", "closed": "ok"}


def test_the_cpu_serve_measured_reaches_the_gauges(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path, cpu=(NOW - 30, 37.5))
    assert (data["vps"]["cpu_percent"], data["vps"]["cpu_at"]) == (37.5, NOW - 30)


def test_the_quotas_read_on_the_vps(tmp_path: Path) -> None:
    """#822, #823: the workers' status line recorded, the month's minutes
    asked with the billing token — both reach the dashboard."""
    from tracerworks.quotas import record_statusline

    quota = tmp_path / "claude-quota.json"
    week = {"used_percentage": 88, "resets_at": NOW + 3600}
    record_statusline(json.dumps({"rate_limits": {"seven_day": week}}), quota, NOW)
    token = tmp_path / "billing.token"
    token.write_text("t", encoding="utf-8")
    token.chmod(0o600)
    body = {"usageItems": [{"unitType": "Minutes", "grossQuantity": 500}]}
    data = snapshot(
        github_world(recorded_world()),
        tmp_path,
        quotas={"claude_quota_file": quota, "billing_token_file": token},
        billing=lambda url, t: (200, json.dumps(body).encode()),
    )
    vps = data["vps"]
    assert vps["claude_quota"]["seven_day"]["used_percent"] == 88
    assert vps["claude_quota"]["five_hour"] is None
    assert (
        vps["actions_quota"]["used_minutes"],
        vps["actions_quota"]["used_percent"],
    ) == (
        500,
        25.0,
    )
    assert '"t"' not in json.dumps(data)


def test_the_settings_tab_s_minutes_win_over_the_project_file_s(
    tmp_path: Path,
) -> None:
    """#2192: the gauge weighs the month's minutes against the state file's
    plan; a reading kept in the cache is weighed again, not asked again."""
    token = tmp_path / "billing.token"
    token.write_text("t", encoding="utf-8")
    token.chmod(0o600)
    body = {"usageItems": [{"unitType": "Minutes", "grossQuantity": 500}]}
    asked: list[str] = []

    def billing(url: str, t: str) -> tuple[int, bytes]:
        asked.append(url)
        return 200, json.dumps(body).encode()

    def gauge(state: State) -> dict[str, Any]:
        data = snapshot(
            github_world(recorded_world()),
            tmp_path,
            state=state,
            quotas={"billing_token_file": token},
            billing=billing,
        )
        quota: dict[str, Any] = data["vps"]["actions_quota"]
        return quota

    first = gauge(State())
    assert (first["included_minutes"], first["used_percent"]) == (2000, 25.0)
    raised = gauge(State(github_actions_included_minutes=4000))
    assert (raised["included_minutes"], raised["used_percent"]) == (4000, 12.5)
    assert len(asked) == 1, "the cached reading, weighed again"


def test_the_gauges_carry_their_end_of_period_projection(tmp_path: Path) -> None:
    """#848, #864: the hover detail says where the week and the month are
    heading, and along which slope — the Tendances rule, from the history
    base, computed here, never by the page."""
    from contextlib import closing

    from tracerworks import store
    from tracerworks.quotas import record_statusline
    from tracerworks.trends import DAY, month_bounds

    quota = tmp_path / "claude-quota.json"
    week = {"used_percentage": 88, "resets_at": NOW + 3600}
    hour = {"used_percentage": 10, "resets_at": NOW + 3600}
    lines = {"rate_limits": {"seven_day": week, "five_hour": hour}}
    record_statusline(json.dumps(lines), quota, NOW)
    token = tmp_path / "billing.token"
    token.write_text("t", encoding="utf-8")
    token.chmod(0o600)
    body = {"usageItems": [{"unitType": "Minutes", "grossQuantity": 500}]}
    base = recorded_settings(tmp_path).history_db
    base.parent.mkdir(parents=True)
    with closing(store.connect(base)) as db:
        store.insert(
            db,
            "claude_quota_sample",
            [{"period": "seven_day", "seen_at": NOW - 2 * DAY + i * DAY,
              "used_percent": 80 + 4 * i, "resets_at": NOW + 3600}
             for i in range(3)],
        )  # fmt: skip
        store.insert(
            db,
            "ci_run",
            [{"project": "o/r", "run_id": 1, "attempt": 1, "workflow": "CI",
              "workflow_path": "ci.yml", "event": "push", "branch": "b",
              "head_sha": "s", "conclusion": "success", "title": "", "url": ""}],
        )  # fmt: skip
        store.insert(
            db,
            "ci_job",
            [{"project": "o/r", "job_id": 1, "run_id": 1, "attempt": 1,
              "name": "j", "machine": "github", "conclusion": "success",
              "completed_at": NOW - 7 * DAY - 60, "billed_minutes": 70, "url": ""},
             {"project": "o/r", "job_id": 2, "run_id": 1, "attempt": 1,
              "name": "j", "machine": "github", "conclusion": "success",
              "completed_at": NOW - DAY, "billed_minutes": 700, "url": ""}],
        )  # fmt: skip
    vps = snapshot(
        github_world(recorded_world()),
        tmp_path,
        quotas={"claude_quota_file": quota, "billing_token_file": token},
        billing=lambda url, t: (200, json.dumps(body).encode()),
    )["vps"]
    seven_day = vps["claude_quota"]["seven_day"]
    # +8 over the 2 days of history, 4 each: 4 a day whatever their weights,
    # an hour left
    assert seven_day["slope"] == {
        "per_day": 4.0, "days": 2.0, "window_days": 7, "half_life_days": 2.0,
    }  # fmt: skip
    assert seven_day["projected_percent"] == round(88 + 4 / 24, 1)
    assert "projected_percent" not in vps["claude_quota"]["five_hour"]
    actions = vps["actions_quota"]
    # 700 a day ago (the job 7 days ago is just out), at 2^-1/2 of the
    # week's weight (half-life 48 h)
    per_day = 700 * 2**-0.5 / ((1 - 2**-3.5) * 2 / math.log(2))
    assert actions["slope"] == {
        "per_day": round(per_day, 2), "days": 7.0, "window_days": 7,
        "half_life_days": 2.0,
    }  # fmt: skip
    month_end = month_bounds(NOW)[1]
    assert actions["projected_minutes"] == round(
        500 + per_day * (month_end - NOW) / DAY, 1
    )


def test_no_history_base_no_projection_on_the_gauges(tmp_path: Path) -> None:
    token = tmp_path / "billing.token"
    token.write_text("t", encoding="utf-8")
    token.chmod(0o600)
    body = {"usageItems": [{"unitType": "Minutes", "grossQuantity": 500}]}
    actions = snapshot(
        github_world(recorded_world()),
        tmp_path,
        quotas={"billing_token_file": token},
        billing=lambda url, t: (200, json.dumps(body).encode()),
    )["vps"]["actions_quota"]
    assert actions["used_minutes"] == 500
    assert (actions["projected_minutes"], actions["slope"]) == (None, None)


def test_unreadable_github_says_unknown_never_an_empty_list(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    fake.answer(
        "gh",
        "issue",
        "list",
        prefix=True,
        returncode=1,
        stdout="error connecting to api.github.com",
    )
    data = snapshot(fake, tmp_path)
    assert data["github"] == {"issues": "unknown", "closed": "unknown"}
    assert (data["waiting"], data["queue"]) == (None, None)
    worker = workers_of(data)[501]
    assert worker["state"] == "active"  # tmux still says so
    assert worker["title"] is None
    assert worker["tickets"] is None
    assert worker["current_ticket"] is None
    assert (worker["done"], worker["total"]) == (None, None)


@pytest.mark.parametrize(
    "answer",
    [
        {"returncode": 1, "stdout": "HTTP 502"},
        # As many as the limit: some may be missing — unknown, not a wrong fraction.
        {
            "stdout": json.dumps(
                [issue(3000 + i, "t", "Parent : #501") for i in range(2000)]
            )
        },
    ],
)
def test_closed_tickets_unreadable_leave_the_fraction_unknown(
    tmp_path: Path, answer: dict[str, Any]
) -> None:
    fake = github_world(recorded_world())
    closed_list(fake, **answer)
    data = snapshot(fake, tmp_path)
    assert data["github"] == {"issues": "ok", "closed": "unknown"}
    worker = workers_of(data)[501]
    assert worker["tickets"] is None
    assert (worker["done"], worker["total"]) == (None, None)
    assert worker["current_ticket"] is not None  # the open list still says it
    queued = {s["number"]: s for s in data["queue"]}
    assert (queued[610]["tickets"], queued[610]["done"]) == (None, None)


def test_an_unreadable_comment_still_says_which_ticket_blocks(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    fake.answer(
        "gh",
        "issue",
        "view",
        "511",
        "-R",
        REPO,
        "--json",
        "comments",
        returncode=1,
        stdout="HTTP 502",
    )
    worker = workers_of(snapshot(fake, tmp_path))[510]
    assert worker["blocked_by"] == {"number": 511, "reason": None}


def test_the_command_prints_the_snapshot_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    out = io.StringIO()
    fake = github_world(recorded_world())
    env = Env(recorded_settings(tmp_path), runner=fake, now=lambda: NOW, out=out)
    assert run(env, ["snapshot"]) == EXIT_OK
    data = json.loads(out.getvalue())
    assert sorted(workers_of(data)) == [501, 510, 520, 530]


def test_the_panel_gets_the_start_of_the_spec_and_tickets_as_written(
    world: dict[int, dict[str, Any]],
) -> None:
    excerpt = world[501]["excerpt"]
    # Raw — no markdown rendered, no HTML escaped here — and cut between words.
    assert excerpt.startswith(
        "## Problem Statement\n\nSam répond à la main <b>partout</b>.\n\n"
        "## Solution\n\nUne console. Une console."
    )
    assert len(excerpt) <= 1500 and excerpt.endswith(("Une", "console."))
    assert [t["excerpt"] for t in world[501]["tickets"]] == [
        "Parent : #501",
        "Parent : #501",
        "Parent : #501. Dépend de : #503",
    ]


def test_a_short_body_is_the_whole_excerpt_and_a_long_one_is_cut() -> None:
    from tracerworks.snapshot import EXCERPT_CHARS, excerpt

    assert excerpt("  court  ") == "court"
    assert excerpt("") == ""
    long = "mot " * 1000
    cut = excerpt(long)
    assert 0 < len(cut) <= EXCERPT_CHARS
    assert long.startswith(cut) and cut.endswith("mot")
    paragraphs = "p" * 800 + "\n\n" + "q" * 1000
    assert excerpt(paragraphs) == "p" * 800
    heading = "a" * 800 + "\n\n## Solution\n\n" + "b" * 1000
    assert excerpt(heading) == "a" * 800


def test_the_page_knows_the_repository_and_the_worker_cap(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path)
    assert (data["repo"], data["max_workers"]) == (REPO, 2)


def test_the_snapshot_explains_occupied_sessions_even_outside_development(
    tmp_path: Path,
) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path)
    workers = workers_of(data)
    assert workers[510]["session_occupied"] is True
    assert (
        workers[510]["session_hold_reason"]
        == "2 unpushed commit(s), 2 uncommitted file(s)"
    )
    assert workers[520]["session_occupied"] is False
    assert workers[520]["session_hold_reason"] is None
    assert workers[530]["session_occupied"] is True


def test_unreadable_tmux_is_never_an_empty_factory(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    fake.answer(
        *TMUX,
        "list-windows",
        prefix=True,
        returncode=1,
        stdout="Operation not permitted",
    )
    for spec in (501, 510):
        fake.answer(
            "git", "-C", str(ROOT / str(spec)), "log", "-1", "--format=%ct", stdout="0"
        )
    data = snapshot(fake, tmp_path)
    assert data["order"] is None
    assert data["sessions_readable"] is False
    assert workers_of(data)[510]["session_occupied"] is None
    assert workers_of(data)[510]["state"] == "unknown"
    assert "unreadable" in data["relauncher"]["refusal"]


def test_a_parked_worker_is_shown_apart_from_a_stopped_one(tmp_path: Path) -> None:
    """#1025: #520 has no window; parked, it says so — and its session."""
    session = "0f1e2d3c-4b5a-4968-8776-655443322110"
    parking.park(recorded_settings(tmp_path), 520, session, 720, 0)
    workers = workers_of(snapshot(github_world(recorded_world()), tmp_path))
    assert workers[520]["state"] == "parked"
    assert workers[520]["parked"] == {"pull": 720, "session": session}
    assert workers[501]["state"] == "active"


def test_a_parked_pull_request_in_conflict_is_never_a_light_check(
    tmp_path: Path,
) -> None:
    """30/10, #2094: parked, its PR in conflict and no CI running — the card
    said « light check (its session closed) » for an hour."""
    parking.park(recorded_settings(tmp_path), 520, None, 815, 0)
    fake = github_world(recorded_world())
    pulls = [
        {**p, "statusCheckRollup": [], "mergeable": "CONFLICTING"}
        if p["number"] == 815
        else p
        for p in PULLS
    ]
    fake.answer(
        "gh", "pr", "list", "-R", REPO, "--state", "open", "--limit", "100",
        "--json", PR_FIELDS, stdout=json.dumps(pulls),
    )  # fmt: skip
    worker = workers_of(snapshot(fake, tmp_path))[520]
    assert (worker["phase"], worker["phase_cause"]) == ("parked", "parked: conflict")
    assert worker["phase_text"] == "parked (PR #815 in conflict, not resumed yet)"


def test_the_snapshot_carries_the_order_as_status_reads_it(tmp_path: Path) -> None:
    """#1823: #520 in CI entered first, then #510 and #501 in development;
    #530 (dead) has no entry yet — ranked at the snapshot's instant, nothing
    written: the page reads, the pass writes."""
    settings = recorded_settings(tmp_path)
    parking.park(settings, 520, "0f1e2d3c-4b5a-4968-8776-655443322110", 720, 0)
    for spec, at in ((520, 1), (510, 2), (501, 3)):
        order.enter(settings, spec, at)
    data = snapshot(github_world(recorded_world()), tmp_path)

    assert data["order"] == [
        {"spec": 520, "rank": 1, "role": "ci"},
        {"spec": 510, "rank": 2, "role": "development"},
        {"spec": 501, "rank": 3, "role": "development"},
        {"spec": 530, "rank": 4, "role": "development"},
    ]
    assert (data["in_flight"], data["in_flight_limit"]) == (4, 6)
    # its one line, in its chantier's words (#1729): its PR red, parked (#1730)
    assert data["order_lines"][0] == "1st #520 parked (PR #720 red, not resumed yet)"
    workers = workers_of(data)
    assert (workers[520]["rank"], workers[520]["role"]) == (1, "ci")
    assert (workers[530]["rank"], workers[530]["role"]) == (4, "development")
    assert workers[501]["set_aside"] is None
    assert [q["rank"] for q in data["queue"]] == list(range(5, 5 + len(data["queue"])))
    assert order.entries(settings) == {520: 1, 510: 2, 501: 3}


def test_a_spec_waiting_for_a_place_heads_the_queue(tmp_path: Path) -> None:
    """#2118: #600's `start` was refused for want of a place — the queue's
    first card, its rank and since when; no longer under Waiting on GitHub."""
    settings = recorded_settings(tmp_path)
    order.wait_for_place(settings, 600, 1_790_413_000)
    data = snapshot(github_world(recorded_world()), tmp_path)

    assert data["place_waits"] == [{"spec": 600, "rank": 1, "since": 1_790_413_000}]
    head, *rest = data["queue"]
    assert head["number"] == 600
    assert head["place_wait"] == {"rank": 1, "since": 1_790_413_000}
    assert all(q["place_wait"] is None for q in rest)
    assert [s["number"] for s in data["waiting"]["specs"]] == []
    assert [q["rank"] for q in data["queue"]] == list(
        range(data["in_flight"] + 1, data["in_flight"] + 1 + len(data["queue"]))
    )


def test_a_stopped_chantier_waiting_for_a_place_says_it_on_its_card(
    tmp_path: Path,
) -> None:
    """#1958 on 30/10: stopped, its worktree kept, its `start` refused — its
    worker card says it waits, the queue's specs coming after it."""
    settings = recorded_settings(tmp_path)
    order.wait_for_place(settings, 600, 1_790_413_000)
    order.wait_for_place(settings, 520, 1_790_412_000)
    data = snapshot(github_world(recorded_world()), tmp_path)

    workers = workers_of(data)
    assert workers[520]["place_wait"] == {"rank": 1, "since": 1_790_412_000}
    assert workers[501]["place_wait"] is None
    assert data["queue"][0]["place_wait"] == {"rank": 2, "since": 1_790_413_000}


def test_a_chantier_set_aside_says_for_whom(tmp_path: Path) -> None:
    settings = recorded_settings(tmp_path)
    order.set_aside(settings, 520, "0f1e2d3c-4b5a-4968-8776-655443322110", 501, 99)
    order.enter(settings, 520, 0)
    workers = workers_of(snapshot(github_world(recorded_world()), tmp_path))

    assert (workers[520]["rank"], workers[520]["role"]) == (1, "set_aside")
    assert workers[520]["set_aside"] == {
        "session": "0f1e2d3c-4b5a-4968-8776-655443322110",
        "for": 501,
        "set_aside_at": 99,
    }


def test_an_unreadable_order_is_unknown_never_room(tmp_path: Path) -> None:
    """A history base that cannot be opened (a folder in its place): the
    order `null`, never an empty list — which would read « room »."""
    fake = github_world(recorded_world())
    settings = replace(recorded_settings(tmp_path), history_db=tmp_path)
    data = build_snapshot(
        fake, settings, Env(settings, runner=fake).tmux, State(), NOW,
        tmp_path / "projects",
    )  # fmt: skip
    assert (data["order"], data["in_flight"], data["in_flight_limit"]) == (
        None,
        None,
        None,
    )
    assert workers_of(data)[501]["rank"] is None
    assert all(q["rank"] is None for q in data["queue"] or [])


# --- En attente GitHub, File --------------------------------------------------------


def test_each_ready_spec_is_in_one_post(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path)
    assert [s["number"] for s in data["waiting"]["specs"]] == [600]
    in_workshop = {w["spec"] for w in data["workers"]}
    queued = [s["number"] for s in data["queue"]]
    assert not in_workshop & (set(queued) | {600})
    # Tickets (#612, labelled ready too) are never specs.
    assert 612 not in queued


def test_the_queue_is_in_start_next_order(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path)
    assert [(s["number"], s["prioritized"]) for s in data["queue"]] == [
        (620, True),
        (605, False),
        (610, False),
    ]
    issues = parse_issues(json.dumps(WAITING_AND_QUEUE), RULES)
    ranked = sorted(
        (i for i in issues if QUEUE.admits(i) and i.parent is None), key=QUEUE.rank
    )
    assert [i.number for i in ranked] == [620, 605, 610]


def test_a_spec_shows_its_start_its_tickets_and_its_fraction(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path)
    queued = {s["number"]: s for s in data["queue"]}
    assert [(t["number"], t["status"]) for t in queued[610]["tickets"]] == [
        (611, "done"),
        (612, "todo"),
    ]
    assert (queued[610]["done"], queued[610]["total"]) == (1, 2)
    assert queued[605]["tickets"] == []  # « pas encore créés »
    [waiting] = data["waiting"]["specs"]
    assert waiting == {
        "number": 600,
        "title": "Spec — Écrite d'avance",
        "excerpt": "## Problem Statement\n\nPlus tard.",
        "labels": ["ready-for-agent"],
        "prioritized": False,
        "is_starred": False,
        "created_at": 1790384400,  # 2026-09-26T01:00Z
        "tickets": [],
        "done": 0,
        "total": 0,
        "open_tickets": 0,
        "waits_launch": False,  # nothing delivered yet: not a tranche waiting
    }


def test_a_spec_kept_open_without_its_launch_label_says_its_tranche_waits(
    tmp_path: Path,
) -> None:
    """#1926: `clean` keeps #610 open (#611 done, #612 open) and takes
    `to-launch` off — out of the queue, the workshop says why it waits."""
    fake = github_world(recorded_world())
    kept = [
        {**i, "labels": [{"name": "ready-for-agent"}]} if i["number"] == 610 else i
        for i in OPEN + WAITING_AND_QUEUE
    ]
    fake.answer(
        "gh", "issue", "list", "-R", REPO, "--state", "open", "--limit", "1000",
        "--json", FIELDS, stdout=json.dumps(kept),
    )  # fmt: skip
    data = snapshot(fake, tmp_path)
    assert 610 not in [s["number"] for s in data["queue"]]
    shown = {s["number"]: s for s in data["waiting"]["specs"]}
    assert shown[610]["waits_launch"] is True
    assert shown[600]["waits_launch"] is False  # never launched: no tranche


#: Open issues of every nature, none launched (#857).
NATURED = [
    issue(800, "Thème sombre", labels=("idea",)),
    issue(801, "Mesurer la latence", labels=("task",)),
    issue(802, "Date fausse", labels=("bug",)),
    issue(803, "Plan de reprise", labels=("spec",)),
    issue(804, "Reprise (1/2) — export", "## Parent\n\n#803", ("ticket",)),
    issue(805, "Reprise (2/2) — import", "Parent : #803"),
    issue(806, "Spec — Sans étiquette"),
    issue(807, "Sans étiquette (1/1) — tout", "## Parent\n\n#806"),
    issue(808, "Tranche orpheline", "Parent : #9999"),
    issue(809, "Autre bug", labels=("bug", "ready-for-agent")),
    issue(810, "Erreur de rendu", "Parent : #802", ("bug",)),
    issue(811, "Spec — en fait un bug", labels=("bug",)),
    issue(812, "Écran (3/3) — bug trouvé", "Parent : #501", ("bug",)),
    issue(813, "Deux natures", labels=("idea", "bug")),
]


def natured_world(tmp_path: Path) -> dict[str, Any]:
    fake = github_world(recorded_world())
    fake.answer(
        "gh", "issue", "list", "-R", REPO, "--state", "open", "--limit", "1000",
        "--json", FIELDS, stdout=json.dumps(OPEN + WAITING_AND_QUEUE + NATURED),
    )  # fmt: skip
    return snapshot(fake, tmp_path)


def test_waiting_on_github_shows_every_open_issue_grouped_by_nature(
    tmp_path: Path,
) -> None:
    """The operator, 01/10: everything neither queued nor in the workshop, in groups —
    specs, bugs, tasks, ideas, then what has no nature; newest first."""
    waiting = natured_world(tmp_path)["waiting"]
    assert list(waiting) == ["specs", "bugs", "tasks", "ideas", "to_sort"]
    assert {group: [i["number"] for i in rows] for group, rows in waiting.items()} == {
        "specs": [806, 803, 600],
        "bugs": [813, 811, 809, 802],  # two natures: the first in NATURES
        "tasks": [801],
        "ideas": [800],
        # No nature, and a ticket whose spec is not open: never lost.
        "to_sort": [808, 701, 700],
    }


def test_a_waiting_spec_counts_its_open_tickets_never_listed_apart(
    tmp_path: Path,
) -> None:
    waiting = natured_world(tmp_path)["waiting"]
    specs = {s["number"]: s for s in waiting["specs"]}
    assert (specs[803]["open_tickets"], specs[806]["open_tickets"]) == (2, 1)
    shown = {i["number"] for rows in waiting.values() for i in rows}
    assert not shown & {804, 805, 807}
    # A ticket of a queued or workshop spec is not waiting either — whatever
    # its label: #812 is a bug of #501's chantier, on its worker's card.
    assert not shown & {503, 504, 511, 512, 521, 612, 812}
    # An issue naming an open parent sits under it, even a bug under a bug.
    assert 810 not in shown
    assert next(b for b in waiting["bugs"] if b["number"] == 802)["open_tickets"] == 1


def test_a_labelled_nature_beats_the_deduced_one(tmp_path: Path) -> None:
    """#811's title says spec, its label says bug: a bug."""
    waiting = natured_world(tmp_path)["waiting"]
    assert waiting["bugs"][1] == {
        "number": 811,
        "title": "Spec — en fait un bug",
        "labels": ["bug"],
        "is_starred": False,
        "created_at": None,
        "open_tickets": 0,
    }


def starred_world(tmp_path: Path, starred: set[int]) -> dict[str, Any]:
    """The natured world, plus a queued spec newer than the others; the
    operator's star (#1875) on the issues of `starred`."""
    newer = issue(630, "Spec — Récente", labels=("ready-for-agent", "to-launch"))

    def star(raw: dict[str, Any]) -> dict[str, Any]:
        if raw["number"] not in starred:
            return raw
        return {**raw, "labels": [*raw["labels"], {"name": "starred"}]}

    world = [star(i) for i in OPEN + WAITING_AND_QUEUE + NATURED + [newer]]
    fake = github_world(recorded_world())
    fake.answer(
        "gh", "issue", "list", "-R", REPO, "--state", "open", "--limit", "1000",
        "--json", FIELDS, stdout=json.dumps(world),
    )  # fmt: skip
    return snapshot(fake, tmp_path)


def test_starred_issues_come_first_in_their_waiting_group(tmp_path: Path) -> None:
    """The operator, 26/10: the starred first in their list, then newest first."""
    waiting = starred_world(tmp_path, {802, 811, 700, 600})["waiting"]
    assert {group: [i["number"] for i in rows] for group, rows in waiting.items()} == {
        "specs": [600, 806, 803],
        "bugs": [811, 802, 813, 809],
        "tasks": [801],
        "ideas": [800],
        "to_sort": [700, 808, 701],
    }
    flags = {i["number"]: i["is_starred"] for rows in waiting.values() for i in rows}
    assert {n for n, on in flags.items() if on} == {802, 811, 700, 600}
    # the star is said by is_starred, never repeated in the labels
    assert not any("starred" in i["labels"] for rows in waiting.values() for i in rows)


def test_nothing_starred_leaves_every_group_in_its_order(tmp_path: Path) -> None:
    waiting = starred_world(tmp_path, set())["waiting"]
    assert [i["number"] for i in waiting["bugs"]] == [813, 811, 809, 802]
    assert not any(i["is_starred"] for rows in waiting.values() for i in rows)


def test_the_star_never_orders_the_queue(tmp_path: Path) -> None:
    """Counter-proof: a newer starred spec stays where start-next takes it."""
    queue = starred_world(tmp_path, {630, 610})["queue"]
    assert [(s["number"], s["is_starred"]) for s in queue] == [
        (620, False),
        (605, False),
        (610, True),
        (630, True),
    ]


def test_queued_and_workshop_issues_are_not_waiting(tmp_path: Path) -> None:
    data = natured_world(tmp_path)
    shown = {i["number"] for rows in data["waiting"].values() for i in rows}
    assert not shown & {s["number"] for s in data["queue"]}
    assert not shown & {w["spec"] for w in data["workers"]}


def test_closed_tickets_are_read_since_the_oldest_open_spec(tmp_path: Path) -> None:
    """A ticket is never older than its spec: the list stays short as the
    repository grows, instead of hitting its limit and going unknown."""
    dated = [
        {**i, "createdAt": i.get("createdAt") or "2026-09-10T08:00:00Z"}
        for i in OPEN + WAITING_AND_QUEUE
    ]
    fake = github_world(recorded_world())
    fake.answer(
        "gh", "issue", "list", "-R", REPO, "--state", "open", "--limit", "1000",
        "--json", FIELDS, stdout=json.dumps(dated),
    )  # fmt: skip
    fake.answer(
        "gh", "issue", "list", "-R", REPO, "--state", "closed", "--limit", "2000",
        "--search", "created:>=2026-09-01", "--json", CLOSED_FIELDS,
        stdout=json.dumps(CLOSED),
    )  # fmt: skip
    data = snapshot(fake, tmp_path)
    assert data["github"]["closed"] == "ok"
    assert workers_of(data)[501]["done"] == 1


# --- the queue relauncher's next pass (#869) ----------------------------------

ROOMY = State(
    claude_weekly_used=42,
    max_workers=9,
    min_available_memory_mb=1000,
    min_available_memory_mb_runner_busy=1000,
)


def test_the_next_pass_is_read_from_the_timer(tmp_path: Path) -> None:
    data = snapshot(github_world(recorded_world()), tmp_path, state=ROOMY)
    assert data["relauncher"] == {"next_at": NOW + 300, "refusal": None}


def test_the_refusal_is_start_nexts_own_guards(tmp_path: Path) -> None:
    """Three windows (#501, #510, #530 dead) for two sessions, and the machine
    under its threshold while the runner runs a CI: the pass will launch
    nothing, and the reasons are the ones `start-next` would print — the
    entries it would write included, though the page writes none."""
    data = snapshot(github_world(recorded_world()), tmp_path)
    assert data["relauncher"]["refusal"] == (
        "no new worker: 3/2 sessions busy: #501 (1st) · #510 (2nd) · #530 (3rd);"
        " available memory 2252 MB < threshold 3500 MB"
        " (runner vps-1, vps-2, vps-3 busy: CI running)"
    )


def test_the_quota_cap_refuses_the_pass_too(tmp_path: Path) -> None:
    capped = replace(ROOMY, claude_weekly_used=95)
    data = snapshot(github_world(recorded_world()), tmp_path, state=capped)
    assert data["relauncher"]["refusal"] == (
        "Claude quota 95/80% (declared): cap reached and"
        " the Codex route is not open (codex.available: false)"
    )


def test_the_queue_paused_refuses_the_pass_and_is_said(tmp_path: Path) -> None:
    """#2192: the pause is start-next's first refusal; the page says it on top."""
    paused = replace(ROOMY, queue_paused=True)
    data = snapshot(github_world(recorded_world()), tmp_path, state=paused)
    assert data["queue_paused"] is True
    assert data["relauncher"]["refusal"].startswith("queue paused (settings)")
    calm = snapshot(github_world(recorded_world()), tmp_path, state=ROOMY)
    assert calm["queue_paused"] is False


def test_an_unreadable_timer_gives_no_hour(tmp_path: Path) -> None:
    fake = github_world(recorded_world())
    timer_answer(fake, "Failed to connect to bus", returncode=1)
    data = snapshot(fake, tmp_path, state=ROOMY)
    assert data["relauncher"] == {"next_at": None, "refusal": None}


def test_a_queue_with_no_free_head_ticket_refuses_the_pass(tmp_path: Path) -> None:
    """Room enough, but every queued spec's head ticket is blocked on GitHub:
    `start-next` would find no candidate, and the page says so (#869)."""
    fake = github_world(recorded_world())
    # #610's open ticket #612 waits on a native dependency; #605, #620: none yet
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/issues/612",
        "--jq",
        ".issue_dependencies_summary.blocked_by // 0",
        stdout="1\n",
    )
    data = snapshot(fake, tmp_path, state=ROOMY)
    assert data["queue"], "the queue is not empty"
    assert data["relauncher"]["refusal"] == (
        "no spec ready-for-agent + to-launch with a free head ticket"
    )


def test_the_workshop_shows_a_text_left_in_a_field_at_rest(tmp_path: Path) -> None:
    """#2003: the field of a worker at rest, read with its colours."""
    fake = github_world(recorded_world())
    fake.answer(
        *TMUX, "capture-pane", "-p", "-e", "-t", target("worker-510"),
        stdout=fixture("pane-claude-field-taken.txt"),
    )  # fmt: skip
    workers = workers_of(snapshot(fake, tmp_path))
    assert workers[510]["pending_field"] == "ouvre la PR de #1587 et #1588 maintenant"
    assert workers[501]["pending_field"] is None  # working: its field not read


# --- who holds a red CI (#2057) ---------------------------------------------

RED_AT = 1790400000.0  # its CI ended red then
REST_AT = RED_AT + 600  # its worker ended its turn ten minutes later
WAITS = Waits(human_minutes=30, parked_red_minutes=120, memory_resume_mb=2048)
MIN = 60


def held(red: Red, now: float = RED_AT + 5 * MIN) -> tuple[str | None, float | None]:
    return red_holder("failure", red, now, WAITS)


@pytest.mark.parametrize(
    ("red", "expected"),
    [
        # its worker works on it: it holds the red, whatever its rest
        (Red(RED_AT, worker=WorkerState.WORKING, rested_at=REST_AT), ("worker", None)),
        # parked, its resume deadline running (#1730)
        (Red(RED_AT, worker=WorkerState.STOPPED, parked_at=RED_AT), ("parked", None)),
        # a machine's red, its re-run asked after the red (#1792): worker or not
        (Red(RED_AT, RED_AT + MIN, WorkerState.WAITING), ("machine", None)),
        (Red(RED_AT, RED_AT + MIN), ("machine", None)),
        # a re-run asked before this red: this red is a new one, nobody's
        (
            Red(RED_AT, RED_AT - MIN, WorkerState.WAITING, rested_at=REST_AT),
            (None, REST_AT),
        ),
        # at rest or stopped since after the red: nobody, since its rest
        (Red(RED_AT, worker=WorkerState.WAITING, rested_at=REST_AT), (None, REST_AT)),
        (Red(RED_AT, worker=WorkerState.STOPPED, rested_at=REST_AT), (None, REST_AT)),
        # a question or a death: nobody (the banner says the worker)
        (Red(RED_AT, worker=WorkerState.ASKING, rested_at=REST_AT), (None, REST_AT)),
        # at rest since before the red, or its rest unknown: since the red
        (Red(RED_AT, worker=WorkerState.WAITING, rested_at=RED_AT - 9), (None, RED_AT)),
        (Red(RED_AT, worker=WorkerState.WAITING), (None, RED_AT)),
        # no worker: nobody, since the red
        (Red(RED_AT), (None, RED_AT)),
    ],
)  # fmt: skip
def test_a_red_ci_is_held_by_its_worker_its_parking_or_its_re_run(
    red: Red, expected: tuple[str | None, float | None]
) -> None:
    assert held(red) == expected


def test_a_parking_holds_the_red_until_its_resume_deadline() -> None:
    red = Red(RED_AT, worker=WorkerState.STOPPED, parked_at=RED_AT + MIN)
    assert held(red, RED_AT + 120 * MIN) == ("parked", None)
    assert held(red, RED_AT + 121 * MIN) == (None, RED_AT + MIN)


def test_a_re_run_github_never_ran_stops_holding_the_red() -> None:
    """#1919: a re-run asked, never run by GitHub — not held forever."""
    red = Red(RED_AT, RED_AT + MIN)
    assert held(red, RED_AT + 15 * MIN) == ("machine", None)
    assert held(red, RED_AT + 16 * MIN) == (None, RED_AT + MIN)


@pytest.mark.parametrize("ci", ["success", "running", "queued", "none"])
def test_a_green_or_running_ci_has_no_red_to_hold(ci: str) -> None:
    assert red_holder(ci, Red(None), RED_AT, WAITS) == (None, None)


def test_each_open_pr_says_who_holds_its_red(tmp_path: Path) -> None:
    """#815 is red (ended 06:05), its chantier #520 stopped: nobody holds it;
    the others are not red."""
    data = snapshot(github_world(recorded_world()), tmp_path)
    pulls = {p["number"]: p for p in data["control"]["pull_requests"]}
    red_at = datetime(2026, 9, 26, 6, 5, tzinfo=UTC).timestamp()
    assert data["red_unowned_minutes"] == 15  # [waits], its default
    assert pulls[815]["red_owner"] is None
    assert pulls[815]["red_unowned_since"] == red_at  # #520 at rest since before it
    held = {n: (p["red_owner"], p["red_unowned_since"]) for n, p in pulls.items()}
    assert {n: h for n, h in held.items() if n != 815} == {
        817: (None, None), 814: (None, None), 816: (None, None),
    }  # fmt: skip


def test_a_machine_red_being_re_run_is_held(tmp_path: Path) -> None:
    settings = recorded_settings(tmp_path)
    with closing(store.connect(settings.history_db)) as db:
        by = machine_red.NO_WORKER
        # asked five minutes ago, after the red: GitHub has time to run it
        machine_red.record(db, REPO, 815, "a" * 40, NOW - 300, by)
    fake = github_world(recorded_world())
    fake.answer(
        "gh", "run", "list", "--repo", REPO, "--commit", "a" * 40,
        "--json", "attempt,startedAt", "--limit", "100", stdout="[]",
    )  # fmt: skip
    pull = next(
        p
        for p in snapshot(fake, tmp_path)["control"]["pull_requests"]
        if p["number"] == 815
    )
    assert (pull["red_owner"], pull["red_unowned_since"]) == ("machine", None)


def _ci_run(
    db: Any, run_id: int, branch: str, conclusion: str, attempt: int = 1, **kw: Any
) -> None:
    store.insert(db, "ci_run", [{
        "project": REPO, "run_id": run_id, "attempt": attempt, "workflow": "CI",
        "workflow_path": kw.get("path", ".github/workflows/ci.yml"),
        "event": "pull_request", "branch": branch, "head_sha": kw.get("sha", "s1"),
        "pr": None, "conclusion": conclusion, "created_at": 1, "started_at": 1,
        "finished_at": 2, "title": "", "url": f"u{run_id}-{attempt}"}])  # fmt: skip


def test_each_open_pr_counts_its_reds_of_every_commit(tmp_path: Path) -> None:
    """#2058: the CI runs of its branch ended red, every commit and every
    attempt — a machine's red re-run counts twice, a run out of time too;
    another workflow, another branch, a green, a cancelled one never."""
    settings = recorded_settings(tmp_path)
    with closing(store.connect(settings.history_db)) as db:
        _ci_run(db, 1, "agent/520-dashboard", "failure", sha="s1")
        _ci_run(db, 1, "agent/520-dashboard", "failure", attempt=2, sha="s1")
        _ci_run(db, 2, "agent/520-dashboard", "success", sha="s2")
        _ci_run(db, 3, "agent/520-dashboard", "failure", sha="s3")
        _ci_run(db, 4, "agent/520-dashboard", "cancelled", sha="s4")
        _ci_run(db, 8, "agent/520-dashboard", "timed_out", sha="s5")
        _ci_run(db, 5, "agent/520-dashboard", "failure", path=".github/workflows/x.yml")
        _ci_run(db, 6, "fix/scopes", "success")
        _ci_run(db, 7, "docs/elsewhere", "failure")
    data = snapshot(github_world(recorded_world()), tmp_path)
    reds = {p["number"]: p["reds"] for p in data["control"]["pull_requests"]}
    assert reds == {815: 4, 814: 0, 816: 0, 817: 0}
