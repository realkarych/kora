from datetime import timedelta
from typing import cast

from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import attempt, node_instance
from kora.engine.expiry import observe_attempt_expiry
from kora.engine.expiry_context import ExpiryState
from kora.ir.json import JsonObject
from tests.api.claim_support import admit_expiry_run
from tests.api.deployment_cases import skeleton_ir
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.plugins.authentication.support import AuthorityClock


async def deadline_authority_ignores_scheduler_tick(
    database: Database, evaluator: Evaluator, scope: str, tick_first: bool
) -> None:
    ir = skeleton_ir()
    if scope == "attempt":
        graph = cast(JsonObject, ir["graph"])
        nodes = cast(JsonObject, graph["nodes"])
        cast(JsonObject, nodes["agent"])["limits"] = {"timeout": "1s"}
    else:
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1s" if scope == "run" else "1h")
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    case.clock.advance(1)
    body = heartbeat_body(granted).model_copy(
        update={"grant_history_complete": granted.grant_history}
    )
    if tick_first:
        async with database.transaction() as writer:
            assert (
                await observe_attempt_expiry(
                    await case.context(writer), granted.attempt_id
                )
                is ExpiryState.EXPIRED
            )

    result = await heartbeat(database, case, AuthorityClock(case.clock.now()), body)
    saved = await state(database)

    assert result.desired_state == "cancel"
    assert result.mode == "recover"
    assert result.permitted_actions == (
        ("observe", "retire", "report_result")
        if scope == "attempt"
        else ("observe", "retire")
    )
    assert saved["attempt"][0]["recovery_required"] is True
    assert saved["attempt"][0]["status"] == "claimed"
    assert saved["attempt"][0]["current_dispatcher_open"] is True


async def own_deadline_keeps_prior_revocation(
    database: Database, evaluator: Evaluator, prior: str
) -> None:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])["limits"] = {
        "timeout": "1s"
    }
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1h")
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    async with database.transaction() as writer:
        if prior == "cancel":
            await writer.execute(
                update(attempt).values(desired_state="cancel", cancel_cause="cancel")
            )
        elif prior == "superseded":
            await writer.execute(update(node_instance).values(current_attempt_id=None))
        else:
            await writer.execute(update(attempt).values(status="finishing"))
    body = heartbeat_body(granted).model_copy(
        update={"grant_history_complete": granted.grant_history}
    )

    result = await heartbeat(
        database, case, AuthorityClock(case.clock.now() + timedelta(seconds=1)), body
    )

    assert result.permitted_actions == ("observe", "retire")
