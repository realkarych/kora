import asyncio

import pytest
from sqlalchemy import event

from kora.db.connection import Database
from kora.engine.admission_discovery import AdmissionDiscoveryFailed, AdmissionSweep
from tests.db.closing import ConnectionLedger
from tests.engine.promotion_support import promotion_case


@pytest.mark.asyncio
async def test_admission_discovery_failure_is_private_and_releases_reader(
    database: Database,
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    ledger = ConnectionLedger(database)
    sweep = AdmissionSweep()

    async def unavailable() -> None:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE node_instance RENAME TO private_unavailable_nodes"
            )
            await sweep.next_page(writer)

    with pytest.raises(AdmissionDiscoveryFailed) as caught:
        await unavailable()
    async with database.read_transaction() as reader:
        recovered = await sweep.next_page(reader)

    assert str(caught.value) == "admission discovery failed"
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert len(recovered.node_ids) == 2
    assert recovered.wrapped
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("query_number", [1, 2])
async def test_admission_discovery_cancelled_query_releases_reader(
    database: Database, query_number: int
) -> None:
    case = await promotion_case(database)
    await case.promote(database)
    sweep = AdmissionSweep()
    ledger = ConnectionLedger(database)
    observed = 0

    def cancel_at_dispatch(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        nonlocal observed
        if "node_instance" in statement and statement.startswith("SELECT"):
            observed += 1
            if observed == query_number:
                task = asyncio.current_task()
                assert task is not None
                task.cancel()

    async def read() -> None:
        async with database.read_transaction() as reader:
            event.listen(
                reader.sync_connection, "before_cursor_execute", cancel_at_dispatch
            )
            try:
                await sweep.next_page(reader)
            finally:
                event.remove(
                    reader.sync_connection, "before_cursor_execute", cancel_at_dispatch
                )

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(read())
    async with database.read_transaction() as reader:
        recovered = await sweep.next_page(reader)

    assert observed == query_number
    assert len(recovered.node_ids) == 2
    assert recovered.wrapped
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
