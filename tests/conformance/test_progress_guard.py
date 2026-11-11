import asyncio
from pathlib import Path

import pytest

from kora.conformance.lifetime import ParentLifetime
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    dispose_test_owner,
    installed_launcher,
    limits,
)
from tests.conformance.progress_guard import Progress, ProgressLauncher
from tests.support.event_loop import turn


class SteppedLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self._offset: float = 0.0

    def time(self) -> float:
        return super().time() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds


def test_progress_scope_expires_only_after_an_interval_without_progress() -> None:
    loop = SteppedLoop()
    progress = Progress()
    survived: list[float] = []

    async def exercise() -> None:
        async with progress.within(5) as scope:
            for _ in range(3):
                loop.advance(4)
                progress.advanced()
                await turn()
                survived.append(scope.deadline - loop.time())
            loop.advance(6)
            await turn()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        with pytest.raises(TimeoutError):
            runner.run(exercise())

    assert len(survived) == 3
    assert all(0 < remaining <= 5 for remaining in survived)
    assert progress.advances == 3
    assert progress.scopes == []


def test_owned_helper_start_reschedules_the_progress_scope(tmp_path: Path) -> None:
    loop = SteppedLoop()
    progress = Progress()

    async def exercise() -> tuple[float, float, bool]:
        clock = ControlledLifetimeClock()
        async with progress.within(5) as scope:
            initial = scope.deadline
            loop.advance(4)
            owner = ParentLifetime(
                clock=clock,
                limits=limits(),
                launcher=ProgressLauncher(installed_launcher(tmp_path), progress),
            )
            try:
                await owner.ready()
                rescheduled = scope.deadline
                expired = scope.timeout.expired()
            finally:
                await dispose_test_owner(owner, clock)
        return initial, rescheduled, expired

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        initial, rescheduled, expired = runner.run(exercise())

    assert progress.advances >= 1
    assert rescheduled >= initial + 4
    assert not expired
    assert progress.scopes == []
