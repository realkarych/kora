import pytest

from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.ir.cost import MAX_MONEY, MAX_SUM, MAX_TOKEN
from tests.api.support import api_capture, client_for
from tests.api.usage_support import report, usage_application, usage_path
from tests.api.worker_support import bearer
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import usage_case, usage_snapshot
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["money", "floor", "rows", "duplicates", "epoch", "source", "bytes"]
)
async def test_usage_api_refuses_malformed_body_atomically(
    database: Database, api_capture: Capture, invalid: str
) -> None:
    case = await usage_case(database)
    value = report(case).model_dump(mode="json")
    row = usage().model_dump(mode="json")
    if invalid in {"money", "floor"}:
        row["usd_estimate" if invalid == "money" else "same_basis_usd_floor"] = "1"
        value["rows"] = [row]
    elif invalid == "rows":
        value["rows"] = [{}] * 65
    elif invalid == "duplicates":
        value["rows"] = [row, row]
    elif invalid == "epoch":
        value["lease_epoch"] = True
    elif invalid == "source":
        row["source"] = {"private": "x" * 8192}
        value["rows"] = [row]
    app = usage_application(database, api_capture, case)
    before = await usage_snapshot(database, case)

    async with client_for(app) as client:
        options = {"Authorization": "Bearer " + case.bearer_token}
        if invalid == "bytes":
            response = await client.post(
                usage_path(case), content=b"x" * (4 * 1024 * 1024 + 1), headers=options
            )
        else:
            response = await client.post(usage_path(case), json=value, headers=options)
    after = await usage_snapshot(database, case)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "API003"
    assert "private" not in response.text
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["%00", "x" * 257])
async def test_usage_api_refuses_malformed_attempt_identifier_before_lookup(
    database: Database, api_capture: Capture, identifier: str
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case)
    ledger = ConnectionLedger(database)

    async with client_for(app) as client:
        response = await client.post(
            f"/api/v1/attempts/{identifier}/usage",
            headers={"Authorization": "Bearer " + case.bearer_token},
            json=report(case).model_dump(mode="json"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "API003"
    assert ledger.checkouts == 1
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True], ids=["asgi", "http"])
async def test_usage_api_transfers_complete_maximal_rows(
    database: Database, api_capture: Capture, network: bool
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case)
    rows = tuple(
        usage(
            model="😀" * 255 + chr(0x1F300 + index),
            observation_id="😀" * 256,
            observation_sequence=MAX_SUM,
            input_tokens=MAX_TOKEN,
            cached_input_tokens=MAX_TOKEN,
            cache_creation_tokens=MAX_TOKEN,
            output_tokens=MAX_TOKEN,
            reasoning_tokens=MAX_TOKEN,
            cache_creation_by_ttl={"5m": MAX_TOKEN, "1h": 0},
            reported_usd=str(MAX_MONEY),
            source={"value": "é" * 4090},
        )
        for index in range(64)
    )
    body = report(case, rows=rows)
    ledger = ConnectionLedger(database)

    async with running_app(app) as origin:
        async with KoraClient(
            origin if network else "http://127.0.0.1",
            case.bearer_token,
            asgi_app=None if network else app,
        ) as client:
            accepted = await client.report_usage(case.attempt_id, body)

    assert len(accepted.rows) == 64
    assert accepted.coverage == body.coverage
    for source, received in zip(rows, accepted.rows, strict=True):
        assert received.model_dump(
            mode="json", exclude={"usd_estimate"}
        ) == source.model_dump(mode="json")
        assert received.usd_estimate is None
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_usage_api_openapi_declares_service_operation_and_closed_body(
    database: Database, api_capture: Capture
) -> None:
    case = await usage_case(database)
    personal = await seed_case(database, "schema")
    app = usage_application(database, api_capture, case)

    async with KoraClient("http://127.0.0.1", bearer(personal), asgi_app=app) as client:
        schema = await client.openapi()
    async with client_for(app) as client:
        refused = await client.get(
            usage_path(case), headers={"Authorization": "Bearer " + case.bearer_token}
        )

    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == {
        "/healthz",
        "/api/v1/workers",
        "/api/v1/attempts/{att}/usage",
    }
    operation = paths["/api/v1/attempts/{att}/usage"]
    assert isinstance(operation, dict)
    post = operation["post"]
    assert isinstance(post, dict)
    assert post["operationId"] == "report_usage"
    components = schema["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)
    request = schemas["UsageReport"]
    assert isinstance(request, dict)
    assert request["additionalProperties"] is False
    assert refused.status_code == 405
    assert refused.headers["Allow"] == "POST"
