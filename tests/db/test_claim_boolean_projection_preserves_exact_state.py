import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.tables import attempt
from kora.db.worker_claims import (
    claim_row,
)
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, True])
async def test_claim_boolean_projection_preserves_exact_state(
    database: Database,
    value: bool | None,
) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(current_dispatcher_open=value))

    async with database.read_transaction() as reader:
        saved = await claim_row(
            reader,
            attempt,
            attempt.c.id == scope.attempt_id,
            ("current_dispatcher_open",),
            65_536,
        )

    assert saved is not None
    assert saved["current_dispatcher_open"] is value
