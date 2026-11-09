"""Every module of tracerworks imports alone, in a fresh process (#1923).

A cycle of imports hides as long as another module happens to be loaded
first: `pytest tests/tracerworks/test_recorder.py` run alone could not even be
collected (25/10 — `recorder` → `snapshot` → `merge_queue` →
`recorder.failed_tests`), while the whole suite was green.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import tracerworks

PACKAGE = Path(tracerworks.__file__).parent
MODULES = sorted(
    "tracerworks." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts)
    for path in PACKAGE.rglob("*.py")
    if path.name != "__init__.py"
)


def test_the_list_holds_every_module() -> None:
    assert "tracerworks.recorder" in MODULES and "tracerworks.runtime.tmux" in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_a_module_imports_alone_in_a_fresh_process(module: str) -> None:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
