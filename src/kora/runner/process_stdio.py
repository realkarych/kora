import asyncio
import socket
from collections.abc import Iterable, Sized
from typing import Final, SupportsIndex

from kora.protocols.time import Clock
from kora.runner.channels import ChannelError, ChannelFailure, wait_future

StdioStreams = tuple[asyncio.StreamReader, asyncio.StreamWriter]

_MAX_RECEIVE_POSITION: Final[int] = (1 << 63) - 1


class StdioReader(asyncio.StreamReader):
    def __init__(self, limit: int) -> None:
        super().__init__(limit=limit)
        self._received: int = 0
        self._input_closed: bool = False

    @property
    def received(self) -> int:
        return self._received

    @property
    def input_closed(self) -> bool:
        return self._input_closed

    def feed_data(self, data: Iterable[SupportsIndex]) -> None:
        self._received = (
            min(self._received + len(data), _MAX_RECEIVE_POSITION)
            if isinstance(data, Sized)
            else _MAX_RECEIVE_POSITION
        )
        super().feed_data(data)

    def feed_eof(self) -> None:
        self._input_closed = True
        super().feed_eof()

    def set_exception(self, exc: Exception) -> None:
        self._input_closed = True
        super().set_exception(exc)


class GatedStdio:
    def __init__(self, endpoint: socket.socket, limit: int) -> None:
        self._endpoint = endpoint
        self._limit = limit
        self._attachment: asyncio.Task[None] | None = None
        self._settlement: asyncio.Task[None] | None = None
        self._streams: StdioStreams | None = None
        self._closing = False
        self._borrowed = False

    @property
    def closed(self) -> bool:
        return (
            self._endpoint.fileno() == -1
            and (self._attachment is None or self._attachment.done())
            and (self._settlement is None or self._settlement.done())
        )

    async def _attach(self) -> None:
        if self._closing:
            self._endpoint.close()
            return
        reader = StdioReader(self._limit)
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_connection(
                lambda: protocol, sock=self._endpoint
            )
        except OSError:
            self._endpoint.close()
            return
        self._streams = reader, asyncio.StreamWriter(transport, protocol, reader, loop)
        if self._closing:
            transport.abort()

    async def prepare(self, deadline_ns: int, clock: Clock) -> None:
        if self._closing:
            raise ChannelError(ChannelFailure.CLOSED)
        if clock.monotonic_ns() >= deadline_ns:
            raise ChannelError(ChannelFailure.DEADLINE)
        if self._attachment is None:
            self._attachment = asyncio.create_task(self._attach())
        await wait_future(self._attachment, deadline_ns, clock)
        if clock.monotonic_ns() >= deadline_ns:
            raise ChannelError(ChannelFailure.DEADLINE)
        if self._closing or self._streams is None:
            raise ChannelError(ChannelFailure.CLOSED)

    def borrow(self) -> StdioStreams:
        if self._closing or self._streams is None:
            raise ChannelError(ChannelFailure.CLOSED)
        if self._borrowed:
            raise ChannelError(ChannelFailure.BUSY)
        self._borrowed = True
        return self._streams

    def close(self) -> None:
        self._closing = True
        if self._streams is not None:
            self._streams[1].transport.abort()
        elif self._attachment is None:
            self._endpoint.close()

    async def _wait_closed(self, writer: asyncio.StreamWriter) -> None:
        try:
            await writer.wait_closed()
        except OSError:
            if self._endpoint.fileno() != -1:
                raise ChannelError(ChannelFailure.CLOSED) from None

    async def settle(self, deadline_ns: int, clock: Clock) -> None:
        self.close()
        if self._attachment is not None:
            await wait_future(self._attachment, deadline_ns, clock)
        if self._streams is not None and self._settlement is None and not self.closed:
            self._settlement = asyncio.create_task(self._wait_closed(self._streams[1]))
        if self._settlement is not None:
            await wait_future(self._settlement, deadline_ns, clock)
        if clock.monotonic_ns() >= deadline_ns:
            raise ChannelError(ChannelFailure.DEADLINE)
        if not self.closed:
            raise ChannelError(ChannelFailure.CLOSED)
