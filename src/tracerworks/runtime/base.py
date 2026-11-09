"""The runtime interface (architecture §6.5): what the rest of `tracerworks` may ask.

It exists so that another runtime (Claude Squad, §16) could plug in without
touching the commands — not to anticipate one. V1 = `TmuxWorktreeRuntime`.
"""

from __future__ import annotations

from typing import Protocol

from tracerworks.level import Level
from tracerworks.messaging import Delivery
from tracerworks.observe import Worker
from tracerworks.providers import Provider


class Runtime(Protocol):
    def launch_blocker(self, provider: Provider) -> str | None: ...

    def spawn(
        self, spec: int, provider: Provider, primer: str, level: Level
    ) -> None: ...

    def status(self, spec: int) -> Worker | None: ...

    def send(
        self, spec: int, text: str, stamp: str, wait_idle: bool = False
    ) -> Delivery: ...

    def stop(self, spec: int) -> None: ...
