import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import read_exact_saved_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("resources", [False, True])
async def test_usage_preparation_reads_the_exact_saved_contract(
    database: Database, resources: bool
) -> None:
    await read_exact_saved_contract(database, resources)
