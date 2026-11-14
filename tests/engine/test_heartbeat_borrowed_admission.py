from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history import GrantHistoryRejected
from kora.engine.attempt_heartbeats import heartbeat_attempt
from tests.api.deployment_cases import evaluator
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.claim_support import WORKER_BYTES, claim_case, state
from tests.engine.heartbeat_support import HEARTBEAT_LIMITS, heartbeat_body

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["native", "raw"])
async def test_borrowed_expiry_allowance_is_admitted_before_any_heartbeat_read(
    database: Database, evaluator: Evaluator, budget: str
) -> None:
    case = await claim_case(database, evaluator)
    claim = await case.claim(database)
    assert claim.delivery is not None
    grant = claim.delivery.system
    before = await state(database)
    parent_bytes = (
        HEARTBEAT_LIMITS.native_bytes + 1
        if budget == "native"
        else HEARTBEAT_LIMITS.raw_bytes + 1
    )

    async with database.transaction() as writer:
        authority = await case.context(writer)
        authority = replace(
            authority, limits=replace(authority.limits, parent_bytes=parent_bytes)
        )
        with executed_queries(writer) as statements:
            with pytest.raises(GrantHistoryRejected):
                await heartbeat_attempt(
                    authority,
                    grant.attempt_id,
                    heartbeat_body(grant),
                    case.principal,
                    HEARTBEAT_LIMITS,
                    WORKER_BYTES,
                )
    after = await state(database)

    assert statements == []
    assert before == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
