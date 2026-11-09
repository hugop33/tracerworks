"""The nature of an open issue, by which « Waiting on GitHub » sorts it (#857).

Five natures, each a label of the project (`[natures]`): a spec, a ticket (a
slice of a spec, naming its parent), a bug, a task, an idea. A nature label
decides; without one, the nature is deduced so that nothing is lost — a title
« Spec — » is a spec, a body naming its parent is a ticket — and otherwise the
issue has none: it is to sort.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tracerworks.conventions import SPEC_PREFIX
from tracerworks.frontier import Issue

Nature = Literal["spec", "ticket", "bug", "task", "idea"]

#: When an issue carries several nature labels, the first of this order wins.
NATURES: tuple[Nature, ...] = ("spec", "ticket", "bug", "task", "idea")


@dataclass(frozen=True)
class Natures:
    """The label of each nature, from the project file."""

    spec: str
    ticket: str
    bug: str
    task: str
    idea: str

    def of(self, issue: Issue) -> Nature | None:
        for nature in NATURES:
            if getattr(self, nature) in issue.labels:
                return nature
        if SPEC_PREFIX.match(issue.title):
            return "spec"
        if issue.parent is not None:
            return "ticket"
        return None
