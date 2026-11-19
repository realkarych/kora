import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import refuse_stale_write_authority


@pytest.mark.asyncio
async def test_usage_preparation_does_not_preserve_stale_write_authority(
    database: Database,
) -> None:
    await refuse_stale_write_authority(database)
