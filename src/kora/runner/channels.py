import asyncio
import os
import socket
import struct
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from enum import StrEnum
from typing import TypeVar

from kora.protocols.time import Clock

MAX_FRAME_BYTES: int = 65536
MAX_RECORD_BYTES: int = 1048576
MAX_TYPED_FRAME_BYTES: int = 1048576
TYPED_HEADER: struct.Struct = struct.Struct("!IB")
T = TypeVar("T")
SendGuard = Callable[[], AbstractContextManager[bool]]


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


def _byte_view(value: memoryview) -> memoryview:
    try:
        return value.cast("B")
    except (TypeError, ValueError):
        raise ChannelError(ChannelFailure.MALFORMED) from None


async def cancel_task(task: asyncio.Task[T]) -> None:
    task.cancel()
    try:
        await asyncio.gather(task, return_exceptions=True)
    finally:
        if task.done() and not task.cancelled():
            task.result()


async def wait_future(ready: asyncio.Future[T], deadline_ns: int, clock: Clock) -> None:
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
        await cancel_task(timeout)


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
            self._wake()
            if self._writing:
                self._loop.remove_writer(self._descriptor)
            else:
                self._loop.remove_reader(self._descriptor)
            self._closed = True


class BoundedChannel:
    def __init__(
        self, endpoint: socket.socket, clock: Clock, *, cutoff_ns: int | None = None
    ) -> None:
        endpoint.setblocking(False)
        self._endpoint = endpoint
        self._clock = clock
        self._cutoff_ns = cutoff_ns
        self._reading = False
        self._writing = False
        self._revoked = False
        self._closed = False
        self._close_failed = False
        self._reader: DescriptorReadiness | None = None
        self._writer: DescriptorReadiness | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_failed(self) -> bool:
        return self._close_failed

    def close(self) -> None:
        self._revoked = True
        if self._closed:
            return
        first: OSError | None = None
        for watcher in (self._reader, self._writer):
            if watcher is not None:
                try:
                    watcher.close()
                except OSError as error:
                    if first is None:
                        first = error
        if first is not None:
            self._close_failed = True
            raise first
        try:
            self._endpoint.close()
        except OSError:
            self._close_failed = True
            raise
        self._closed = True

    def _close_after_failure(self) -> None:
        try:
            self.close()
        except OSError:
            return

    def _bounded_deadline(self, deadline_ns: int) -> int:
        return (
            deadline_ns
            if self._cutoff_ns is None
            else min(self._cutoff_ns, deadline_ns)
        )

    async def _wait(self, deadline_ns: int | None, *, writing: bool) -> None:
        waiter = DescriptorReadiness(self._endpoint.fileno(), writing=writing)
        if writing:
            self._writer = waiter
        else:
            self._reader = waiter
        failed = False
        try:
            if deadline_ns is None:
                await waiter.ready
            else:
                await wait_future(waiter.ready, deadline_ns, self._clock)
        except (ChannelError, asyncio.CancelledError, OSError):
            failed = True
            raise
        finally:
            try:
                waiter.close()
            except OSError:
                self._close_failed = True
                if not failed:
                    raise
            else:
                if writing:
                    self._writer = None
                else:
                    self._reader = None

    async def wait_readable(self) -> None:
        if self._reading:
            raise ChannelError(ChannelFailure.BUSY)
        self._reading = True
        try:
            if self._revoked:
                raise ChannelError(ChannelFailure.CLOSED)
            await self._wait(self._cutoff_ns, writing=False)
            if self._revoked:
                raise ChannelError(ChannelFailure.CLOSED)
            if (
                self._cutoff_ns is not None
                and self._clock.monotonic_ns() >= self._cutoff_ns
            ):
                raise ChannelError(ChannelFailure.DEADLINE)
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
            raise
        finally:
            self._reading = False

    async def _read_exact(
        self, size: int, deadline_ns: int, received: Callable[[], None] | None = None
    ) -> bytes:
        deadline_ns = self._bounded_deadline(deadline_ns)
        result = bytearray()
        while len(result) < size:
            if self._revoked:
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
            if received is not None:
                received()
                received = None
            result.extend(chunk)
        return bytes(result)

    async def _read_into(self, target: memoryview, deadline_ns: int) -> None:
        deadline_ns = self._bounded_deadline(deadline_ns)
        offset = 0
        while offset < len(target):
            if self._revoked:
                raise ChannelError(ChannelFailure.CLOSED)
            if self._clock.monotonic_ns() >= deadline_ns:
                raise ChannelError(ChannelFailure.DEADLINE)
            try:
                received = self._endpoint.recv_into(target[offset:])
            except BlockingIOError:
                await self._wait(deadline_ns, writing=False)
                continue
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ChannelError(ChannelFailure.CLOSED) from error
            if received == 0:
                raise ChannelError(ChannelFailure.CLOSED)
            offset += received

    async def _read_typed_header(
        self,
        deadline_ns: int,
        reservations: Mapping[int, int],
        received: Callable[[], None] | None = None,
    ) -> tuple[int, int]:
        header = await self._read_exact(TYPED_HEADER.size, deadline_ns, received)
        length = int.from_bytes(header[:4], "big")
        if length > MAX_TYPED_FRAME_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        kind = header[4]
        reservation = reservations.get(kind)
        if reservation is None:
            raise ChannelError(ChannelFailure.MALFORMED)
        if not length <= reservation <= MAX_TYPED_FRAME_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        return kind, length

    async def read_typed_frame(
        self,
        deadline_ns: int,
        reservations: Mapping[int, int],
        *,
        received: Callable[[], None] | None = None,
    ) -> tuple[int, bytes]:
        if not all(0 <= cap <= MAX_TYPED_FRAME_BYTES for cap in reservations.values()):
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._reading:
            raise ChannelError(ChannelFailure.BUSY)
        self._reading = True
        completed = False
        try:
            kind, length = await self._read_typed_header(
                deadline_ns, reservations, received
            )
            payload = await self._read_exact(length, deadline_ns)
            completed = True
            return kind, payload
        finally:
            if not completed:
                self._close_after_failure()
            self._reading = False

    async def read_typed_frame_into(
        self, deadline_ns: int, kind: int, target: memoryview
    ) -> int:
        target = _byte_view(target)
        if target.readonly:
            raise ChannelError(ChannelFailure.MALFORMED)
        if not 0 < len(target) <= MAX_TYPED_FRAME_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._reading:
            raise ChannelError(ChannelFailure.BUSY)
        self._reading = True
        try:
            _, length = await self._read_typed_header(deadline_ns, {kind: len(target)})
            if length == 0:
                raise ChannelError(ChannelFailure.MALFORMED)
            await self._read_into(target[:length], deadline_ns)
            return length
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
            raise
        finally:
            self._reading = False

    async def expect_eof(self, deadline_ns: int) -> None:
        deadline_ns = self._bounded_deadline(deadline_ns)
        if self._reading:
            raise ChannelError(ChannelFailure.BUSY)
        self._reading = True
        try:
            while True:
                if self._revoked:
                    raise ChannelError(ChannelFailure.CLOSED)
                if self._clock.monotonic_ns() >= deadline_ns:
                    raise ChannelError(ChannelFailure.DEADLINE)
                try:
                    chunk = self._endpoint.recv(1)
                except BlockingIOError:
                    await self._wait(deadline_ns, writing=False)
                    continue
                if chunk:
                    raise ChannelError(ChannelFailure.MALFORMED)
                break
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
            raise
        else:
            self.close()
        finally:
            self._reading = False

    def _send_socket(self, value: memoryview) -> int | None:
        try:
            return self._endpoint.send(value)
        except BlockingIOError:
            return None
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ChannelError(ChannelFailure.CLOSED) from error

    def _send(
        self,
        value: memoryview,
        deadline_ns: int,
        guard: SendGuard | None,
        sent: Callable[[], None] | None,
    ) -> int | None:
        if guard is None and sent is None:
            return self._send_socket(value)
        failure: BaseException | None = None
        with nullcontext(True) if guard is None else guard() as allowed:
            try:
                if not allowed or self._revoked:
                    raise ChannelError(ChannelFailure.CLOSED)
                if self._clock.monotonic_ns() >= deadline_ns:
                    raise ChannelError(ChannelFailure.DEADLINE)
                count = self._send_socket(value)
                if sent is not None and count == len(value):
                    sent()
                return count
            finally:
                failure = sys.exception()
        if failure is not None:
            raise failure
        raise ChannelError(ChannelFailure.CLOSED)

    async def _write(
        self,
        value: bytes | memoryview,
        deadline_ns: int,
        guard: SendGuard | None = None,
        sent: Callable[[], None] | None = None,
    ) -> None:
        deadline_ns = self._bounded_deadline(deadline_ns)
        remaining = memoryview(value)
        while remaining:
            if self._revoked:
                raise ChannelError(ChannelFailure.CLOSED)
            if self._clock.monotonic_ns() >= deadline_ns:
                raise ChannelError(ChannelFailure.DEADLINE)
            written = self._send(remaining, deadline_ns, guard, sent)
            if written is None:
                await self._wait(deadline_ns, writing=True)
                continue
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
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
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
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
            raise
        finally:
            self._writing = False

    async def write_typed_frame(
        self,
        kind: int,
        value: bytes | memoryview,
        deadline_ns: int,
        *,
        guard: SendGuard | None = None,
        sent: Callable[[], None] | None = None,
    ) -> None:
        payload = _byte_view(value) if isinstance(value, memoryview) else value
        if len(payload) > MAX_TYPED_FRAME_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        if self._writing:
            raise ChannelError(ChannelFailure.BUSY)
        header = TYPED_HEADER.pack(len(payload), kind)
        self._writing = True
        completed = False
        try:
            await self._write(header, deadline_ns, guard, None if payload else sent)
            await self._write(payload, deadline_ns, guard, sent)
            completed = True
        finally:
            if not completed:
                self._close_after_failure()
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
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
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
        except (ChannelError, asyncio.CancelledError, OSError):
            self._close_after_failure()
            raise
        finally:
            self._writing = False


class CaptureDrain:
    def __init__(
        self, descriptor: int, sink: Callable[[bytes], None] | None = None
    ) -> None:
        os.set_blocking(descriptor, False)
        self._descriptor = descriptor
        self._sink = sink
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
    def readiness(self) -> asyncio.Future[None]:
        return self._eof

    @property
    def closed(self) -> bool:
        return self._closed

    def _consume(self) -> None:
        try:
            chunk = os.read(self._descriptor, MAX_FRAME_BYTES)
        except BlockingIOError:
            return
        if self._sink is not None:
            self._sink(chunk)
        if not chunk:
            self._loop.remove_reader(self._descriptor)
            self._saw_eof = True
            self._eof.set_result(None)
        elif self._sink is None:
            self.bytes_discarded = min(2**63 - 1, self.bytes_discarded + len(chunk))

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
