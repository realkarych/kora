from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.run_event_position_v1 import MAX_POSITION, RunEventPositionRejected
from kora.db.tables import event, run_event_position, run_event_position_state
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.cancellation import CancellationOutcome
from kora.engine.expiry import expire_run
from kora.engine.expiry_context import ExpiryState
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.start_acceptance import AcceptanceOutcome, StartAcceptance
from kora.protocols.runtime import RuntimeStatus
from tests.api.claim_support import admit_expiry_run
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.cancellation_support import pending_cancellation
from tests.engine.claim_support import claim_case
from tests.engine.graph_output_cases import child
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.engine.lock_support import DeliberateRollback, lock_inventory
from tests.engine.promotion_support import promotion_case
from tests.engine.start_cases import deploy, start
from tests.engine.test_start_acceptance import accepted, promote
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@dataclass(frozen=True)
class PositionSnapshot:
    high_water: int
    positions: tuple[tuple[int, str, str], ...]


async def snapshot(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> PositionSnapshot:
    state = run_event_position_state.c
    positioned = run_event_position.c
    high_water = await connection.scalar(
        select(state.high_water).where(
            state.run_id == run_id, state.namespace_id == namespace_id, state.ready
        )
    )
    assert type(high_water) is int
    with await connection.execute(
        select(positioned.position, event.c.id, event.c.kind)
        .join(event, event.c.id == positioned.event_id)
        .where(
            positioned.run_id == run_id,
            event.c.run_id == run_id,
            event.c.namespace_id == namespace_id,
        )
        .order_by(positioned.position)
    ) as rows:
        positions = tuple(cast(tuple[int, str, str], tuple(row)) for row in rows)
    return PositionSnapshot(high_water, positions)


async def saved_positions(
    database: Database, namespace_id: str, run_id: str
) -> PositionSnapshot:
    async with database.read_snapshot() as reader:
        return await snapshot(reader, namespace_id, run_id)


@pytest.mark.asyncio
async def test_lower_event_id_appends_after_creation_position_and_start_replay(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    started = accepted(await start(database, case, seconds=30))
    created = await saved_positions(database, case.namespace_id, started.run_id)

    status = await promote(database, case, started.run_id)
    replayed = await start(database, case, seconds=60)
    positioned = await saved_positions(database, case.namespace_id, started.run_id)

    assert status == "running"
    assert replayed == StartAcceptance(AcceptanceOutcome.REPLAYED, started)
    assert created.high_water == 1
    assert positioned.high_water == 2
    creation, promotion = positioned.positions
    assert created.positions == (creation,)
    creation_position, creation_id, creation_kind = creation
    promotion_position, promotion_id, promotion_kind = promotion
    assert (creation_position, creation_kind) == (1, "run.created")
    assert (promotion_position, promotion_kind) == (2, "run.started")
    assert promotion_id < creation_id


@pytest.mark.asyncio
async def test_rolled_back_promotion_leaves_its_position_available(
    database: Database,
) -> None:
    case = await promotion_case(database)
    before = await case.snapshot(database)
    empty = await saved_positions(database, case.scope.namespace_id, case.run_id)
    ledger = ConnectionLedger(database)
    observations: list[tuple[PromotionOutcome, PositionSnapshot]] = []

    async def roll_back() -> None:
        async with database.transaction() as writer:
            rolled_back = await case.apply(writer, await case.locks(writer))
            tentative = await snapshot(writer, case.scope.namespace_id, case.run_id)
            observations.append((rolled_back, tentative))
            raise DeliberateRollback()

    with pytest.raises(DeliberateRollback):
        await roll_back()
    after_rollback = await case.snapshot(database)
    positions_after_rollback = await saved_positions(
        database, case.scope.namespace_id, case.run_id
    )
    committed = await case.promote(database)
    replayed = await case.promote(database)
    final = await saved_positions(database, case.scope.namespace_id, case.run_id)

    [(rolled_back, tentative)] = observations
    assert rolled_back is committed is PromotionOutcome.APPLIED
    assert replayed is PromotionOutcome.UNCHANGED
    assert after_rollback == before
    assert positions_after_rollback == empty == PositionSnapshot(0, ())
    assert tentative.high_water == final.high_water == 1
    [(rolled_back_position, rolled_back_id, rolled_back_kind)] = tentative.positions
    [(committed_position, committed_id, committed_kind)] = final.positions
    assert rolled_back_position == committed_position == 1
    assert rolled_back_kind == committed_kind == "run.started"
    assert rolled_back_id != committed_id
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "different_namespace", "exhausted"])
async def test_promotion_refuses_invalid_position_state_without_an_event(
    database: Database, state: Literal["missing", "different_namespace", "exhausted"]
) -> None:
    case = await promotion_case(database)
    positions = run_event_position_state
    only_run = positions.c.run_id == case.run_id
    async with database.transaction() as writer:
        if state == "missing":
            await writer.execute(delete(positions).where(only_run))
        elif state == "different_namespace":
            namespace_id = await Seeder(writer).namespace("different-position-owner")
            await writer.execute(
                update(positions).where(only_run).values(namespace_id=namespace_id)
            )
        else:
            await writer.execute(
                update(positions).where(only_run).values(high_water=MAX_POSITION)
            )
    before = await case.snapshot(database)

    with pytest.raises(RunEventPositionRejected):
        await case.promote(database)
    after = await case.snapshot(database)
    async with database.read_snapshot() as reader:
        saved_id = await reader.scalar(
            select(run_event_position.c.event_id).where(
                run_event_position.c.run_id == case.run_id
            )
        )

    assert after == before
    assert saved_id is None
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_cancellation_position_survives_exact_replay(database: Database) -> None:
    case = await pending_cancellation(database)

    applied = await case.apply()
    after = await saved_positions(database, case.namespace_id, case.run_id)
    repeated = await case.apply()
    replay = await saved_positions(database, case.namespace_id, case.run_id)

    assert applied.outcome is CancellationOutcome.APPLIED
    assert repeated.outcome is CancellationOutcome.UNCHANGED
    assert after == replay
    assert replay.high_water == 1
    [(position, _, kind)] = replay.positions
    assert (position, kind) == (1, "run.cancelled")


@pytest.mark.asyncio
async def test_claim_heartbeat_and_expiry_preserve_consecutive_positions_on_replay(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    child(ir, "runtime")["limits"] = {"timeout": "2s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "2s")
    parent = case.binding.run
    ledger = ConnectionLedger(database)

    async def expire() -> ExpiryState:
        async with database.transaction() as writer:
            context = await case.context(writer)
            await context.locks.acquire_scopes(context.capacity_scopes(None))
            return await expire_run(context)

    claim = await case.claim(database)
    assert claim.delivery is not None
    claim_replay = await case.claim(database)
    claimed = await saved_positions(database, parent.namespace_id, parent.run_id)
    clock = AuthorityClock(case.clock.now() + timedelta(seconds=1))
    observation = RuntimeStatus(
        state="lost", exit_code=None, phase="execution", observed_at=clock.now()
    )
    body = heartbeat_body(claim.delivery.system).model_copy(
        update={"observation": observation}
    )
    first_heartbeat = await heartbeat(database, case, clock, body)
    after_heartbeat = await saved_positions(
        database, parent.namespace_id, parent.run_id
    )
    heartbeat_replay = await heartbeat(database, case, clock, body)
    case.clock.advance(2)
    expired = await expire()
    expiry_replay = await expire()
    final = await saved_positions(database, parent.namespace_id, parent.run_id)
    async with database.read_snapshot() as reader:
        worker_position = await reader.scalar(
            select(run_event_position.c.position)
            .join(event, event.c.id == run_event_position.c.event_id)
            .where(event.c.kind == "worker.registered")
        )

    assert claim_replay.outcome is ClaimOutcome.REPLAYED
    assert claim_replay.delivery == claim.delivery
    assert first_heartbeat == heartbeat_replay
    assert expired is expiry_replay is ExpiryState.EXPIRED
    assert claimed.high_water == 1
    assert after_heartbeat.high_water == 3
    assert final.high_water == 5
    assert claimed.positions == final.positions[:1]
    assert after_heartbeat.positions == final.positions[:3]
    assert [(position, kind) for position, _, kind in final.positions] == [
        (1, "attempt.claimed"),
        (2, "attempt.execution_unknown"),
        (3, "attempt.recovery_required"),
        (4, "node.cancelled"),
        (5, "run.failed"),
    ]
    assert worker_position is None
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
