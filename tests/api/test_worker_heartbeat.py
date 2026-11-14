from datetime import timedelta
from pathlib import Path

import pytest

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database
from kora.protocols.evidence import ClosureEvidence
from kora.protocols.worker_heartbeats import WorkerHeartbeat
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.heartbeat_support import heartbeat_api
from tests.api.history_support import history_query
from tests.api.support import client_for
from tests.api.worker_support import bearer
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import ClaimCase, state
from tests.engine.heartbeat_support import self_closure
from tests.plugins.authentication.support import AuthorityClock, seed_case
from tests.support.parity import uncovered_operations

__all__ = ["evaluator", "heartbeat_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_claim_history_heartbeat_closure_and_replay_share_one_application(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
) -> None:
    case, clock, app = heartbeat_api
    ledger = ConnectionLedger(database)
    async with (
        running_app(app) as origin,
        KoraClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            BEARER,
            asgi_app=app if transport == "asgi" else None,
        ) as client,
    ):
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
        assert granted is not None
        page = await client.grant_history(granted.attempt_id, history_query(granted))
        body = WorkerHeartbeat(
            dispatcher_id=granted.current_grant.dispatcher_id,
            lease_epoch=granted.lease_epoch,
            grant_history_complete=page.snapshot,
            evidence=ClosureEvidence(
                runtime=None, dispatchers=(self_closure(granted),), publication=None
            ),
        )
        clock.moment += timedelta(seconds=16)

        accepted = await client.heartbeat_attempt(granted.attempt_id, body)
        before = await state(database)
        repeated = await client.heartbeat_attempt(granted.attempt_id, body)
        after = await state(database)
        retained = await client.grant_history(
            granted.attempt_id, history_query(granted)
        )

    assert accepted == repeated
    assert accepted.desired_state == "run"
    assert accepted.lease_until == clock.now() + timedelta(seconds=120)
    assert accepted.permitted_actions == ("observe", "retire", "report_result")
    assert accepted.grant_history_verified == page.snapshot
    assert accepted.grant_history is None
    assert before == after
    assert retained == page
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_heartbeat_openapi_and_exact_service_only_parity(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
) -> None:
    case, _, app = heartbeat_api
    personal = await seed_case(database, "schema")
    path = f"/api/v1/attempts/{case.binding.attempt_id}/heartbeat"
    async with KoraClient("http://127.0.0.1", bearer(personal), asgi_app=app) as client:
        schema = await client.openapi()
    async with client_for(app) as client:
        refused = await client.get(path, headers={"Authorization": "Bearer " + BEARER})

    paths = schema["paths"]
    assert isinstance(paths, dict)
    operation = paths["/api/v1/attempts/{att}/heartbeat"]
    assert isinstance(operation, dict)
    post = operation["post"]
    assert isinstance(post, dict)
    assert post["operationId"] == "heartbeat_attempt"
    components = schema["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)
    body = schemas["WorkerHeartbeat"]
    assert isinstance(body, dict)
    assert body["additionalProperties"] is False
    assert not uncovered_operations(paths, Path.cwd() / "src")
    assert uncovered_operations(
        {"/api/v1/attempts/{att}/heartbeat": {"get": {}}}, Path.cwd() / "src"
    )
    assert refused.status_code == 405
    assert refused.headers["Allow"] == "POST"


@pytest.mark.asyncio
async def test_heartbeat_ordinary_response_omits_publication_echo(
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
) -> None:
    case, _, app = heartbeat_api
    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
    assert granted is not None
    body = WorkerHeartbeat(
        dispatcher_id=granted.current_grant.dispatcher_id,
        lease_epoch=granted.lease_epoch,
    )
    path = f"/api/v1/attempts/{granted.attempt_id}/heartbeat"
    headers = {"Authorization": "Bearer " + BEARER}

    async with client_for(app) as client:
        first = await client.post(
            path, headers=headers, json=body.model_dump(mode="json")
        )
        acknowledged = await client.post(
            path,
            headers=headers,
            json=body.model_copy(
                update={"grant_history_complete": granted.grant_history}
            ).model_dump(mode="json"),
        )

    assert first.status_code == acknowledged.status_code == 200
    assert "publication" not in first.json()
    assert "publication" not in acknowledged.json()
    assert first.json()["grant_history_verified"] is None
    assert first.json()["grant_history"] == granted.grant_history.model_dump(
        mode="json"
    )
    assert "grant_history" not in acknowledged.json()
    assert (
        acknowledged.json()["grant_history_verified"] == first.json()["grant_history"]
    )
