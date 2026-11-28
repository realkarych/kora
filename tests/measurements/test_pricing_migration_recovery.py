import pytest

from kora.db.connection import Database, DatabaseSettings
from kora.db.migrations import current_revision
from kora.db.run_event_page_v1 import RUN_EVENT_REVISION
from tests.db.predecessor import upgrade_to_run_event_metadata
from tests.measurements.harness import new_report
from tests.measurements.pricing_lifetime_support import pricing_close_recovery


@pytest.mark.asyncio
async def test_pricing_measurement_recovers_to_current_schema(
    empty_database_settings: DatabaseSettings,
) -> None:
    report = new_report()

    await pricing_close_recovery(empty_database_settings, report)

    database = Database(empty_database_settings)
    try:
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
    finally:
        await database.close()
    rerun = await upgrade_to_run_event_metadata(empty_database_settings)
    assert revision == RUN_EVENT_REVISION
    assert (rerun.from_revision, rerun.to_revision, rerun.steps) == (
        RUN_EVENT_REVISION,
        RUN_EVENT_REVISION,
        0,
    )
