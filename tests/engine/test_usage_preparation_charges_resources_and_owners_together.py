import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import charge_resources_and_owners_together


@pytest.mark.asyncio
@pytest.mark.parametrize("resources", [False, True])
async def test_usage_preparation_charges_resources_and_owners_together(
    database: Database, resources: bool
) -> None:
    await charge_resources_and_owners_together(database, resources)
