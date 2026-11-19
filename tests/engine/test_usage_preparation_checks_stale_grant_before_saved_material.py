import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import check_stale_grant_before_saved_material


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["ir", "resources"])
async def test_usage_preparation_checks_stale_grant_before_saved_material(
    database: Database, field: str
) -> None:
    await check_stale_grant_before_saved_material(database, field)
