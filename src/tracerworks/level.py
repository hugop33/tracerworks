"""The model and the effort a chantier's worker is started with (#2251, spec #2250).

Chosen ON THE SPEC, by labels, like the CI's mode (`carried_labels`): one
label per dimension, its prefix and the models allowed named by the project
(`[workers] model_label_prefix`, `effort_label_prefix`, `models`), the
default too (`[workers] model`, `effort`). The efforts are Claude Code's own
levels, known here: they are the tool's words, not the project's.

Read at EVERY start of a worker — a launch, the resume of a branch, the resume
of a chantier parked or set aside — so that a label changed between two starts
is the one passed at the next; never inside a running session.

A LABEL THAT CANNOT BE READ NEVER STOPS THE FACTORY. A value unknown, or two
labels of one dimension, leave that dimension at its default, and the reason
is said with the launch: a refusal would keep a chantier waiting all night.

Passed as `--model` and `--effort` on the command line, never through the
injected settings' `effortLevel`: on 03/11 the 249 workers' sessions all sent
`medium` while the VPS's settings said `high` — the option is what is obeyed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: Claude Code's effort levels (`claude --effort`), lowest first.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Where a dimension's value comes from.
LABEL = "label"
DEFAULT = "default"


@dataclass(frozen=True)
class LevelRules:
    """The project's `[workers]` keys of the level, checked at load."""

    model: str  # the default model, an alias (`claude --model`)
    effort: str  # the default effort, one of EFFORTS
    models: tuple[str, ...]  # the models a label may ask for
    model_prefix: str  # a model's label: this prefix, then the model
    effort_prefix: str  # an effort's label: this prefix, then the effort


@dataclass(frozen=True)
class Choice:
    """One dimension: its value, where it comes from, and — when its default
    stands although the spec has a label of it — why."""

    value: str
    source: str  # LABEL or DEFAULT
    label: str | None = None  # the label it comes from
    anomaly: str | None = None  # why the spec's label(s) were not followed

    def describe(self, dimension: str) -> str:
        if self.source == LABEL:
            return f"{dimension} {self.value} (label {self.label})"
        if self.anomaly is not None:
            return f"{dimension} {self.value} (default — {self.anomaly})"
        return f"{dimension} {self.value} (default)"


@dataclass(frozen=True)
class Level:
    model: Choice
    effort: Choice

    def args(self) -> list[str]:
        """The words of a Claude worker's command line."""
        return ["--model", self.model.value, "--effort", self.effort.value]

    def describe(self) -> str:
        """« model opus (default), effort xhigh (label effort-xhigh) »."""
        return f"{self.model.describe('model')}, {self.effort.describe('effort')}"

    @property
    def anomalies(self) -> list[str]:
        return [c.anomaly for c in (self.model, self.effort) if c.anomaly]


def choose(labels: Iterable[str], rules: LevelRules) -> Level:
    """The level the spec's labels ask for, each dimension on its own."""
    present = list(dict.fromkeys(labels))
    return Level(
        model=_dimension(present, rules.model_prefix, rules.models, rules.model),
        effort=_dimension(present, rules.effort_prefix, EFFORTS, rules.effort),
    )


def unreadable(rules: LevelRules, why: str) -> Level:
    """The default of each dimension, the spec's labels unread (GitHub
    unreachable): said, never a resume refused for it."""
    anomaly = f"labels unreadable: {why}"
    return Level(
        model=Choice(rules.model, DEFAULT, anomaly=anomaly),
        effort=Choice(rules.effort, DEFAULT, anomaly=anomaly),
    )


def _dimension(
    labels: Sequence[str], prefix: str, allowed: Sequence[str], default: str
) -> Choice:
    named = sorted(label for label in labels if label.startswith(prefix))
    if not named:
        return Choice(default, DEFAULT)
    unknown = [label for label in named if label[len(prefix) :] not in allowed]
    if unknown:
        return Choice(default, DEFAULT, anomaly=f"{', '.join(unknown)} unknown")
    if len(named) > 1:
        return Choice(
            default, DEFAULT, anomaly=f"{' and '.join(named)} contradict each other"
        )
    [label] = named
    return Choice(label[len(prefix) :], LABEL, label=label)


# --- asked and seen (#2252) ---------------------------------------------------------
#
# ASKED is what the worker's last start passed, recorded by tracerworks then: the
# spec's labels may have changed since, but they count only at the next start
# — and `status` reads no GitHub (#2137). SEEN is what its session's log says
# it really sent (`usage.ChantierUsage.seen`). A gap between the two is marked:
# on 03/11, `effortLevel: high` was asked for weeks while `medium` was sent.


def _record_path(folder: Path, spec: int) -> Path:
    return folder / "levels" / f"{spec}.json"


def record(folder: Path, spec: int, level: Level) -> None:
    """What `spec`'s worker was started with, kept for `status` and the
    dashboard until its next start."""
    path = _record_path(folder, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(level)), encoding="utf-8")


def recorded(folder: Path, spec: int) -> Level | None:
    """What `spec`'s worker was last started with; None: started before
    #2251, or never — unknown, never guessed."""
    try:
        data = json.loads(_record_path(folder, spec).read_text(encoding="utf-8"))
        return Level(model=Choice(**data["model"]), effort=Choice(**data["effort"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def same_model(alias: str, model: str) -> bool:
    """`opus` is `claude-opus-5-5`: an alias names a family, the log the
    model it resolved to."""
    family = alias.lower().split("[", 1)[0]  # `opus[1m]`: a context, same model
    words = re.split(r"[^a-z0-9]+", model.lower())
    return alias.lower() == model.lower() or family in words


def view(asked: Level | None, seen: tuple[str | None, str | None]) -> dict[str, Any]:
    """`model` and `effort`, each asked (with where it comes from) and seen,
    and whether the two differ — both known only."""
    out: dict[str, Any] = {}
    for dimension, choice, saw in (
        ("model", asked.model if asked else None, seen[0]),
        ("effort", asked.effort if asked else None, seen[1]),
    ):
        differs = None
        if choice is not None and saw is not None:
            differs = (
                not same_model(choice.value, saw)
                if dimension == "model"
                else choice.value != saw
            )
        out[dimension] = {
            "asked": choice.value if choice else None,
            "source": choice.source if choice else None,
            "label": choice.label if choice else None,
            "anomaly": choice.anomaly if choice else None,
            "seen": saw,
            "differs": differs,
        }
    return out


def describe_view(shown: dict[str, Any]) -> str | None:
    """`status`'s words: « opus · high », its gap marked, what is unknown
    said; None when nothing is known — nothing asked, nothing sent."""
    said = _describe_view(shown)
    if said is None:
        return None
    sides = (shown["model"], shown["effort"])
    anomalies = [side["anomaly"] for side in sides if side["anomaly"]]
    return " — ".join([said, *anomalies])


def _describe_view(shown: dict[str, Any]) -> str | None:
    model, effort = shown["model"], shown["effort"]
    asked = f"{model['asked'] or '?'} · {effort['asked'] or '?'}"
    seen = f"{model['seen'] or '?'} · {effort['seen'] or '?'}"
    if model["asked"] is None and effort["asked"] is None:
        if model["seen"] is None and effort["seen"] is None:
            return None
        return f"{seen} sent (asked unknown)"
    if model["differs"] or effort["differs"]:
        return f"{asked} asked — MISMATCH: {seen} sent"
    if model["seen"] is None or effort["seen"] is None:
        return f"{asked} (sent: {seen})"
    return asked
