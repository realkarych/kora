import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database, Dialect
from kora.engine.attempt_claims import ClaimOutcome, claim_attempt
from tests.api.deployment_cases import evaluator
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    SOURCE_LIMITS,
    UNCERTAINTY,
    WORKER_BYTES,
    another_run,
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_selected_attempt_lock_has_its_original_run_predicate(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    other = await another_run(database, case)
    before = await state(database)

    async with database.transaction() as writer:
        context = await case.context(writer)
        with executed_queries(writer) as statements:
            decision = await claim_attempt(
                context,
                other.binding.attempt_id,
                case.query,
                case.principal,
                None,
                SOURCE_LIMITS,
                HISTORY_LIMITS,
                WORKER_BYTES,
                UNCERTAINTY,
            )
    after = await state(database)

    assert decision.outcome is ClaimOutcome.INELIGIBLE
    assert before == after
    [(sql, _)] = [(sql, args) for sql, args in statements if "FROM attempt " in sql]
    assert "EXISTS (SELECT node_instance.id" in sql
    assert "node_instance.id = attempt.node_instance_id" in sql
    assert "node_instance.run_id =" in sql
    if database.dialect is Dialect.POSTGRESQL:
        assert sql.endswith("FOR UPDATE SKIP LOCKED")
