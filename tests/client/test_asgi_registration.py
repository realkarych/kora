import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Literal

import pytest
from sqlalchemy import event, insert, select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kora.client import KoraClient
from kora.client.errors import ClientError, Failure, Outcome
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.tables import worker, worker_execution_domain
from kora.models.workers import WorkerRegistration
from kora.protocols.identity_records import ProcessIdentity
from tests.api.support import api_capture
from tests.api.worker_support import bearer, registration, worker_application
from tests.client.support import SCHEMA, ProbeClient, running_app
from tests.db.closing import ConnectionLedger, reached
from tests.plugins.authentication.support import AuthorityClock, seed_case

__all__ = ["api_capture"]


class HoldResponse:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def held_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.started.set()
                await self.release.wait()
            await send(message)

        try:
            await self.app(scope, receive, held_send)
        finally:
            self.finished.set()


class ClosingStream(StreamingResponse):
    def __init__(self, source: AsyncGenerator[bytes, None]) -> None:
        super().__init__(source, media_type="application/json")
        self.source = source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with aclosing(self.source):
            await super().__call__(scope, receive, send)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "shutdown", "deadline"])
async def test_asgi_registration_lost_response_releases_resources_and_replays(
    database: Database,
    api_capture: Capture,
    ending: Literal["cancel", "shutdown", "deadline"],
) -> None:
    case = await seed_case(database, "worker", "service")
    app = worker_application(database, api_capture, AuthorityClock())
    held = HoldResponse(app)
    ledger = ConnectionLedger(database)
    client = ProbeClient(
        "http://127.0.0.1", bearer(case), asgi_app=held, request_timeout=3
    )
    body = registration(case)
    request = asyncio.create_task(client.register_worker(body))

    try:
        async with asyncio.timeout(10):
            await held.started.wait()
            if ending == "cancel":
                request.cancel()
            elif ending == "shutdown":
                await client.aclose()
            if ending == "deadline":
                with pytest.raises(ClientError) as failure:
                    await request
                assert failure.value.reason is Failure.DEADLINE
                assert failure.value.outcome is Outcome.UNKNOWN
            else:
                with pytest.raises(asyncio.CancelledError):
                    await request
            await held.finished.wait()
    finally:
        held.release.set()
        await client.aclose()
        await asyncio.gather(request, return_exceptions=True)
    async with KoraClient(
        "http://127.0.0.1", bearer(case), asgi_app=app
    ) as replay_client:
        replay = await replay_client.register_worker(body)

    assert replay.worker_id == body.name
    assert client.request_owners() == ()
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_asgi_response_is_bounded_before_transport_accumulation() -> None:
    chunks: list[int] = []
    finished = asyncio.Event()

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            for index in range(1000):
                chunks.append(index)
                yield bytes(1024)
        finally:
            finished.set()

    async def endpoint(request: Request) -> StreamingResponse:
        return ClosingStream(body())

    app = Starlette(routes=[Route("/api/v1/probe", endpoint)])

    async with ProbeClient("http://127.0.0.1", "service", asgi_app=app) as client:
        with pytest.raises(ClientError) as failure:
            await client.probe("GET", response_limit=2048)
    async with asyncio.timeout(5):
        await finished.wait()

    assert failure.value.reason is Failure.RESPONSE_LIMIT
    assert failure.value.outcome is Outcome.UNKNOWN
    assert chunks == [0, 1, 2]
    assert client.request_owners() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_asgi_partial_json_response_is_not_success(raises: bool) -> None:
    finished = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {"type": "http.response.body", "body": SCHEMA, "more_body": True}
            )
            if raises:
                raise ValueError("private-application-error")
        finally:
            finished.set()

    async with ProbeClient("http://127.0.0.1", "service", asgi_app=app) as client:
        with pytest.raises(ClientError) as failure:
            await client.openapi()

    assert failure.value.reason is Failure.STREAM
    assert failure.value.outcome is Outcome.UNKNOWN
    assert finished.is_set()
    assert client.request_owners() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "shutdown"])
async def test_registration_cancel_releases_contended_writer(
    database: Database,
    api_capture: Capture,
    ending: str,
) -> None:
    case = await seed_case(database, "worker", "service")
    body = registration(case)
    app = worker_application(database, api_capture, AuthorityClock())
    ledger = ConnectionLedger(database)
    entered = asyncio.Event()

    def observe(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        many: bool,
    ) -> None:
        if statement == "BEGIN IMMEDIATE" or statement.startswith(
            "INSERT INTO worker_execution_domain"
        ):
            entered.set()

    async with database.transaction() as holder:
        await holder.execute(
            insert(worker_execution_domain).values(
                execution_domain_id=body.execution_domain_id,
                runtime=body.runtime,
                worker_id=body.name,
            )
        )
        event.listen(
            database.writer_engine.sync_engine, "before_cursor_execute", observe
        )
        async with ProbeClient(
            "http://127.0.0.1", bearer(case), asgi_app=app
        ) as client:
            task = asyncio.create_task(client.register_worker(body))
            try:
                async with asyncio.timeout(10):
                    if database.dialect is Dialect.SQLITE:
                        assert await reached(
                            lambda: (
                                database.units_in_flight() == 2
                                and ledger.outstanding == 1
                            )
                        )
                    else:
                        await entered.wait()
                    if ending == "cancel":
                        task.cancel()
                    else:
                        await client.aclose()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                event.remove(
                    database.writer_engine.sync_engine, "before_cursor_execute", observe
                )
                await client.aclose()
                await asyncio.gather(task, return_exceptions=True)
        assert ledger.outstanding == 1
        assert client.request_owners() == ()
    async with database.read_transaction() as connection:
        rows = (await connection.execute(select(worker.c.id))).all()
    async with KoraClient(
        "http://127.0.0.1", bearer(case), asgi_app=app
    ) as next_client:
        accepted = await next_client.register_worker(body)

    assert rows == []
    assert accepted.worker_id == body.name
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_asgi_shutdown_releases_queued_admission(
    database: Database,
    api_capture: Capture,
) -> None:
    case = await seed_case(database, "worker", "service")
    held = HoldResponse(worker_application(database, api_capture, AuthorityClock()))
    client = ProbeClient(
        "http://127.0.0.1", bearer(case), asgi_app=held, max_connections=1
    )
    first = asyncio.create_task(client.register_worker(registration(case)))

    try:
        async with asyncio.timeout(10):
            await held.started.wait()
            second = asyncio.create_task(client.register_worker(registration(case)))
            while len(client.request_owners()) != 2:
                await asyncio.sleep(0)
            await client.aclose()
            results = await asyncio.gather(first, second, return_exceptions=True)
    finally:
        held.release.set()
        await client.aclose()
        await asyncio.gather(first, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert client.request_owners() == ()
    assert held.finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True])
async def test_registration_client_accepts_additive_response_fields(
    network: bool,
) -> None:
    body = WorkerRegistration(
        name="worker",
        incarnation_id="incarnation",
        runtime="local",
        execution_domain_id="domain",
        version="0.1.0",
        capacity=1,
        process=ProcessIdentity(
            host_id=None,
            boot_id=None,
            pid_namespace=None,
            pid=123,
            start_time=1,
            leads_group=False,
        ),
    )

    async def endpoint(request: Request) -> JSONResponse:
        assert request.headers["authorization"] == "Bearer service"
        assert await request.json() == body.model_dump(mode="json")
        return JSONResponse(
            {
                "worker_id": "worker",
                "incarnation_id": "incarnation",
                "registered_at": "2026-11-13T00:00:00Z",
                "future": {"opaque": True},
            }
        )

    app = Starlette(routes=[Route("/api/v1/workers", endpoint, methods=["POST"])])

    async with running_app(app) as origin:
        async with KoraClient(
            origin if network else "http://127.0.0.1",
            "service",
            asgi_app=None if network else app,
        ) as client:
            result = await client.register_worker(body)

    assert result.worker_id == "worker"
    assert result.incarnation_id == "incarnation"
