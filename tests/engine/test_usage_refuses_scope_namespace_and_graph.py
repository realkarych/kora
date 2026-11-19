import pytest

from kora.db.connection import Database
from tests.engine.usage_behavior import refuse_changed_prepared_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["namespace", "graph"])
async def test_usage_refuses_changed_prepared_scope(
    database: Database, binding: str
) -> None:
    await refuse_changed_prepared_scope(database, binding)
