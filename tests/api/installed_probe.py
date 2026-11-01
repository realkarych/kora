import asyncio
import importlib.metadata
import os
import sys
from collections.abc import Iterator
from itertools import count
from pathlib import Path

from starlette.types import Message

import kora
from kora.api.application import create_application
from kora.api.exchange import COMPONENT
from kora.db.connection import Database, parse_database_url
from kora.observability import LoggingSettings, configure_process
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.time import SystemClock


async def exercise(root: Path, environment: Path) -> None:
    assert kora.__file__ is not None
    assert Path(kora.__file__).is_relative_to(environment)
    names = {item.metadata["Name"] for item in importlib.metadata.distributions()}
    assert "httpx" not in names
    assert "opentelemetry-sdk" not in names
    clock = SystemClock()
    descriptor = os.open(os.devnull, os.O_WRONLY)
    diagnostics = configure_process(
        COMPONENT, LoggingSettings(), "installed-api", clock.now, descriptor
    )
    database = Database(
        parse_database_url("sqlite+aiosqlite:///" + str(root / "unused.sqlite"))
    )
    identities: Iterator[int] = count()

    def request_id() -> str:
        return f"req_installed_{next(identities)}"

    app = create_application(
        StaticTokenAuthenticator({}, database), diagnostics, clock, request_id
    )
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    outgoing: asyncio.Queue[Message] = asyncio.Queue()
    messages: list[Message] = []

    async def lifespan_send(message: Message) -> None:
        await outgoing.put(message)

    async def run_lifespan() -> None:
        await app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            lifespan_send,
        )

    async def receive() -> Message:
        raise AssertionError("Health must not consume a request body.")

    async def send(message: Message) -> None:
        messages.append(message)

    task = asyncio.create_task(run_lifespan())
    try:
        await incoming.put({"type": "lifespan.startup"})
        assert (await asyncio.wait_for(outgoing.get(), 10))[
            "type"
        ] == "lifespan.startup.complete"
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/healthz",
                "raw_path": b"/healthz",
                "root_path": "",
                "query_string": b"",
                "headers": [],
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 200
        assert dict(messages[0]["headers"])[b"x-request-id"] == b"req_installed_0"
        assert messages[1]["body"] == b'{"status":"ok"}'
        await incoming.put({"type": "lifespan.shutdown"})
        assert (await asyncio.wait_for(outgoing.get(), 10))[
            "type"
        ] == "lifespan.shutdown.complete"
        await asyncio.wait_for(task, 10)
        assert diagnostics.writer_is_alive()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()
        assert diagnostics.shutdown(1)
        assert not diagnostics.writer_is_alive()
        os.close(descriptor)


if __name__ == "__main__":
    asyncio.run(exercise(Path.cwd(), Path(sys.argv[1])))
    sys.stdout.write("installed-api-ok\n")
