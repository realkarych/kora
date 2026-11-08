import asyncio
import socket

from kora.protocols.time import Clock
from kora.runner.channels import ChannelError, ChannelFailure, wait_future

StdioStreams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


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
        try:
            self._streams = await asyncio.open_connection(
                sock=self._endpoint, limit=self._limit
            )
        except OSError:
            self._endpoint.close()
            return
        if self._closing:
            self._streams[1].transport.abort()

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
