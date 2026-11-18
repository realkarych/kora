import pytest

from kora.db.connection import Database
from tests.engine.full_claim_admission import exercise_full_claim


@pytest.mark.asyncio
async def test_full_claim_preparation_precedes_physical_reservation(
    database: Database,
) -> None:
    await exercise_full_claim(database, "node_policy", 1)
