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


def test_progress_moves_every_open_scope_and_leaves_an_expired_one() -> None:
    loop = SteppedLoop()
    progress = Progress()
    remaining: list[float] = []

    async def exercise() -> None:
        async with progress.within(10) as outer:
            async with progress.within(5) as inner:
                loop.advance(4)
                progress.advanced()
                remaining.append(outer.deadline - loop.time())
                remaining.append(inner.deadline - loop.time())
                loop.advance(6)
                try:
                    await turn()
                finally:
                    progress.advanced()
                    remaining.append(outer.deadline - loop.time())

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        with pytest.raises(TimeoutError):
            runner.run(exercise())

    assert 9 < remaining[0] <= 10
    assert 4 < remaining[1] <= 5
    assert 9 < remaining[2] <= 10
    assert progress.advances == 2
    assert progress.scopes == []


def test_owned_helper_start_reschedules_the_progress_scope(tmp_path: Path) -> None:
    loop = SteppedLoop()
    progress = Progress()

    async def exercise() -> tuple[float, float]:
        clock = ControlledLifetimeClock()
        async with progress.within(5) as scope:
            initial = scope.deadline
            loop.advance(1)
            owner = ParentLifetime(
                clock=clock,
                limits=limits(),
                launcher=ProgressLauncher(installed_launcher(tmp_path), progress),
            )
            try:
                await owner.ready()
                rescheduled = scope.deadline
            finally:
                await dispose_test_owner(owner, clock)
        return initial, rescheduled

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        initial, rescheduled = runner.run(exercise())

    assert progress.advances >= 1
    assert rescheduled >= initial + 1
    assert progress.scopes == []
