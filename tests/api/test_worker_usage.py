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
    run,
    usage_record,
    worker,
)
from kora.db.tokens import REVOKED
from kora.ir.cost import MAX_TOKEN
from kora.ir.json import DEFAULT_MAX_DEPTH, JsonObject
from kora.models.workers import WorkerRegistration
from kora.protocols.identity_records import ProcessIdentity
from tests.api.support import api_capture
from tests.api.usage_support import report, usage_application
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import usage_case, usage_snapshot

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
