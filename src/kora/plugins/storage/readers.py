import asyncio
import os
from collections.abc import AsyncIterator, Callable
from typing import Protocol, Self

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.files import FileLease, RootAnchor
from kora.plugins.storage.payload import FileRunner, FileSegment, PayloadFiles
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import MAX_CHUNK_BYTES


def close_leases(leases: tuple[FileLease, ...]) -> None:
    for lease in reversed(leases):
        lease.close()


class ReadJobs(Protocol):
    def __call__(
        self, function: Callable[[], bytes], ctx: ProtocolContext
    ) -> asyncio.Task[bytes]: ...


class LocalReadStream:
    def __init__(
        self,
        files: PayloadFiles,
        segments: tuple[FileSegment, ...],
        leases: tuple[FileLease, ...],
        runner: FileRunner,
        on_close: Callable[["LocalReadStream"], None],
        offset: int,
        length: int,
        ctx: ProtocolContext,
        read_jobs: ReadJobs,
    ) -> None:
        self.files = files
        self.segments = segments
        self.leases = leases
        self.runner = runner
        self.on_close = on_close
        self.offset = offset
        self.remaining = length
        self.ctx = ctx
        self.read_jobs = read_jobs
        self._file: FileLease | None = None
        self._segment_index = 0
        self._segment_offset = 0
        self._read_task: asyncio.Task[bytes] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        while self._segment_index < len(segments):
            size = segments[self._segment_index].size
            if self.offset < size:
                self._segment_offset = self.offset
                break
            self.offset -= size
            self._segment_index += 1

    def read_native(self, maximum: int) -> bytes:
        if self.remaining == 0:
            return b""
        while self._segment_index < len(self.segments):
            segment = self.segments[self._segment_index]
            if self._file is None:
                self._file = self.files.open_segment(segment)
                os.lseek(self._file.fd, self._segment_offset, os.SEEK_SET)
            bound = min(maximum, self.remaining, segment.size - self._segment_offset)
            if bound == 0:
                self._file.close()
                self._file = None
                self._segment_index += 1
                self._segment_offset = 0
                continue
            chunk = os.read(self._file.fd, bound)
            if not chunk:
                refuse("storage_integrity")
            self.remaining -= len(chunk)
            self._segment_offset += len(chunk)
            return chunk
        refuse("storage_integrity")

    async def read(self, maximum: int) -> bytes:
        if type(maximum) is not int or not 1 <= maximum <= MAX_CHUNK_BYTES:
            refuse("storage_range")
        if self._closed or self._close_task is not None:
            return b""
        if self._read_task is not None:
            refuse("storage_busy")

        task = self.read_jobs(lambda: self.read_native(maximum), self.ctx)

        def completed(reading: asyncio.Task[bytes]) -> None:
            if self._read_task is reading:
                self._read_task = None

        self._read_task = task
        task.add_done_callback(completed)
        try:
            chunk = await asyncio.shield(task)
        except asyncio.CancelledError:
            self.start_close()
            raise
        if not chunk:
            await self.aclose()
        return chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read(MAX_CHUNK_BYTES)
        if not chunk:
            raise StopAsyncIteration
        return chunk

    def start_close(self) -> asyncio.Task[None]:
        if self._close_task is None:

            async def close() -> None:
                reading = self._read_task

                def close_native() -> None:
                    if self._file is not None:
                        self._file.close()
                        self._file = None
                    close_leases(self.leases)

                try:
                    if reading is not None:
                        async with self.ctx.activity.span(
                            "storage.reader.close_pending"
                        ):
                            try:
                                await reading
                            except asyncio.CancelledError:
                                if not reading.cancelled():
                                    raise
                finally:
                    async with self.ctx.activity.span("storage.reader.close"):
                        await self.runner(close_native)
                    self._closed = True
                    self.on_close(self)

            self._close_task = asyncio.create_task(close())
        return self._close_task

    async def aclose(self) -> None:
        await asyncio.shield(self.start_close())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        await self.aclose()


class ReaderManager:
    def __init__(
        self, root: RootAnchor, runner: FileRunner, read_jobs: ReadJobs
    ) -> None:
        self.root = root
        self.runner = runner
        self.read_jobs = read_jobs
        self.files = PayloadFiles(root)
        self.streams: set[LocalReadStream] = set()
        self.handles = 0

    async def open(
        self,
        segments: tuple[FileSegment, ...],
        owner_lock: str,
        validate: Callable[[], None],
        offset: int,
        length: int,
        ctx: ProtocolContext,
    ) -> LocalReadStream:
        if self.handles >= 8:
            refuse("storage_busy")
        self.handles += 1
        transferred = False

        def acquire() -> tuple[FileLease, ...]:
            reader = next(
                (
                    lease
                    for index in range(self.root.manifest.limits.reader_slots)
                    if (lease := self.root.try_lock(f"reader-{index}")) is not None
                ),
                None,
            )
            if reader is None:
                refuse("storage_busy")
            try:
                owner = self.root.try_lock(owner_lock, shared=True)
                if owner is None:
                    refuse("storage_busy")
            except BaseException:
                reader.close()
                raise
            return reader, owner

        leases: tuple[FileLease, ...] = ()
        try:
            leases = await self.runner(acquire, on_cancel=close_leases)
            await self.runner(validate)
            stream = LocalReadStream(
                self.files,
                segments,
                leases,
                self.runner,
                self.closed,
                offset,
                length,
                ctx,
                self.read_jobs,
            )
            self.streams.add(stream)
            transferred = True
            return stream
        finally:
            if not transferred:
                close_leases(leases)
                self.handles -= 1

    def closed(self, stream: LocalReadStream) -> None:
        self.streams.remove(stream)
        self.handles -= 1

    async def aclose(self) -> None:
        results = await asyncio.gather(
            *(stream.aclose() for stream in tuple(self.streams)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("storage reader closure failed", errors)
