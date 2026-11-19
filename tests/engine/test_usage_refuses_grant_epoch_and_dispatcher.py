import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import refuse_current_grant_change_without_writes


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["epoch", "dispatcher"])
async def test_usage_refuses_current_grant_changes_without_writes(
    database: Database, changed: str
) -> None:
    await refuse_current_grant_change_without_writes(database, changed)
