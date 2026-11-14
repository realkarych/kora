from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import run
from kora.ir.json import JsonObject
from kora.protocols.runtime import RuntimeStatus
from tests.api.claim_support import admit_expiry_run
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger", ["own_deadline", "run_deadline", "lost", "nonzero_exit", "revoked"]
)
async def test_heartbeat_records_each_first_recovery_transition_once(
    database: Database, evaluator: Evaluator, trigger: str
) -> None:
    ir = skeleton_ir()
    if trigger == "own_deadline":
        graph = cast(JsonObject, ir["graph"])
        cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])["limits"] = {
            "timeout": "1s"
        }
    elif trigger == "run_deadline":
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1s" if trigger == "run_deadline" else "1h")
    claim = await case.claim(database)
    assert claim.delivery is not None
    grant = claim.delivery.system
    clock = AuthorityClock(case.clock.now() + timedelta(seconds=1))
    body = heartbeat_body(grant)
    if trigger in ("lost", "nonzero_exit"):
        body = body.model_copy(
            update={
                "observation": RuntimeStatus(
                    state="lost" if trigger == "lost" else "exited",
                    exit_code=None if trigger == "lost" else 1,
                    phase="execution",
                    observed_at=clock.now(),
                )
            }
        )
    if trigger == "revoked":
        async with database.transaction() as writer:
            await writer.execute(
                update(run)
                .where(run.c.id == case.binding.run.run_id)
                .values(status="cancelled")
            )
    before = await state(database)

    await heartbeat(database, case, clock, body)
    after = await state(database)
    await heartbeat(database, case, clock, body)
    replay = await state(database)

    events = [
        item for item in after["event"] if item["kind"] == "attempt.recovery_required"
    ]
    assert before["attempt"][0]["recovery_required"] is False
    assert after["attempt"][0]["recovery_required"] is True
    assert len(events) == 1
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert set(cast(dict[str, object], payload)) == {"number", "reasons"}
    assert payload["number"] == after["attempt"][0]["number"]
    assert payload["reasons"] == [
        "deadline_expired"
        if trigger.endswith("deadline")
        else "authority_revoked"
        if trigger == "revoked"
        else "execution_lost"
    ]
    assert after == replay
