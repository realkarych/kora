import asyncio

import pytest
from sqlalchemy import text

from kora.db.connection import Database, Dialect
from tests.db.closing import until
from tests.measurements.metadata_lifetime_support import CheckoutBalance, StatementWatch
from tests.measurements.pricing_lifetime_support import pricing_quiescence


@pytest.mark.asyncio
async def test_pricing_quiescence_waits_for_server_release(database: Database) -> None:
    balance = CheckoutBalance(database)
    watch = StatementWatch(database, "pg_stat_activity")
    external = Database(database.settings)

    try:
        async with asyncio.TaskGroup() as group:
            async with external.read_transaction() as connection:
                await connection.execute(text("SELECT 1"))
                waiting = group.create_task(
                    pricing_quiescence(database, balance, len(asyncio.all_tasks()) + 1)
                )
                await until(lambda: watch.observed >= 2 or waiting.done())
                if database.dialect is Dialect.POSTGRESQL:
                    assert not waiting.done()
    finally:
        await external.close()

    assert waiting.result() is None
    assert database.units_in_flight() == 0
    assert balance.outstanding == 0
    assert database.writer_is_free()
