import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import keep_historical_resource_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expiring", "expired"])
async def test_usage_preparation_keeps_historical_resource_metadata(
    database: Database, state: str
) -> None:
    await keep_historical_resource_metadata(database, state)
