import pytest
from sqlalchemy import text

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.engine.attempt_claims import ClaimOutcome, claim_attempt
from kora.engine.writer_mode import WRITABLE_POSTGRES_MODE
from tests.api.deployment_cases import evaluator
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    SOURCE_LIMITS,
    UNCERTAINTY,
    WORKER_BYTES,
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("mode", ["repeatable_read", "readonly"])
@pytest.mark.asyncio
async def test_claim_requires_fresh_writable_postgres_transaction(
    database: Database, evaluator: Evaluator, mode: str
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    before = await state(database)

    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.execute(
                text(
                    "SET TRANSACTION READ ONLY"
                    if mode == "readonly"
                    else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
            )
            context = await case.context(writer)
            with executed_queries(writer) as statements:
                with pytest.raises(ClaimSourcesRejected):
                    await claim_attempt(
                        context,
                        case.binding.attempt_id,
                        case.query,
                        case.principal,
                        prepared,
                        SOURCE_LIMITS,
                        HISTORY_LIMITS,
                        WORKER_BYTES,
                        UNCERTAINTY,
                    )
        assert [sql for sql, _ in statements] == [WRITABLE_POSTGRES_MODE.text]
        assert await state(database) == before
    else:
        decision = await case.claim(database)
        assert decision.outcome is ClaimOutcome.GRANTED

    assert database.units_in_flight() == 0
    assert database.writer_is_free()
