from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import event

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.worker_history import HistoryAccessRejected
from kora.ir.json import JsonObject
from tests.api.claim_support import admit_expiry_run
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["attempt", "run"])
async def test_observed_heartbeat_expiry_cannot_reopen_after_wall_clock_rollback(
    database: Database, evaluator: Evaluator, scope: str
) -> None:
    ir = skeleton_ir()
    if scope == "attempt":
        graph = cast(JsonObject, ir["graph"])
        node = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
        node["limits"] = {"timeout": "1s"}
    else:
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1s" if scope == "run" else "1h")
    claim = await case.claim(database)
    assert claim.delivery is not None
    grant = claim.delivery.system
    body = heartbeat_body(grant).model_copy(
        update={"grant_history_complete": grant.grant_history}
    )
    clock = AuthorityClock(case.clock.now() + timedelta(seconds=1))

    expired = await heartbeat(database, case, clock, body)
    after_expiry = await state(database)
    clock.moment -= timedelta(seconds=1)
    rolled_back = await heartbeat(database, case, clock, body)
    after_rollback = await state(database)

    assert expired.desired_state == rolled_back.desired_state == "cancel"
    assert expired.mode == rolled_back.mode == "recover"
    assert "seal_request" not in rolled_back.permitted_actions
    assert after_expiry["attempt"][0]["cancel_cause"] == "timeout"
    assert after_rollback["attempt"][0]["desired_state"] == "cancel"
    assert (
        after_expiry["run" if scope == "run" else "attempt"][0]["deadline_expired_at"]
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("crossing", ["final_deadline", "expiry_lease", "backward"])
async def test_heartbeat_final_authority_covers_time_changes_during_writes(
    database: Database, evaluator: Evaluator, crossing: str
) -> None:
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1s")
    claim = await case.claim(database)
    assert claim.delivery is not None
    grant = claim.delivery.system
    start = case.clock.now()
    deadline = start + timedelta(seconds=1)
    clock = AuthorityClock(start if crossing == "final_deadline" else deadline)
    body = heartbeat_body(grant).model_copy(
        update={"grant_history_complete": grant.grant_history}
    )
    before = await state(database)
    transitions: list[str] = []

    def advance_after_write(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        if transitions:
            return
        if crossing == "expiry_lease":
            if (
                statement.startswith("UPDATE run ")
                and "deadline_expired_at" in statement
            ):
                clock.moment = grant.lease_until
                transitions.append("expiry")
        elif (
            statement.startswith("UPDATE attempt ") and "last_heartbeat_at" in statement
        ):
            clock.moment = deadline if crossing == "final_deadline" else start
            transitions.append("heartbeat")

    target = database.writer_engine.sync_engine
    event.listen(target, "after_cursor_execute", advance_after_write)
    try:
        if crossing == "expiry_lease":
            with pytest.raises(HistoryAccessRejected) as refused:
                await heartbeat(database, case, clock, body)
            assert refused.value.reason == "stale"
        else:
            result = await heartbeat(database, case, clock, body)
            assert result.desired_state == "cancel"
            assert result.mode == "recover"
            assert "seal_request" not in result.permitted_actions
    finally:
        event.remove(target, "after_cursor_execute", advance_after_write)
    after = await state(database)

    assert len(transitions) == 1
    if crossing == "expiry_lease":
        assert before == after
    else:
        assert after["run"][0]["deadline_expired_at"] == deadline
        assert after["attempt"][0]["cancel_cause"] == "timeout"
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
