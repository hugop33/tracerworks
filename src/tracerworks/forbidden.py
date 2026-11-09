"""What a worker is denied (#1529): the project's rules, plus the tool's own.

The project lists its rules (`[workers] disallowed_tools`, reviewed like
code). The tool adds, derived from the labels of the project file, the rules
that keep a worker from putting or removing the launch label, the priority
label or the operator's star (#1875): a fine-grained token acts under the
operator's login, so the login of whoever labelled a spec cannot tell a
worker from the operator — only this can. Derived, not copied: renaming a
label in the project file moves them.

Claude Code permission rules: `*` matches any run of characters, spaces
included; a Bash rule holds for each command of a compound line.
"""

from __future__ import annotations

from tracerworks.settings import Settings


def label_rules(label: str) -> list[str]:
    """Putting, removing, or creating with, `label` — and the label itself."""
    return [
        f"Bash(gh *--add-label*{label}*)",
        f"Bash(gh *--remove-label*{label}*)",
        f"Bash(gh issue create*{label}*)",
        f"Bash(gh label *{label}*)",
    ]


def denied(settings: Settings) -> list[str]:
    rules = list(settings.disallowed_tools)
    for label in (settings.launch_label, settings.priority_label, settings.star_label):
        rules += [rule for rule in label_rules(label) if rule not in rules]
    # THE MERGE QUEUE'S FULL MODE IS TRACERWORKS'S ALONE (#1716): a worker that put
    # it on would run the whole suite at each push. Putting and removing only —
    # a pull request's text may name it.
    testing = settings.merge_queue.testing_label
    rules += [
        rule for rule in label_rules(testing) if "-label" in rule and rule not in rules
    ]
    return rules
