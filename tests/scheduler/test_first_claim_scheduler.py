import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util.concurrency import await_

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.engine.admission_processing import AdmissionProcessor
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from kora.scheduler.loop import Scheduler, first_claim_scheduler
from tests.api.claim_support import BEARER
from tests.api.deployment_cases import GRAPH, preparer
from tests.api.deployment_cases import SELECTION as INSTALLATION
from tests.api.scheduler_claim_support import (
    PARAMS,
    scheduler_claim_ir,
    scheduler_claim_setup,
)
from tests.api.support import api_capture
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, fixed_randomness
from tests.engine.admission_processing_support import LIMITS as ADMISSION_LIMITS
from tests.engine.attempt_admission_support import SELECTION
from tests.engine.claim_support import state
from tests.engine.lock_support import lock_inventory
from tests.engine.test_http_trigger_admission import KEY, RUNS, VERSIONS

__all__ = ["api_capture", "preparer"]

BOUND = 10.0
RELEASE_CYCLES = 1_000


def scheduler(
    database: Database, clock: FakeClock, seed: int, *, page_size: int = 100
) -> Scheduler:
    return first_claim_scheduler(
        AdmissionProcessor(
            database,
            ADMISSION_LIMITS,
            SELECTION,
            INSTALLATION,
            clock.now,
            IdentifierGenerator(clock.now, fixed_randomness(seed)),
            1,
            None,
            timedelta(seconds=1),
            initial_root_only=True,
        ),
        page_size=page_size,
    )


def pending_page(statement: str, parameters: object) -> bool:
    return (
        statement.startswith("SELECT run.id")
        and "run.status =" in statement
        and isinstance(parameters, tuple)
        and "pending" in cast("tuple[object, ...]", parameters)
    )


@asynccontextmanager
async def held_sql(
    engine: AsyncEngine,
    matches: Callable[[str, object], bool],
) -> AsyncGenerator[tuple[asyncio.Event, asyncio.Event]]:
    reached, release = asyncio.Event(), asyncio.Event()

    def hold(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if not reached.is_set() and matches(statement, parameters):
            reached.set()
            await_(release.wait())

    event.listen(engine.sync_engine, "after_cursor_execute", hold)
    try:
        yield reached, release
    finally:
        release.set()
        event.remove(engine.sync_engine, "after_cursor_execute", hold)


async def stop_on_sql(
    owner: Scheduler,
    engine: AsyncEngine,
    matches: Callable[[str, object], bool],
) -> None:
    async with held_sql(engine, matches) as (reached, release):
        task = asyncio.create_task(owner.run())
        try:
            await asyncio.wait_for(reached.wait(), BOUND)
            owner.stop()
            assert not task.done()
        finally:
            owner.stop()
            release.set()
            await asyncio.wait_for(task, BOUND)


@pytest.mark.asyncio
async def test_trigger_to_first_claimable_attempt(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    clock = setup.clock
    first_loop = scheduler(database, clock, 41)
    restarted_loop = scheduler(database, clock, 42)
    ledger = ConnectionLedger(database)
    headers = {"Authorization": str(setup.user.request.authorization)}

    async with running_app(setup.app) as origin:
        async with httpx.AsyncClient(
            base_url=origin, trust_env=False, timeout=BOUND
        ) as api:
            published = await api.post(
                VERSIONS,
                json={"graph": GRAPH, "ir": scheduler_claim_ir(), "resources": []},
                headers=headers,
            )
            assert published.status_code == 201
            deployed = DeployResponse.model_validate(published.json())
            response = await api.post(
                RUNS,
                json={"params": PARAMS},
                headers=headers | {"Idempotency-Key": KEY},
            )
            assert response.status_code == 201
            accepted = AcceptedRun.model_validate(response.json())
        await first_loop.recover()
        await stop_on_sql(first_loop, database.reader_engine, pending_page)
        pending = await state(database)
        await restarted_loop.recover()
        await stop_on_sql(
            restarted_loop,
            database.writer_engine,
            lambda sql, _: sql.lower().startswith("insert into attempt "),
        )
        created = await state(database)
        async with KoraClient(origin, BEARER) as worker:
            await worker.register_worker(setup.registration)
            first = await worker.claim_worker(setup.registration.name, setup.request)
            before_replay = await state(database)
            replay = await worker.claim_worker(setup.registration.name, setup.request)
        after_replay = await state(database)

    assert pending["run"][0]["status"] == "pending"
    assert pending["attempt"] == pending["node_instance"] == []
    assert created["run"][0]["status"] == "running"
    assert len(created["attempt"]) == len(after_replay["attempt_dispatcher"]) == 1
    assert len(after_replay["attempt_first_claim_basis"]) == 1
    assert [row["kind"] for row in created["event"]].count("run.started") == 1
    assert created["attempt"][0]["status"] == "created"
    assert created["attempt"][0]["started_at"] is None
    assert first.claim is not None
    assert first.claim.attempt_id == created["attempt"][0]["id"]
    assert (
        first.claim.execution_context.graph_version_id
        == accepted.graph_version_id.removeprefix("gv_")
    )
    assert (
        first.claim.execution_context.execution_contract_digest
        == deployed.execution_contract_digest
    )
    assert first.claim.params == PARAMS
    assert replay == first
    assert before_replay == after_replay
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_scheduler_shutdown_releases_resources(database: Database) -> None:
    ledger = ConnectionLedger(database)
    clock = FakeClock()

    for _ in range(RELEASE_CYCLES):
        owner = scheduler(database, clock, 43)
        await stop_on_sql(
            owner,
            database.reader_engine,
            lambda sql, _: sql.startswith("SELECT attempt.id"),
        )
        assert ledger.outstanding == database.units_in_flight() == 0
        assert database.writer_is_free()
    locks = await lock_inventory(database)
    await database.close()

    assert ledger.checkouts >= RELEASE_CYCLES
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == 0
    assert locks == []
