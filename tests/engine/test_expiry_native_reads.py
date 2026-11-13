from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.db.connection import Database, Dialect
from kora.db.tables import attempt
from kora.engine.expiry import observe_run_expiry
from kora.engine.expiry_context import ExpiryFailed, ExpiryState
from kora.engine.lease_recovery import recover_expired_lease
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.engine.expiry_support import execution_case


@pytest.mark.asyncio
async def test_run_expiry_accepts_compressed_long_timeout(database: Database) -> None:
    case = await execution_case(database, timeout="0" * 4096 + "1s")
    case.promotion.clock.advance(1)

    async with database.transaction() as writer:
        result = await observe_run_expiry(await case.context(writer, False))
    saved, _, _ = await case.promotion.snapshot(database)

    assert result is ExpiryState.EXPIRED
    assert saved["deadline_expired_at"] == case.promotion.clock.now()
    assert saved["limits"] == {"timeout": "0" * 4096 + "1s"}


@pytest.mark.asyncio
async def test_attempt_read_admission_precedes_late_projection(
    database: Database,
) -> None:
    case = await execution_case(database)
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.attempt_id)
                .values(recovery={"retained": "private-retained" * 100_000})
            )
    case.promotion.clock.advance(30)
    before = await case.snapshot(database)

    async with database.transaction() as writer:
        context = replace(await case.context(writer, False), attempt_bytes=1024)
        with executed_queries(writer) as queries:
            recovered = await recover_expired_lease(context, case.attempt_id)
        if database.dialect is Dialect.SQLITE:
            width_sql, width_args = queries[-1]
            program = await sqlite_instructions(writer, width_sql, width_args)
            wide_columns = {
                index
                for index, column in enumerate(attempt.c)
                if column.name
                in {
                    "recovery",
                    "handle",
                    "input_bindings",
                    "execution_request",
                    "scratch",
                    "counters",
                    "outcome",
                    "deadline_binding",
                }
            }
            reads = [
                row for row in program if row[1] == "Column" and row[3] in wide_columns
            ]
            assert reads
            assert all(type(row[6]) is int and row[6] & (0x40 | 0x80) for row in reads)
    after = await case.snapshot(database)

    assert recovered is (database.dialect is Dialect.POSTGRESQL)
    if database.dialect is Dialect.SQLITE:
        assert after == before
        assert not any(
            "SELECT attempt.id, attempt.node_instance_id" in sql for sql, _ in queries
        )
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_corrupt_original_outcome_refuses_without_mutating_recovery(
    database: Database,
) -> None:
    case = await execution_case(database, status="finishing")
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.attempt_id)
            .values(
                execution_state="stopped",
                outcome={"private-invalid-receipt": "private-canary"},
                outcome_payload_state="not_applicable",
            )
        )
    case.promotion.clock.advance(30)
    before = await case.snapshot(database)

    async with database.transaction() as writer:
        context = await case.context(writer, False)
        with pytest.raises(ExpiryFailed, match="execution authority expiry failed"):
            await recover_expired_lease(context, case.attempt_id)
    after = await case.snapshot(database)

    assert after == before
    assert database.units_in_flight() == 0
