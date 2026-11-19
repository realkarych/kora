import pytest
from sqlalchemy import update

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt
from kora.db.worker_claims import (
    claim_row,
)
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
async def test_claim_scalar_read_refuses_wide_sqlite_row(database: Database) -> None:
    scope = await seed(database)
    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(error={"content": "x" * 50_000}))

    async with database.read_transaction() as reader:
        if database.dialect is Dialect.SQLITE:
            with pytest.raises(ClaimSourcesRejected):
                await claim_row(
                    reader, attempt, attempt.c.id == scope.attempt_id, ("id",), 4096
                )
        else:
            row = await claim_row(
                reader, attempt, attempt.c.id == scope.attempt_id, ("id",), 4096
            )
            assert row is not None
            assert row["id"] == scope.attempt_id

    assert database.units_in_flight() == 0
