import asyncio

import pytest
from sqlalchemy import select
from starlette.types import Message

from kora.api.application import create_application
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import namespace, price_table
from kora.engine.admission_initialization import AdmissionInitializationRefused
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.support import RequestIds, api_capture, client_for, lifespan
from tests.db.seed import START
from tests.engine.test_admission_initialization import initializer, namespace_id, rates
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["api_capture"]


@pytest.mark.asyncio
async def test_api_startup_commits_admission_configuration_before_serving(
    database: Database, api_capture: Capture
) -> None:
    identity = await namespace_id(database)
    service = initializer(
        database,
        (
            NamespacePolicyConfiguration.prepare(
                identity, 2, {"concurrency": {"max_running_attempts": 3}}
            ),
        ),
        (PriceConfiguration(None, START, {"m": rates()}),),
    )
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        AuthorityClock(),
        RequestIds(),
        admission_initialization=service,
    )

    async with lifespan(app):
        async with client_for(app) as client:
            response = await client.get("/healthz")
        async with database.read_transaction() as connection:
            revision = await connection.scalar(select(namespace.c.policy_revision))
            rows = (await connection.execute(select(price_table.c.id))).scalars().all()

    assert response.status_code == 200
    assert revision == 2
    assert set(rows) == {service.baseline.id, service.prices[0].id}
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_api_startup_refusal_is_content_free_and_rolls_back(
    database: Database, api_capture: Capture
) -> None:
    identity = await namespace_id(database)
    marker = "private-admission-policy-content"
    service = initializer(
        database,
        (NamespacePolicyConfiguration.prepare(identity, 1, {"private": marker}),),
    )
    app = create_application(
        StaticTokenAuthenticator({}, database),
        api_capture.diagnostics,
        AuthorityClock(),
        RequestIds(),
        admission_initialization=service,
    )
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    messages: list[Message] = []
    await incoming.put({"type": "lifespan.startup"})

    async def send(message: Message) -> None:
        messages.append(message)

    with pytest.raises(AdmissionInitializationRefused) as failure:
        await app(
            {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
            incoming.get,
            send,
        )

    assert [message["type"] for message in messages] == ["lifespan.startup.failed"]
    assert marker not in str(messages)
    assert failure.value.__context__ is None
    async with database.read_transaction() as connection:
        assert await connection.scalar(select(namespace.c.policy_revision)) == 1
        assert (await connection.execute(select(price_table.c.id))).all() == []
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
