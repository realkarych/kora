import pytest
from sqlalchemy.exc import DBAPIError

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt
from kora.db.worker_claims import (
    claim_row,
)
from tests.engine.test_grant_history import seed


@pytest.mark.asyncio
async def test_claim_boolean_projection_refuses_malformed_state(
    database: Database,
) -> None:
    scope = await seed(database)

    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "UPDATE attempt SET current_dispatcher_open = 'invalid'"
            )
        async with database.read_transaction() as reader:
            with pytest.raises(ClaimSourcesRejected):
                await claim_row(
                    reader,
                    attempt,
                    attempt.c.id == scope.attempt_id,
                    ("current_dispatcher_open",),
                    65_536,
                )
    else:
        with pytest.raises(DBAPIError):
            async with database.transaction() as writer:
                await writer.exec_driver_sql(
                    "UPDATE attempt SET current_dispatcher_open = 'invalid'"
                )

    assert database.units_in_flight() == 0
