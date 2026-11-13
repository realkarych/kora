import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import select, update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    node_instance,
    run,
    worker,
)
from kora.engine.attempt_claims import ClaimOutcome
from kora.ir.json import JsonObject
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.db.seed import START
from tests.engine.claim_support import (
    UNCERTAINTY,
    another_run,
    claim_case,
    state,
    wait_for_claim_waiter,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_zero_timeout_returns_learnable_expired_first_grant(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    node = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
    node["limits"] = {"timeout": "0ms"}
    case = await claim_case(database, evaluator, ir)

    first = await case.claim(database)
    saved = await state(database)
    replay = await case.claim(database)

    assert first.outcome is ClaimOutcome.GRANTED
    assert first.delivery is not None
    assert replay.delivery is not None
    assert replay.delivery.encode() == first.delivery.encode()
    assert first.delivery.system.permitted_actions == ("seal_request",)
    deadline = first.delivery.system.deadline.value
    assert deadline is not None
    assert deadline.at == "2026-10-20T12:00:00.000000Z"
    assert deadline.clock_uncertainty_us == str(
        UNCERTAINTY // timedelta(microseconds=1)
    )
    assert saved["attempt"][0]["deadline_expired_at"] == START
    assert saved["attempt"][0]["recovery_required"] is True
    assert saved["attempt"][0]["desired_state"] == "cancel"
    assert saved["run"][0]["needs_scheduling"] is True
    assert saved["run"][0]["deadline_expired_at"] is None


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
async def test_parent_expiry_uses_time_after_worker_guard_wait(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    runtime = cast(JsonObject, ir["runtime"])
    runtime["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    other = await another_run(database, case)
    prepared = await other.prepare(database)
    before = await state(database)
    guarded, release = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as writer:
            await case.context(writer)
            await writer.execute(
                select(worker.c.id)
                .where(worker.c.id == case.query.worker_id)
                .with_for_update()
            )
            guarded.set()
            await release.wait()

    async def contender() -> ClaimOutcome:
        async with database.transaction() as writer:
            return (await other.apply(writer, prepared)).outcome

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        held = tasks.create_task(holder())
        await guarded.wait()
        pending = tasks.create_task(contender())
        await wait_for_claim_waiter(database)
        case.clock.advance(1)
        release.set()
        await held
        outcome = await pending
    after = await state(database)

    assert outcome is ClaimOutcome.EXPIRE
    assert before == after
    assert database.units_in_flight() == 0
