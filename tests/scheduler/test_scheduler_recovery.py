import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import event as sql_events
from sqlalchemy import select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import attempt, event, node_instance, run, wait
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import GRAPH, preparer
from tests.api.scheduler_claim_support import (
    PARAMS,
    scheduler_claim_ir,
    scheduler_claim_setup,
)
from tests.api.support import api_capture
from tests.client.support import running_app
from tests.engine.expiry_support import execution_case
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
