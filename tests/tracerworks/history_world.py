"""What happened around the recorded world, for Livré and the events (#811).

Now is 2026-09-26T07:10Z; the window is 48 h. In it: PR #812 merged, deploy
#300, ticket #502 closed, spec #600 created, chantier #620 launched, run #91
red. Out of it (older): PR #790, deploy #302, ticket #611, spec #605, the
launch of #605, run #80. Deploy run #301 failed: not a delivery.
"""

from __future__ import annotations

import json
from typing import Any

from tests.tracerworks.fakes import FakeRunner

REPO = "owner/repo"
MERGED_FIELDS = "number,title,mergedAt,url"

RELAUNCHER_LOG = """--- 2026-09-20T03:00:00+00:00
#605 Spec — Plus ancienne — ticket de tête #606 — t
worker-605 lancé (claude)
--- 2026-09-26T03:00:00+00:00
tracerworks: refusé — pas de nouveau worker : runner occupé
--- 2026-09-26T03:05:00+00:00
#620 Spec — Urgente — head ticket #621 — t
worker-620 launched (claude)
#999 kept — worker-999 launched (claude) is quoted here, not a launch
"""
# 605 is a journal of before #850, in French; 620 one of after: both are launches


def merged(n: int, at: str) -> dict[str, Any]:
    return {
        "number": n,
        "title": f"PR {n}",
        "mergedAt": at,
        "url": f"https://github.com/{REPO}/pull/{n}",
    }


def deploy(run_id: int, conclusion: str, at: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "Deploy",
        "display_title": "Deploy",
        "conclusion": conclusion,
        "updated_at": at,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "head_branch": "main",
        "pull_requests": [],
    }


def history_world(fake: FakeRunner) -> FakeRunner:
    fake.answer(
        "gh",
        "pr",
        "list",
        "-R",
        REPO,
        "--state",
        "merged",
        "--limit",
        "100",
        "--json",
        MERGED_FIELDS,
        stdout=json.dumps(
            [merged(812, "2026-09-26T06:00:00Z"), merged(790, "2026-09-23T10:00:00Z")]
        ),
    )
    fake.answer(
        "gh",
        "api",
        f"repos/{REPO}/actions/workflows/deploy.yml/runs?per_page=100",
        stdout=json.dumps(
            {
                "workflow_runs": [
                    deploy(300, "success", "2026-09-26T06:10:00Z"),
                    deploy(301, "failure", "2026-09-26T05:00:00Z"),
                    deploy(302, "success", "2026-09-22T10:00:00Z"),
                ]
            }
        ),
    )
    # The failed runs are in the dated list of `control_world` (#825).
    return fake
