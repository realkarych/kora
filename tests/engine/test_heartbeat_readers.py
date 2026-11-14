import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import attempt
from kora.engine.lease_recovery import recover_expired_lease
from kora.engine.worker_history import HistoryRead, read_worker_history
from kora.protocols.evidence import ClosureEvidence
from kora.protocols.history import GrantHistoryPosition
from tests.api.deployment_cases import evaluator
from tests.db.test_run_input_parent import open_sqlite
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, claim_case, state
from tests.engine.heartbeat_bounds import MAX_TEXT, maximal_counters, maximal_recovery
from tests.engine.heartbeat_support import (
    HEARTBEAT_LIMITS,
    heartbeat,
    heartbeat_body,
    self_closure,
)
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


async def maximum_reader_handoff(database: Database, evaluator: Evaluator) -> None:
    case = await claim_case(database, evaluator, execution_domain_id=MAX_TEXT)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    maximum = maximal_recovery().worker
    assert maximum is not None
    assert maximum.normal_completion is not None
    assert maximum.termination is not None
    normal = maximum.normal_completion.model_copy(
        update={"attempt_id": granted.attempt_id}
    )
    termination = maximum.termination.model_copy(
        update={"attempt_id": granted.attempt_id}
    )
    counters = maximal_counters()
    closure = self_closure(granted)
    closure = closure.model_copy(
        update={
            "quiescence": closure.quiescence.model_copy(
                update={"evidence_ref": MAX_TEXT}
            )
        }
    )
    clock = AuthorityClock()
    limits = replace(HEARTBEAT_LIMITS, attempt_row=131_072)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                scratch={"retained": "x" * 8192}, recovery={"reason": MAX_TEXT}
            )
        )
    before = await state(database)
    body = heartbeat_body(granted)

    await heartbeat(
        database,
        case,
        clock,
        body.model_copy(
            update={
                "counters": counters,
                "observation": maximum.last_observation,
                "evidence": ClosureEvidence(
                    runtime=normal, dispatchers=(closure,), publication=None
                ),
            }
        ),
        limits=limits,
    )
    await heartbeat(
        database,
        case,
        clock,
        body.model_copy(
            update={
                "evidence": ClosureEvidence(
                    runtime=termination, dispatchers=(), publication=None
                )
            }
        ),
        limits=limits,
    )
    after = await state(database)
    page = await read_worker_history(
        database,
        HistoryRead(
            granted.attempt_id,
            granted.current_grant.dispatcher_id,
            granted.lease_epoch,
            granted.grant_history.tip(),
            GrantHistoryPosition(
                after_epoch=None,
                received_count=0,
                prefix_digest=granted.grant_history.seed(),
            ),
        ),
        case.principal,
        clock,
        HISTORY_LIMITS,
        WORKER_BYTES,
        deadline=asyncio.get_running_loop().time() + 10,
    )
    replay = await case.claim(database)
    async with database.transaction() as writer:
        not_due = await recover_expired_lease(
            await case.context(writer), granted.attempt_id
        )

    assert page
    assert replay.delivery is not None
    assert replay.delivery.encode() == decision.delivery.encode()
    assert not not_due
    assert (
        after["attempt_claim_read_admission"] == before["attempt_claim_read_admission"]
    )
    assert after["attempt"][0]["scratch"] == before["attempt"][0]["scratch"]
    assert after["attempt"][0]["execution_state"] == "stopped"
    assert after["attempt"][0]["counters"] == counters.model_dump(mode="json")


@pytest.mark.asyncio
async def test_maximum_heartbeat_values_remain_readable_by_claim_history_and_expiry(
    database: Database, evaluator: Evaluator
) -> None:
    await maximum_reader_handoff(database, evaluator)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
async def test_maximum_heartbeat_row_growth_accounts_for_utf16(
    tmp_path: Path, evaluator: Evaluator, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        await maximum_reader_handoff(database, evaluator)
    finally:
        await database.close()
