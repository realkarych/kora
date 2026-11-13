from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import (
    api_token,
    attempt,
    graph_version,
    node_instance,
    run,
    usage_record,
    worker,
)
from kora.db.tokens import REVOKED
from kora.ir.cost import MAX_MONEY, MAX_SUM, MAX_TOKEN
from kora.ir.json import DEFAULT_MAX_DEPTH, JsonObject
from kora.models.workers import WorkerRegistration
from kora.protocols.identity_records import ProcessIdentity
from tests.api.support import HttpCall, api_capture, client_for
from tests.api.usage_support import report, usage_application, usage_path
from tests.api.worker_support import bearer
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import usage_case, usage_snapshot
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True], ids=["asgi", "http"])
@pytest.mark.parametrize("new_domain", [False, True])
async def test_usage_api_accepts_retained_grant_after_worker_reregistration(
    database: Database, api_capture: Capture, network: bool, new_domain: bool
) -> None:
    case = await usage_case(database)
    async with database.read_transaction() as reader:
        original_domain = await reader.scalar(
            select(attempt.c.execution_domain_id).where(attempt.c.id == case.attempt_id)
        )
    assert isinstance(original_domain, str)
    registration = WorkerRegistration(
        name=case.principal.owner,
        incarnation_id="replacement-incarnation",
        runtime="local",
        execution_domain_id="replacement-domain" if new_domain else original_domain,
        version="0.1.0",
        capacity=1,
        process=ProcessIdentity(
            host_id="host",
            boot_id="boot",
            pid_namespace="namespace",
            pid=2345,
            start_time=600,
            leads_group=True,
        ),
    )
    body = report(case)
    app = usage_application(database, api_capture, case)
    ledger = ConnectionLedger(database)

    async with running_app(app) as origin:
        async with KoraClient(
            origin if network else "http://127.0.0.1",
            case.bearer_token,
            asgi_app=None if network else app,
        ) as client:
            registered = await client.register_worker(registration)
            first = await client.report_usage(case.attempt_id, body)
            before_replay = await usage_snapshot(database, case)
            replay = await client.report_usage(case.attempt_id, body)
            after_replay = await usage_snapshot(database, case)
    async with database.read_transaction() as reader:
        latest_incarnation = await reader.scalar(
            select(worker.c.incarnation_id).where(worker.c.id == case.principal.owner)
        )

    assert (
        registered.incarnation_id == latest_incarnation == registration.incarnation_id
    )
    assert first.rows[0].usd_estimate == "1"
    assert first.coverage == body.coverage
    assert first == replay
    assert before_replay == after_replay
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [False, True], ids=["asgi", "http"])
async def test_usage_api_returns_submitted_rows_and_readonly_replay(
    database: Database, api_capture: Capture, network: bool
) -> None:
    case = await usage_case(database)
    source: JsonObject = {"private\0": [1, 1.0, -0.0]}
    for _ in range(DEFAULT_MAX_DEPTH - 3):
        source = {"nested": source}
    rows = (usage(model="missing"), usage(input_tokens=1_000_000, source=source))
    body = report(case, rows=rows)
    app = usage_application(database, api_capture, case)
    ledger = ConnectionLedger(database)

    async with running_app(app) as origin:
        async with KoraClient(
            origin if network else "http://127.0.0.1",
            case.bearer_token,
            asgi_app=None if network else app,
        ) as client:
            first = await client.report_usage(case.attempt_id, body)
            async with database.transaction() as connection:
                await connection.execute(update(run).values(needs_scheduling=False))
            before = await usage_snapshot(database, case)
            case.clock.moment += timedelta(seconds=1)
            replay = await client.report_usage(case.attempt_id, body)
            subset = await client.report_usage(
                case.attempt_id, report(case, rows=(rows[1],))
            )
            after = await usage_snapshot(database, case)
            async with database.transaction() as connection:
                await connection.execute(
                    update(api_token)
                    .where(api_token.c.id == case.principal.token_id)
                    .values(expires_at=REVOKED)
                )
            with pytest.raises(ApiError) as refused:
                await client.report_usage(case.attempt_id, body)
    records = api_capture.finish()

    assert [row.model for row in first.rows] == [row.model for row in rows]
    assert [row.usd_estimate for row in first.rows] == [None, "1"]
    assert first.rows[1].model_dump(mode="json")["source"] == source
    assert "same_basis_usd_floor" not in first.model_dump_json()
    assert first == replay
    assert subset.rows == first.rows[1:]
    assert subset.coverage == body.coverage
    assert before == after
    assert refused.value.code == "API009"
    assert ledger.outstanding == database.units_in_flight() == 0
    assert case.bearer_token not in str(records)
    assert "private" not in str(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["missing", "user", "unknown"])
async def test_usage_api_authenticates_before_reading_body(
    database: Database, api_capture: Capture, credential: str
) -> None:
    case = await usage_case(database)
    personal = await seed_case(database, "personal")
    authorization = {
        "missing": "",
        "user": personal.request.authorization or "",
        "unknown": "Bearer unknown",
    }[credential]
    call = HttpCall(
        path=usage_path(case),
        method="POST",
        headers=[(b"authorization", authorization.encode())],
    )
    before = await usage_snapshot(database, case)

    await call.run(usage_application(database, api_capture, case))
    after = await usage_snapshot(database, case)

    assert call.receives == 0
    assert call.status == (403 if credential == "user" else 401)
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("same_namespace", [False, True])
@pytest.mark.parametrize("corrupt_binding", ["none", "binding", "coverage"])
async def test_usage_api_hides_foreign_namespace_and_refuses_other_worker(
    database: Database,
    api_capture: Capture,
    same_namespace: bool,
    corrupt_binding: str,
) -> None:
    case = await usage_case(database)
    other = await seed_case(database, "other", "service")
    if corrupt_binding == "binding":
        async with database.transaction() as connection:
            await connection.execute(update(node_instance).values(node_path="x" * 1025))
    elif corrupt_binding == "coverage":
        async with database.transaction() as connection:
            await connection.execute(
                update(attempt).values(usage_coverage={"private": "x" * 60000})
            )
    if same_namespace:
        async with database.transaction() as connection:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == other.principal.token_id)
                .values(namespaces=case.principal.model_dump(mode="json")["namespaces"])
            )
    app = usage_application(database, api_capture, case)
    before = await usage_snapshot(database, case)

    async with KoraClient("http://127.0.0.1", bearer(other), asgi_app=app) as client:
        with pytest.raises(ApiError) as refusal:
            await client.report_usage(case.attempt_id, report(case))
        with pytest.raises(ApiError) as missing:
            await client.report_usage("absent", report(case))
    after = await usage_snapshot(database, case)

    assert refusal.value.code == ("API005" if same_namespace else "API001")
    assert missing.value.code == "API001"
    assert before == after


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
@pytest.mark.parametrize("corrupt", ["contract", "coverage", "binding"])
async def test_usage_api_stale_grant_precedes_corrupt_contract(
    database: Database, api_capture: Capture, corrupt: str
) -> None:
    case = await usage_case(database)
    app = usage_application(database, api_capture, case)
    async with database.transaction() as connection:
        if corrupt == "contract":
            await connection.execute(
                update(graph_version).values(ir={"private": "bad"})
            )
        elif corrupt == "coverage":
            await connection.execute(
                update(attempt).values(usage_coverage={"private": "x" * 60000})
            )
        else:
            await connection.execute(update(node_instance).values(node_path="x" * 1025))
    before = await usage_snapshot(database, case)

    async with KoraClient(
        "http://127.0.0.1", case.bearer_token, asgi_app=app
    ) as client:
        with pytest.raises(ApiError) as stale:
            await client.report_usage(case.attempt_id, report(case, lease_epoch=2))
        with pytest.raises(ApiError) as corrupt_refusal:
            await client.report_usage(case.attempt_id, report(case))
    after = await usage_snapshot(database, case)
    records = api_capture.finish()

    assert stale.value.code == "API008"
    assert corrupt_refusal.value.code == "API010"
    assert before == after
    assert "private" not in str(records)
    assert sum(record["event"] == "api.request.failed" for record in records) == 1


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
async def test_usage_api_rolls_back_derived_money_overflow(
    database: Database, api_capture: Capture
) -> None:
    case = await usage_case(database, {"input": "99999999999999"})
    app = usage_application(database, api_capture, case)
    body = report(case, rows=(usage(model="first"), usage(input_tokens=MAX_TOKEN)))
    before = await usage_snapshot(database, case)

    async with KoraClient(
        "http://127.0.0.1", case.bearer_token, asgi_app=app
    ) as client:
        with pytest.raises(ApiError) as refused:
            await client.report_usage(case.attempt_id, body)
    after = await usage_snapshot(database, case)

    assert refused.value.code == "API010"
    error = refused.value.body["error"]
    assert isinstance(error, dict)
    assert error["retryable"] is False
    assert before == after


@pytest.mark.asyncio
async def test_usage_api_retains_floor_through_null_and_accepts_final_correction(
    database: Database, api_capture: Capture
) -> None:
    case = await usage_case(database, {"cache_creation_1h": "2"})
    app = usage_application(database, api_capture, case)
    first = usage(
        cache_creation_tokens=1_000_000,
        cache_creation_by_ttl={"5m": 0, "1h": 1_000_000},
    )
    unknown = usage(cache_creation_tokens=1_000_000, observation_sequence=1)
    lower = usage(
        cache_creation_tokens=1_000_000,
        cache_creation_by_ttl={"5m": 1_000_000, "1h": 0},
        observation_sequence=2,
    )
    corrected = usage(
        cache_creation_tokens=1_000_000,
        observation_sequence=2,
        basis="harness_list",
        reported_usd="1",
    )

    async with KoraClient(
        "http://127.0.0.1", case.bearer_token, asgi_app=app
    ) as client:
        accepted = await client.report_usage(
            case.attempt_id, report(case, rows=(first,))
        )
        nullable = await client.report_usage(
            case.attempt_id, report(case, rows=(unknown,))
        )
        with pytest.raises(ApiError) as refused:
            await client.report_usage(case.attempt_id, report(case, rows=(lower,)))
        final = await client.report_usage(
            case.attempt_id, report(case, rows=(corrected,))
        )
    async with database.read_transaction() as connection:
        saved = (await connection.execute(select(usage_record))).mappings().one()

    assert accepted.rows[0].usd_estimate == "2"
    assert nullable.rows[0].usd_estimate is None
    assert refused.value.code == "API002"
    error = refused.value.body["error"]
    assert isinstance(error, dict)
    assert error["details"] == {"reason": "usage_regression"}
    assert final.rows[0].usd_estimate == "1"
    assert saved["same_basis_usd_floor"] == Decimal("1")


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
    assert set(paths) == {"/healthz", "/api/v1/attempts/{att}/usage"}
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
