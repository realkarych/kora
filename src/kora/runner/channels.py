import asyncio
import os
import socket
import struct
from enum import StrEnum

from kora.protocols.time import Clock

MAX_FRAME_BYTES: int = 65536
MAX_RECORD_BYTES: int = 1048576


class ChannelFailure(StrEnum):
    CLOSED = "closed"
    DEADLINE = "deadline"
    CAPACITY = "capacity"
    MALFORMED = "malformed"
    BUSY = "busy"


class ChannelError(Exception):
    def __init__(self, reason: ChannelFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


async def wait_future(
    ready: asyncio.Future[None], deadline_ns: int, clock: Clock
) -> None:
    if ready.done():
        ready.result()
        return
    if clock.monotonic_ns() >= deadline_ns:
        raise ChannelError(ChannelFailure.DEADLINE)
    timeout = asyncio.create_task(clock.sleep_until(deadline_ns))
    try:
        await asyncio.wait((ready, timeout), return_when=asyncio.FIRST_COMPLETED)
        if not ready.done():
            timeout.result()
            raise ChannelError(ChannelFailure.DEADLINE)
        ready.result()
    finally:
        timeout.cancel()
        try:
            await timeout
        except asyncio.CancelledError:
            if not timeout.cancelled():
                raise


class DescriptorReadiness:
    def __init__(self, descriptor: int, *, writing: bool) -> None:
        self._loop = asyncio.get_running_loop()
        self.ready: asyncio.Future[None] = self._loop.create_future()
        self._descriptor = descriptor
        self._writing = writing
        self._closed = False
        if writing:
            self._loop.add_writer(descriptor, self._wake)
        else:
            self._loop.add_reader(descriptor, self._wake)

    def _wake(self) -> None:
        if not self.ready.done():
            self.ready.set_result(None)

    def close(self) -> None:
        if not self._closed:
            if self._writing:
                self._loop.remove_writer(self._descriptor)
            else:
                self._loop.remove_reader(self._descriptor)
            self._closed = True
            self._wake()


class BoundedChannel:
    def __init__(self, endpoint: socket.socket, clock: Clock) -> None:
        endpoint.setblocking(False)
        self._endpoint = endpoint
        self._clock = clock
        self._reading = False
        self._writing = False
        self._closed = False
        self._reader: DescriptorReadiness | None = None
        self._writer: DescriptorReadiness | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            if self._reader is not None:
                self._reader.close()
            if self._writer is not None:
                self._writer.close()
            self._endpoint.close()
            self._closed = True

    async def _wait(self, deadline_ns: int, *, writing: bool) -> None:
        waiter = DescriptorReadiness(self._endpoint.fileno(), writing=writing)
        if writing:
            self._writer = waiter
        else:
            self._reader = waiter
        try:
            await wait_future(waiter.ready, deadline_ns, self._clock)
        finally:
            waiter.close()
            if writing:
                self._writer = None
            else:
                self._reader = None

    async def _read_exact(self, size: int, deadline_ns: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            if self._closed:
                raise ChannelError(ChannelFailure.CLOSED)
            if self._clock.monotonic_ns() >= deadline_ns:
                raise ChannelError(ChannelFailure.DEADLINE)
            try:
                chunk = self._endpoint.recv(size - len(result))
            except BlockingIOError:
                await self._wait(deadline_ns, writing=False)
                continue
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ChannelError(ChannelFailure.CLOSED) from error
            if not chunk:
                raise ChannelError(ChannelFailure.CLOSED)
            result.extend(chunk)
        return bytes(result)

    async def _write(self, value: bytes, deadline_ns: int) -> None:
        remaining = memoryview(value)
        while remaining:
            if self._closed:
                raise ChannelError(ChannelFailure.CLOSED)
            if self._clock.monotonic_ns() >= deadline_ns:
                raise ChannelError(ChannelFailure.DEADLINE)
            try:
                written = self._endpoint.send(remaining)
            except BlockingIOError:
                await self._wait(deadline_ns, writing=True)
                continue
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ChannelError(ChannelFailure.CLOSED) from error
            if written == 0:
                raise ChannelError(ChannelFailure.CLOSED)
            remaining = remaining[written:]

    async def _read_frame(self, deadline_ns: int, reservation: int) -> bytes:
        length = int.from_bytes(await self._read_exact(4, deadline_ns), "big")
        if length > reservation:
            raise ChannelError(ChannelFailure.CAPACITY)
        return await self._read_exact(length, deadline_ns)

    async def _write_frame(self, value: bytes, deadline_ns: int) -> None:
        await self._write(struct.pack("!I", len(value)) + value, deadline_ns)

    async def read_frame(
        self, deadline_ns: int, reservation: int = MAX_FRAME_BYTES
    ) -> bytes:
        if not 0 <= reservation <= MAX_FRAME_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._reading:
            raise ChannelError(ChannelFailure.BUSY)
        self._reading = True
        try:
            return await self._read_frame(deadline_ns, reservation)
        except (ChannelError, asyncio.CancelledError):
            self.close()
            raise
        finally:
            self._reading = False

    async def write_frame(self, value: bytes, deadline_ns: int) -> None:
        if len(value) > MAX_FRAME_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._writing:
            raise ChannelError(ChannelFailure.BUSY)
        self._writing = True
        try:
            await self._write_frame(value, deadline_ns)
        except (ChannelError, asyncio.CancelledError):
            self.close()
            raise
        finally:
            self._writing = False

    async def read_record(self, deadline_ns: int, reservation: int) -> bytes:
        if not 0 <= reservation <= MAX_RECORD_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._reading:
            raise ChannelError(ChannelFailure.BUSY)
        self._reading = True
        try:
            header = await self._read_frame(deadline_ns, 4)
            if len(header) != 4:
                raise ChannelError(ChannelFailure.MALFORMED)
            length = int.from_bytes(header, "big")
            if length > reservation:
                raise ChannelError(ChannelFailure.CAPACITY)
            result = bytearray()
            while len(result) < length:
                chunk = await self._read_frame(
                    deadline_ns, min(MAX_FRAME_BYTES, length - len(result))
                )
                if not chunk or len(chunk) > length - len(result):
                    raise ChannelError(ChannelFailure.MALFORMED)
                result.extend(chunk)
            return bytes(result)
        except (ChannelError, asyncio.CancelledError):
            self.close()
            raise
        finally:
            self._reading = False

    async def write_record(self, value: bytes, deadline_ns: int) -> None:
        if len(value) > MAX_RECORD_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._writing:
            raise ChannelError(ChannelFailure.BUSY)
        self._writing = True
        try:
            await self._write_frame(struct.pack("!I", len(value)), deadline_ns)
            for offset in range(0, len(value), MAX_FRAME_BYTES):
                await self._write_frame(
                    value[offset : offset + MAX_FRAME_BYTES], deadline_ns
                )
        except (ChannelError, asyncio.CancelledError):
            self.close()
            raise
        finally:
            self._writing = False


class CaptureDrain:
    def __init__(self, descriptor: int) -> None:
        os.set_blocking(descriptor, False)
        self._descriptor = descriptor
        self._loop = asyncio.get_running_loop()
        self._eof: asyncio.Future[None] = self._loop.create_future()
        self._closed = False
        self._saw_eof = False
        self.bytes_discarded = 0
        self._loop.add_reader(descriptor, self._consume)

    @property
    def eof(self) -> bool:
        return self._saw_eof

    @property
    def closed(self) -> bool:
        return self._closed

    def _consume(self) -> None:
        try:
            chunk = os.read(self._descriptor, MAX_FRAME_BYTES)
        except BlockingIOError:
            return
        if chunk:
            self.bytes_discarded = min(2**63 - 1, self.bytes_discarded + len(chunk))
        else:
            self._loop.remove_reader(self._descriptor)
            self._saw_eof = True
            self._eof.set_result(None)

    async def drain(self, deadline_ns: int, clock: Clock) -> None:
        if self._closed and not self._saw_eof:
            raise ChannelError(ChannelFailure.CLOSED)
        await wait_future(self._eof, deadline_ns, clock)
        if not self._saw_eof:
            raise ChannelError(ChannelFailure.CLOSED)

    def close(self) -> None:
        if not self._closed:
            self._loop.remove_reader(self._descriptor)
            os.close(self._descriptor)
            if not self._eof.done():
                self._eof.set_result(None)
            self._closed = True
