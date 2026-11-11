import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from kora.conformance.io_helper import IOHelper, IOHelperLauncher
from kora.protocols.time import Clock


@dataclass(frozen=True)
class ProgressScope:
    timeout: asyncio.Timeout
    seconds: float

    @property
    def deadline(self) -> float:
        when = self.timeout.when()
        assert when is not None
        return when


@dataclass
class Progress:
    scopes: list[ProgressScope] = field(default_factory=list[ProgressScope])
    advances: int = 0

    def advanced(self) -> None:
        self.advances += 1
        now = asyncio.get_running_loop().time()
        for scope in self.scopes:
            if not scope.timeout.expired():
                scope.timeout.reschedule(now + scope.seconds)

    @asynccontextmanager
    async def within(self, seconds: float) -> AsyncGenerator[ProgressScope]:
        async with asyncio.timeout(seconds) as timeout:
            scope = ProgressScope(timeout, seconds)
            self.scopes.append(scope)
            try:
                yield scope
            finally:
                self.scopes.remove(scope)


@dataclass(frozen=True)
class ProgressLauncher:
    launcher: IOHelperLauncher
    progress: Progress

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        helper = self.launcher.start(descriptors, clock)
        self.progress.advanced()
        return helper
