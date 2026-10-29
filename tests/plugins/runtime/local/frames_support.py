import asyncio
import hashlib
import json
import socket
import struct
import tracemalloc
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import pytest

from kora.plugins.runtime.local.channel import FrameType
from kora.protocols.deadlines import ClockSample
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import Clock
from kora.runner.channels import BoundedChannel

ORIGIN_NS: int = 10**12
E = TypeVar("E", bound=Exception)
BUFFER_BYTES: int = 262144


@dataclass
class StepClock(Clock):
    offset_ns: int = 0
    sleepers: list[tuple[int, asyncio.Future[None]]] = field(default_factory=lambda: [])
    waiting: asyncio.Event = field(default_factory=asyncio.Event)

    def now(self) -> datetime:
        return datetime(2026, 10, 27, tzinfo=UTC) + timedelta(
            microseconds=self.offset_ns // 1000
        )

    def monotonic_ns(self) -> int:
        return ORIGIN_NS + self.offset_ns

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.now(), monotonic_ns=self.monotonic_ns())

    async def sleep_until(self, deadline_ns: int) -> None:
        if deadline_ns <= self.monotonic_ns():
            return
        woken: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.sleepers.append((deadline_ns, woken))
        self.waiting.set()
        await woken

    def advance(self, nanoseconds: int) -> None:
        self.offset_ns += nanoseconds
        due = [entry for entry in self.sleepers if entry[0] <= self.monotonic_ns()]
        self.sleepers = [entry for entry in self.sleepers if entry not in due]
        for _, woken in due:
            if not woken.done():
                woken.set_result(None)
        if not any(not woken.done() for _, woken in self.sleepers):
            self.waiting.clear()


@dataclass
class TickingClock(Clock):
    step_ns: int
    offset_ns: int = 0

    def now(self) -> datetime:
        return datetime(2026, 10, 27, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        self.offset_ns += self.step_ns
        return ORIGIN_NS + self.offset_ns

    def sample(self) -> ClockSample:
        return ClockSample(wall=self.now(), monotonic_ns=self.monotonic_ns())

    async def sleep_until(self, deadline_ns: int) -> None:
        raise AssertionError("a buffered message never waits")


@dataclass
class Pair:
    receiver: BoundedChannel
    raw: socket.socket


@asynccontextmanager
async def channel_pair(clock: Clock) -> AsyncGenerator[Pair]:
    first, second = socket.socketpair()
    for endpoint in (first, second):
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, BUFFER_BYTES)
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_BYTES)
    second.setblocking(False)
    receiver = BoundedChannel(first, clock)
    try:
        yield Pair(receiver, second)
    finally:
        receiver.close()
        second.close()


def frame(kind: int, payload: bytes) -> bytes:
    return struct.pack("!IB", len(payload), kind) + payload


def header(op: str, sections: list[tuple[str, bytes]]) -> bytes:
    declared = [
        {"name": name, "length": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in sections
    ]
    document = {"op": op, "request_id": "request-1", "sections": declared}
    return frame(FrameType.JSON, json.dumps(document).encode("utf-8"))


def bytes_frames(body: bytes, size: int) -> bytes:
    return b"".join(
        frame(FrameType.BYTES, body[offset : offset + size])
        for offset in range(0, len(body), size)
    )


async def send_raw(endpoint: socket.socket, payload: bytes) -> None:
    await asyncio.get_running_loop().sock_sendall(endpoint, payload)


async def refused_peak(
    action: Awaitable[object], kind: type[E] = KoraProtocolError
) -> tuple[E, int]:
    tracemalloc.start()
    try:
        with pytest.raises(kind) as refused:
            await action
        return refused.value, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
