from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    node_instance,
    run,
)
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.lease_recovery import recover_expired_lease
from kora.ir.json import JsonObject
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.db.seed import START
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize(
    "condition", ["running", "cancelled", "finishing", "terminal_run", "retired_node"]
)
@pytest.mark.asyncio
async def test_regrant_preserves_original_identity_deadline_and_resource_ownership(
    database: Database, evaluator: Evaluator, condition: str
) -> None:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    node = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
    node["limits"] = {"timeout": "1h"}
    case = await claim_case(database, evaluator, ir)
    first = await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                execution_state="live",
                desired_state="cancel" if condition == "cancelled" else "run",
                status="finishing" if condition == "finishing" else "claimed",
            )
        )
        await writer.execute(
            update(run).values(
                target_key="held",
                target_held_since=START,
                status="failed" if condition == "terminal_run" else "running",
            )
        )
        if condition == "retired_node":
            await writer.execute(
                update(node_instance).values(current_attempt_id=None, status="failed")
            )
    original = await state(database)
    case.clock.advance(121)
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "next-claim"})
    )

    second = await next_case.claim(database)
    saved = await state(database)
    replay = await next_case.claim(database)
    after_replay = await state(database)

    assert second.outcome is ClaimOutcome.GRANTED
    assert first.delivery is not None
    assert second.delivery is not None
    assert replay.delivery is not None
    assert second.delivery.system.mode == (
        "execute" if condition == "running" else "recover"
    )
    assert second.delivery.system.permitted_actions == ("observe", "retire")
    assert second.delivery.system.lease_epoch == 2
    assert second.delivery.system.grant_history.count == 2
    assert second.delivery.system.deadline == first.delivery.system.deadline
    assert second.delivery.encode() == replay.delivery.encode()
    assert after_replay == saved
    assert original["attempt"][0]["recovery_version"] == 1
    assert saved["attempt"][0]["recovery_version"] == 3
    assert [
        row["payload"]
        for row in saved["event"]
        if row["kind"] == "attempt.execution_unknown"
    ] == [
        {
            "version": 2,
            "domain": case.registration.execution_domain_id,
            "reason": "lease_expired",
            "actor": {"kind": "system", "component": "scheduler"},
            "evidence": None,
        }
    ]
    assert saved["attempt"][0]["started_at"] == original["attempt"][0]["started_at"]
    assert (
        saved["attempt"][0]["execution_domain_id"]
        == original["attempt"][0]["execution_domain_id"]
    )
    assert saved["attempt"][0]["recovery_required"] is True
    assert (
        saved["run"][0]["target_held_since"] == original["run"][0]["target_held_since"]
    )


@pytest.mark.asyncio
async def test_regrant_advances_version_after_committed_lease_invalidation(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    first = await case.claim(database)
    case.clock.advance(121)
    async with database.transaction() as writer:
        invalidated = await recover_expired_lease(
            await case.context(writer), case.binding.attempt_id
        )
    after_lease_loss = await state(database)
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "next-claim"})
    )

    second = await next_case.claim(database)
    saved = await state(database)

    assert first.outcome is ClaimOutcome.GRANTED
    assert invalidated
    assert after_lease_loss["attempt"][0]["lease_until"] is None
    assert after_lease_loss["attempt"][0]["recovery_version"] == 2
    assert second.outcome is ClaimOutcome.GRANTED
    assert second.delivery is not None
    assert second.delivery.system.lease_epoch == 2
    assert saved["attempt"][0]["recovery_version"] == 3
    assert saved["event"] == after_lease_loss["event"]
