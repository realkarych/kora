from datetime import UTC, datetime

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from kora.client import ClientError, KoraClient
from kora.client.errors import Failure
from kora.protocols.history import GrantHistorySnapshot
from kora.protocols.worker_heartbeats import (
    HeartbeatResponse,
    PublicationExpectation,
    WorkerHeartbeat,
)
from tests.client.support import running_app


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("invalid", [None, "mode", "grant_history", "publication"])
async def test_heartbeat_client_accepts_additions_and_validates_known_fields(
    network: bool, invalid: str | None
) -> None:
    body = WorkerHeartbeat(dispatcher_id="grant", lease_epoch=1)
    snapshot = GrantHistorySnapshot(
        version=1,
        namespace_id="ns",
        attempt_id="attempt",
        execution_domain_id="domain",
        upper_epoch=1,
        count=1,
        digest="a" * 64,
    )
    publication = PublicationExpectation(
        binding="local",
        binding_revision=1,
        credential_mode="local_credential_free",
        operation_id="publish",
        prepared_digest="sha256:" + "a" * 64,
        destination_digest="sha256:" + "b" * 64,
    )
    expected = HeartbeatResponse(
        desired_state="run",
        lease_until=datetime(2026, 11, 14, tzinfo=UTC),
        mode="execute",
        permitted_actions=("seal_request",),
        grant_history=snapshot,
        grant_history_verified=snapshot,
        publication=publication,
    )
    document: dict[str, object] = expected.model_dump(mode="json")
    document["future"] = True
    for field, record in (
        ("grant_history", snapshot),
        ("grant_history_verified", snapshot),
        ("publication", publication),
    ):
        document[field] = record.model_dump(mode="json") | {"future": True}
    if invalid is not None:
        document[invalid] = "invalid"

    async def endpoint(request: Request) -> JSONResponse:
        assert request.headers["authorization"] == "Bearer service"
        assert await request.json() == body.model_dump(mode="json")
        return JSONResponse(document)

    app = Starlette(
        routes=[Route("/api/v1/attempts/attempt/heartbeat", endpoint, methods=["POST"])]
    )

    async with (
        running_app(app) as origin,
        KoraClient(
            origin if network else "http://127.0.0.1",
            "service",
            asgi_app=None if network else app,
        ) as client,
    ):
        if invalid is None:
            result = await client.heartbeat_attempt("attempt", body)

            assert result == expected
        else:
            with pytest.raises(ClientError) as caught:
                await client.heartbeat_attempt("attempt", body)

            assert caught.value.reason is Failure.SCHEMA
