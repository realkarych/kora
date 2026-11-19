import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import recheck_grant_after_waiting_for_run_lock


@pytest.mark.asyncio
async def test_usage_rechecks_grant_after_waiting_for_run_lock(
    database: Database,
) -> None:
    await recheck_grant_after_waiting_for_run_lock(database)
