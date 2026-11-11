import asyncio
import os
import subprocess
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from kora.conformance.io_helper import IOHelper, IOHelperLauncher
from kora.protocols.time import Clock
from kora.runner.channels import DescriptorReadiness
from tests.support.observability import ProcessResult

STREAM_CHUNK: Final[int] = 65536


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


def open_pipe(readers: ExitStack, writers: ExitStack) -> tuple[int, int]:
    read_end, write_end = os.pipe()
    readers.callback(os.close, read_end)
    writers.callback(os.close, write_end)
    return read_end, write_end


async def drain(descriptor: int, sink: bytearray, progress: Progress | None) -> None:
    os.set_blocking(descriptor, False)
    while True:
        readiness = DescriptorReadiness(descriptor, writing=False)
        try:
            await readiness.ready
        finally:
            readiness.close()
        try:
            chunk = os.read(descriptor, STREAM_CHUNK)
        except BlockingIOError:
            continue
        if not chunk:
            return
        sink.extend(chunk)
        if progress is not None and b"\n" in chunk:
            progress.advanced()


async def progress_output(
    process: asyncio.subprocess.Process,
    command: Sequence[str],
    streams: tuple[int, int],
    progress: Progress,
    seconds: float,
) -> ProcessResult:
    output = bytearray()
    errors = bytearray()
    try:
        async with progress.within(seconds):
            async with asyncio.TaskGroup() as group:
                group.create_task(drain(streams[0], output, None))
                group.create_task(drain(streams[1], errors, progress))
            code = await process.wait()
    except TimeoutError:
        raise subprocess.TimeoutExpired(
            list(command),
            seconds,
            output=bytes(output) or None,
            stderr=bytes(errors) or None,
        ) from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return ProcessResult(code, bytes(output), bytes(errors))


async def progress_run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    progress: Progress,
    seconds: float,
    stdin: int | None = None,
    umask: int = -1,
) -> ProcessResult:
    with ExitStack() as readers:
        with ExitStack() as writers:
            output_read, output_write = open_pipe(readers, writers)
            errors_read, errors_write = open_pipe(readers, writers)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=dict(environment),
                stdin=stdin,
                stdout=output_write,
                stderr=errors_write,
                umask=umask,
            )
        return await progress_output(
            process, command, (output_read, errors_read), progress, seconds
        )
