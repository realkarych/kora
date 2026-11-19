import pytest

from kora.db.connection import Database
from kora.ir.json import JsonValue
from tests.engine.artifact_projection_native import round_trip_projection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        9007199254740993,
        9223372036854775807,
    ],
)
async def test_cel_projection_number_kinds_round_trip(
    database: Database, value: JsonValue
) -> None:
    await round_trip_projection(database, value)
