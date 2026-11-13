from pathlib import Path

import pytest
from fastapi import FastAPI

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.db.connection import Database
from kora.ir.json import parse_json_object
from kora.models.worker_history import WorkerHistoryQuery
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api, history_query
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import (
    ClaimCase,
    state,
)
from tests.plugins.authentication.support import AuthorityClock
from tests.support.parity import schema_operations, uncovered_operations

__all__ = ["evaluator", "history_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
async def test_claim_history_page_and_replay_are_read_only(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
) -> None:
    case, _, app = history_api
    ledger = ConnectionLedger(database)

    async with running_app(app) as url:
        async with KoraClient(
            "http://127.0.0.1" if transport == "asgi" else url,
            BEARER,
            asgi_app=app if transport == "asgi" else None,
        ) as client:
            response = await client.claim_worker(case.query.worker_id, request(case))
            assert response.claim is not None
            granted = response.claim
            query = history_query(granted)
            before = await state(database)
            first = await client.grant_history(case.binding.attempt_id, query)
            replay = await client.grant_history(case.binding.attempt_id, query)
            after = await state(database)

    assert first == replay
    assert first.complete
    assert first.snapshot == granted.grant_history
    assert first.grants == (granted.current_grant,)
    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_history_openapi_has_exact_query_and_worker_only_parity(
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
) -> None:
    _, _, app = history_api
    assert isinstance(app.application, FastAPI)

    schema = parse_json_object(app.application.openapi())
    paths = schema["paths"]
    assert isinstance(paths, dict)
    operation_path = paths["/api/v1/attempts/{att}/grants"]
    assert isinstance(operation_path, dict)
    operation = operation_path["get"]
    assert isinstance(operation, dict)
    parameters = operation["parameters"]
    assert isinstance(parameters, list)
    queries = {
        parameter["name"]: parameter
        for parameter in parameters
        if isinstance(parameter, dict)
        and isinstance(parameter["name"], str)
        and parameter["in"] == "query"
    }
    source = Path(__file__).resolve().parents[2] / "src"

    assert set(queries) == set(WorkerHistoryQuery.model_fields)
    assert {
        name for name, parameter in queries.items() if parameter["required"]
    } == set(WorkerHistoryQuery.model_fields) - {"after_epoch", "snapshot_upper_epoch"}
    assert ("GET", "/api/v1/attempts/{}/grants") in schema_operations(paths)
    assert uncovered_operations(paths, source) == []
    assert uncovered_operations(
        {"/api/v1/attempts/{att}/grants": {"post": {}}}, source
    ) == [
        "cli: POST /api/v1/attempts/{}/grants",
        "mcp: POST /api/v1/attempts/{}/grants",
    ]
