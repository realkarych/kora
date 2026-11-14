import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text, update

from kora.db.connection import Database, Dialect
from kora.db.tables import usage_record
from kora.engine.approval_cost import ApprovalCostUnavailable, calculate_approval_cost
from kora.engine.locks import TransitionLocks
from tests.db.seed import START
from tests.engine.approval_cost_support import (
    cost_attempt,
    cost_case,
    cost_usage,
    coverage,
)
from tests.engine.lock_support import wait_for_lock_waiter


@pytest.mark.asyncio
async def test_approval_cost_reads_after_the_competing_writer_commits(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        usage_id = await cost_usage(writer, identity)
    original = await case.calculate(database)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def report() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.run_id)
            ready.set()
            await asyncio.wait_for(release.wait(), 10)
            await writer.execute(
                update(usage_record)
                .where(usage_record.c.id == usage_id)
                .values(usd_estimate=Decimal("0.2"))
            )

    reporter = asyncio.create_task(report())
    await asyncio.wait_for(ready.wait(), 10)
    calculator = asyncio.create_task(case.calculate(database))
    try:
        await wait_for_lock_waiter(database)
        assert not calculator.done()
        release.set()
        await asyncio.wait_for(reporter, 10)
        result = await asyncio.wait_for(calculator, 10)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(reporter, calculator), 10)

    assert original.usd_estimate == "0.1"
    assert result.usd_estimate == "0.2"
    assert original.models[0].usd_estimate == "0.1"
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readonly", "repeatable"])
async def test_approval_cost_requires_fresh_writable_reads(
    database: Database, mode: str
) -> None:
    case = await cost_case(database)

    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.execute(
                text(
                    "SET TRANSACTION READ ONLY"
                    if mode == "readonly"
                    else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
            )
            locks = TransitionLocks(writer)
            await locks.acquire_run(case.run_id)
            with pytest.raises(ApprovalCostUnavailable):
                await calculate_approval_cost(
                    writer, locks, case.namespace_id, case.run_id, START
                )
    result = await case.calculate(database)

    assert result.usd_estimate == "0"
    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["coverage", "usage"])
async def test_approval_cost_refuses_observations_after_snapshot_time(
    database: Database, source: str
) -> None:
    case = await cost_case(database)
    snapshot_time = START - timedelta(seconds=1)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer,
            case,
            "agent",
            saved_coverage=coverage(at=snapshot_time if source == "usage" else START),
        )
        if source == "usage":
            await cost_usage(writer, identity)

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.run_id)
        with pytest.raises(ApprovalCostUnavailable):
            await calculate_approval_cost(
                writer, locks, case.namespace_id, case.run_id, snapshot_time
            )

    assert database.writer_is_free()
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_approval_cost_preserves_unknown_dimensions_and_billing_sets(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, first = await cost_attempt(writer, case, "first", saved_coverage=coverage())
        _, second = await cost_attempt(
            writer, case, "second", saved_coverage=coverage(completeness="partial")
        )
        await cost_usage(writer, first)
        usage_id = await cost_usage(
            writer,
            second,
            amount="0.25",
            billing="gateway",
            completeness="partial",
        )
        await writer.execute(
            update(usage_record)
            .where(usage_record.c.id == usage_id)
            .values(
                basis="gateway", known_token_fields=["input_tokens", "output_tokens"]
            )
        )

    result = await case.calculate(database)

    assert result.usd_estimate == result.known_usd_estimate == "0.35"
    assert result.completeness == "partial"
    assert result.missing_report_attempts == 0
    assert len(result.models) == 1
    model = result.models[0]
    assert model.unknown_token_fields == (
        "cache_creation_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    assert model.billing_classes == ("api_key", "gateway")
    assert model.bases == ("gateway", "price_table")
    assert model.input_tokens == 6
    assert model.output_tokens == 10
    assert model.reasoning_tokens == 8


@pytest.mark.asyncio
async def test_approval_cost_requires_the_current_writer_connection(
    database: Database,
) -> None:
    case = await cost_case(database)

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(case.run_id)
        async with database.read_transaction() as reader:
            with pytest.raises(ApprovalCostUnavailable):
                await calculate_approval_cost(
                    reader, locks, case.namespace_id, case.run_id, START
                )
    with pytest.raises(ApprovalCostUnavailable):
        await calculate_approval_cost(
            writer, locks, case.namespace_id, case.run_id, START
        )

    assert database.units_in_flight() == 0
    assert database.writer_is_free()
