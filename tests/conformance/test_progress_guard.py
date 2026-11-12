import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.lifetime import ParentLifetime
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    dispose_test_owner,
    installed_launcher,
    limits,
)
from tests.conformance.progress_guard import Progress, ProgressLauncher, progress_run
from tests.support.event_loop import turn
from tests.support.observability import ProcessResult
from tests.support.provider_tooling import INSTALLED_COMMAND_SECONDS

LINE_PER_INPUT_CHILD: Final[str] = (
    "import os\n"
    "os.write(2, b'ready\\n')\n"
    "while os.read(0, 1):\n"
    "    os.write(2, b'step\\n')\n"
    "os.write(1, b'finished')\n"
)


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


@dataclass
class SignalledProgress(Progress):
    observed: asyncio.Event = field(default_factory=asyncio.Event)

    def advanced(self) -> None:
        super().advanced()
        self.observed.set()


async def next_progress(progress: SignalledProgress) -> None:
    async with asyncio.timeout(10):
        await progress.observed.wait()
    progress.observed.clear()


class InputPipe:
    def __init__(self) -> None:
        self.read_end, self.write_end = os.pipe()
        self.reader_open = True
        self.writer_open = True

    def close_reader(self) -> None:
        if self.reader_open:
            self.reader_open = False
            os.close(self.read_end)

    def close_writer(self) -> None:
        if self.writer_open:
            self.writer_open = False
            os.close(self.write_end)

    def close(self) -> None:
        try:
            self.close_writer()
        finally:
            self.close_reader()


def line_per_input_run(
    pipe: InputPipe, progress: SignalledProgress
) -> asyncio.Task[ProcessResult]:
    return asyncio.create_task(
        progress_run(
            [sys.executable, "-I", "-c", LINE_PER_INPUT_CHILD],
            cwd=Path.cwd(),
            environment={},
            progress=progress,
            seconds=INSTALLED_COMMAND_SECONDS,
            stdin=pipe.read_end,
        )
    )


def test_process_progress_scope_outlives_spaced_output_lines() -> None:
    loop = SteppedLoop()
    progress = SignalledProgress()
    step = INSTALLED_COMMAND_SECONDS - 20

    async def exercise() -> ProcessResult:
        pipe = InputPipe()
        try:
            run = line_per_input_run(pipe, progress)
            await next_progress(progress)
            for _ in range(3):
                loop.advance(step)
                os.write(pipe.write_end, b"x")
                await next_progress(progress)
            pipe.close_writer()
            async with asyncio.timeout(10):
                return await run
        finally:
            pipe.close()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        result = runner.run(exercise())

    assert 3 * step > INSTALLED_COMMAND_SECONDS
    assert result == ProcessResult(0, b"finished", b"ready\nstep\nstep\nstep\n")
    assert progress.advances == 4
    assert progress.scopes == []


def test_process_progress_scope_kills_a_silent_child() -> None:
    loop = SteppedLoop()
    progress = SignalledProgress()

    async def exercise() -> subprocess.TimeoutExpired:
        pipe = InputPipe()
        try:
            run = line_per_input_run(pipe, progress)
            await next_progress(progress)
            pipe.close_reader()
            loop.advance(INSTALLED_COMMAND_SECONDS + 1)
            with pytest.raises(subprocess.TimeoutExpired) as raised:
                async with asyncio.timeout(10):
                    await run
            with pytest.raises(BrokenPipeError):
                os.write(pipe.write_end, b"x")
            return raised.value
        finally:
            pipe.close()

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        expired = runner.run(exercise())

    assert expired.cmd == [sys.executable, "-I", "-c", LINE_PER_INPUT_CHILD]
    assert expired.timeout == INSTALLED_COMMAND_SECONDS
    assert expired.output is None
    assert expired.stderr == b"ready\n"
    assert progress.advances == 1
    assert progress.scopes == []
