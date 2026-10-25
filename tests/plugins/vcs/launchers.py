import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from kora.executor.launcher import (
    Ending,
    LaunchSpec,
    ProcessLauncher,
    StepLauncher,
    StepOutcome,
)


def is_push(spec: LaunchSpec) -> bool:
    return "push" in spec.argv


@dataclass
class RecordingLauncher(StepLauncher):
    inner: StepLauncher = field(default_factory=ProcessLauncher)
    specs: list[LaunchSpec] = field(default_factory=lambda: [])
    drop_push_response: bool = False
    after_push: Callable[[], None] | None = None

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        self.specs.append(spec)
        outcome = await self.inner.run(spec)
        if is_push(spec):
            if self.after_push is not None:
                self.after_push()
            if self.drop_push_response:
                return replace(outcome, ending=Ending.UNKNOWN, exit_code=None)
        return outcome

    def pushes(self) -> int:
        return sum(1 for spec in self.specs if is_push(spec))


STALL: tuple[str, ...] = ("-I", "-c", "import time\ntime.sleep(60)")


@dataclass
class StallingLauncher(StepLauncher):
    marker: str
    inner: StepLauncher = field(default_factory=ProcessLauncher)
    stalled: int = 0

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        if self.marker not in spec.argv:
            return await self.inner.run(spec)
        self.stalled += 1
        return await self.inner.run(replace(spec, argv=(sys.executable, *STALL)))
