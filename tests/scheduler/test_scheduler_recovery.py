import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy import event as sql_events
from sqlalchemy import func, insert, select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import attempt, event, node_instance, run, wait
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.staging_maintenance import StagingMaintenance
from kora.models.runs import AcceptedRun
from kora.scheduler.discovery import Discovery
from kora.scheduler.tick import Tick
from tests.api.deployment_cases import GRAPH, preparer
from tests.api.scheduler_claim_support import (
    PARAMS,
    node_id,
    scheduler_claim_ir,
    scheduler_claim_setup,
)
from tests.api.support import api_capture
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.admission_processing_support import processor
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.expiry_support import execution_case
from tests.engine.promotion_support import append_pending
from tests.engine.test_http_trigger_admission import RUNS, VERSIONS
from tests.scheduler.test_first_claim_scheduler import BOUND, scheduler

__all__ = ["api_capture", "preparer"]


async def pending_runs(origin: str, authorization: str) -> list[str]:
    headers = {"Authorization": authorization}
    async with httpx.AsyncClient(
        base_url=origin, trust_env=False, timeout=BOUND
    ) as client:
        deployed = await client.post(
            VERSIONS,
            json={"graph": GRAPH, "ir": scheduler_claim_ir(), "resources": []},
            headers=headers,
        )
        assert deployed.status_code == 201
        accepted: list[str] = []
        for index in range(2):
            result = await client.post(
                RUNS,
                json={"params": PARAMS},
                headers=headers | {"Idempotency-Key": f"scheduler-late-{index}"},
            )
            assert result.status_code == 201
            accepted.append(
                AcceptedRun.model_validate(result.json()).run_id.removeprefix("run_")
            )
    return accepted


async def admitted_waits(
    database: Database, run_ids: list[str]
) -> list[tuple[str, datetime]]:
    query = (
        select(node_instance.c.id, wait.c.wake_at)
        .join(wait, wait.c.node_instance_id == node_instance.c.id)
        .join(event, event.c.node_instance_id == node_instance.c.id)
        .where(
            node_instance.c.run_id.in_(run_ids),
            wait.c.kind == "admission",
            wait.c.status == "open",
            event.c.kind == "node.waiting",
        )
        .order_by(event.c.id)
    )
    async with database.read_transaction() as reader:
        with await reader.execute(query) as result:
            return [(row.id, row.wake_at) for row in result]


@pytest.mark.asyncio
async def test_scheduler_rotates_past_future_wait_and_recovers_terminal_parent(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    orphan = await execution_case(database)
    setup.clock.advance(60)
    cutoff = setup.clock.now()
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == orphan.promotion.run_id)
            .values(status="cancelled", needs_scheduling=False)
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == orphan.attempt_id)
            .values(lease_until=cutoff)
        )
    owner = scheduler(database, setup.clock, 43, page_size=1)
    async with running_app(setup.app) as origin:
        accepted = await pending_runs(origin, str(setup.user.request.authorization))

    await owner.recover()
    recovered = await orphan.snapshot(database)
    settled_waits = asyncio.Event()
    pending_inserts = len(accepted)

    def observe(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal pending_inserts
        if statement.lower().startswith("insert into wait "):
            pending_inserts -= 1
            if pending_inserts == 0:
                settled_waits.set()

    writer_engine = database.writer_engine.sync_engine
    sql_events.listen(writer_engine, "after_cursor_execute", observe)
    task = asyncio.create_task(owner.run())
    try:
        await asyncio.wait_for(settled_waits.wait(), BOUND)
    except TimeoutError as error:
        owner.stop()
        await task
        raise AssertionError(
            (pending_inserts, await admitted_waits(database, accepted))
        ) from error
    finally:
        owner.stop()
        await task
        sql_events.remove(writer_engine, "after_cursor_execute", observe)
    observed = await admitted_waits(database, accepted)
    async with database.read_transaction() as reader:
        unexpected = await reader.scalar(
            select(attempt.c.id).where(attempt.c.id != orphan.attempt_id)
        )

    assert len(observed) == len(accepted)
    assert recovered["recovery_required"] is True
    assert recovered["execution_domain_id"] == orphan.domain_id
    assert all(wake == cutoff + timedelta(seconds=1) for _, wake in observed)
    assert [node for node, _ in observed] == sorted(node for node, _ in observed)
    assert unexpected is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_scheduler_tick_continues_open_admission_sweep_without_hot_wrap(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    roots = [case.node_id]
    run_ids = [case.promotion.run_id]
    for _ in range(2):
        added = await append_pending(database, case.promotion)
        assert await added.promote(database) is PromotionOutcome.APPLIED
        roots.append(await node_id(database, added.run_id, "agent"))
        run_ids.append(added.run_id)
    admission = replace(
        processor(database, case), runtime_limit=None, initial_root_only=True
    )
    clock, identifiers = admission.clock, admission.identifiers
    wake = clock() + timedelta(seconds=1)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await writer.execute(
            update(run).where(run.c.id.in_(run_ids)).values(needs_scheduling=False)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id.in_(roots))
            .values(status="waiting")
        )
        await writer.execute(
            update(node_instance)
            .where(
                node_instance.c.run_id.in_(run_ids),
                node_instance.c.id.not_in(roots),
            )
            .values(status="cancelled")
        )
        for root in roots:
            await writer.execute(
                insert(wait).values(
                    id=seed.new_id(),
                    node_instance_id=root,
                    kind="admission",
                    status="open",
                    wake_at=wake,
                    **seed.stamps(),
                )
            )
    limits = admission.limits
    source = limits.preparation.snapshot
    graph_limits = GraphSnapshotLimits(
        source.graph, source.graph_native_bytes, source.owner_text_bytes
    )
    tick = Tick(
        Discovery(database, 2),
        clock,
        BOUND,
        PendingPromotion(database, graph_limits, limits.promotion, clock, identifiers),
        admission,
        StagingMaintenance(
            database,
            limits.promotion,
            limits.sources.attempt_row,
            clock,
            identifiers,
            admission.selection.runtime,
        ),
    )
    ledger = ConnectionLedger(database)
    event_count = select(func.count()).select_from(event)
    async with database.read_transaction() as reader:
        initial_events = await reader.scalar(event_count)
    stopped = asyncio.Event()
    processed: list[str] = []

    def observe(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        *arguments: object,
    ) -> None:
        if statement.startswith("SELECT id, kind, attempt_id "):
            assert isinstance(parameters, tuple)
            node = cast("tuple[object, ...]", parameters)[0]
            assert isinstance(node, str)
            processed.append(node)

    reader_engine = database.reader_engine.sync_engine
    sql_events.listen(reader_engine, "after_cursor_execute", observe)
    try:
        continuation = [await tick(stopped.is_set), await tick(stopped.is_set)]
    finally:
        sql_events.remove(reader_engine, "after_cursor_execute", observe)
    selected = "engine.admission_discovery.selected"
    completed = "engine.admission_processing.completed"
    observed = [
        (
            record["event"],
            record.get("count"),
            record.get("wrapped"),
            record.get("outcome"),
        )
        for record in api_capture.finish()
        if record["event"] in (selected, completed, "scheduler.transition")
    ]
    saved = (
        select(wait.c.node_instance_id, wait.c.wake_at, node_instance.c.status)
        .join(node_instance, node_instance.c.id == wait.c.node_instance_id)
        .where(wait.c.kind == "admission", wait.c.status == "open")
        .order_by(wait.c.node_instance_id)
    )
    async with database.read_transaction() as reader:
        with await reader.execute(saved) as result:
            waits = result.all()
        reservation = await reader.scalar(select(attempt.c.id).limit(1))
        final_events = await reader.scalar(event_count)

    assert continuation == [True, False]
    assert observed == [
        (selected, 2, True, None),
        (completed, None, None, "refused"),
        (completed, None, None, "refused"),
        (selected, 1, False, None),
        (completed, None, None, "refused"),
    ]
    assert processed == sorted(roots)
    assert waits == [(root, wake, "waiting") for root in sorted(roots)]
    assert reservation is None
    assert initial_events == final_events
    assert ledger.outstanding == database.units_in_flight() == 0
