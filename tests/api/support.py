import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from starlette.types import ASGIApp, Message, Scope

from kora.api.application import create_application
from kora.api.exchange import COMPONENT, ApiExchange
from kora.conformance.capture import Capture, captured
from kora.db.connection import Database
from kora.observability import Level, LoggingSettings
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.plugins.authentication.support import AuthorityClock

POISON: str = "private-api-content-marker"


class RequestIds:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"req_api_{self.count}"


@pytest.fixture
def api_capture() -> Generator[Capture, None, None]:
    clock = AuthorityClock()
    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "api-test", clock.now
    ) as capture:
        yield capture


def application(database: Database, capture: Capture) -> ApiExchange:
    return create_application(
        StaticTokenAuthenticator({}, database),
        capture.diagnostics,
        AuthorityClock(),
        RequestIds(),
    )


@asynccontextmanager
async def client_for(app: ASGIApp) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test", trust_env=False
    ) as client:
        yield client


@dataclass
class HttpCall:
    path: str = "/healthz"
    method: str = "GET"
    headers: list[tuple[bytes, bytes]] = field(
        default_factory=list[tuple[bytes, bytes]]
    )
    root_path: str = ""
    query: bytes = b""
    messages: list[Message] = field(default_factory=list[Message])
    receives: int = 0

    def scope(self) -> Scope:
        return {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": self.method,
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode("utf-8"),
            "root_path": self.root_path,
            "query_string": self.query,
            "headers": self.headers,
            "server": ("api.test", 80),
            "client": ("127.0.0.1", 12345),
        }

    async def receive(self) -> Message:
        self.receives += 1
        return {"type": "http.request", "body": POISON.encode(), "more_body": False}

    async def send(self, message: Message) -> None:
        self.messages.append(dict(message))

    async def run(self, app: ASGIApp) -> None:
        await app(self.scope(), self.receive, self.send)

    @property
    def status(self) -> int:
        value: object = self.messages[0]["status"]
        assert type(value) is int
        return value

    @property
    def body(self) -> bytes:
        bodies: list[bytes] = []
        for message in self.messages:
            if message["type"] == "http.response.body":
                value: object = message["body"]
                assert isinstance(value, bytes)
                bodies.append(value)
        return b"".join(bodies)


@asynccontextmanager
async def lifespan(app: ASGIApp) -> AsyncGenerator[None, None]:
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()

    async def send(message: Message) -> None:
        await outgoing.put(message)

    async def run() -> None:
        await app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            send,
        )

    task = asyncio.create_task(run())
    try:
        await incoming.put({"type": "lifespan.startup"})
        assert (await asyncio.wait_for(outgoing.get(), 10))[
            "type"
        ] == "lifespan.startup.complete"
        yield
        await incoming.put({"type": "lifespan.shutdown"})
        assert (await asyncio.wait_for(outgoing.get(), 10))[
            "type"
        ] == "lifespan.shutdown.complete"
        await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
