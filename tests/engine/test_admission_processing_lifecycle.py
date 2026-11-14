import asyncio
import sys
from dataclasses import replace
from types import FrameType

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.util.concurrency import await_

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.tables import attempt
from kora.engine.admission_processing import (
    AdmissionProcessingFailed,
    AdmissionProcessor,
)
from kora.engine.attempt_admission import PreparedAttemptAdmission
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger, WriterCutoffLoop
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.lock_support import lock_inventory

__all__ = ["api_capture", "preparer"]


def refuse_claim_handoff(
    owner: AdmissionProcessor, node_id: str, cancel: bool, phase: str = "claim"
) -> None:
    loop = WriterCutoffLoop()

    async def exercise() -> None:
        database = Database(owner.database.settings)
        local = replace(owner, database=database)
        ledger = ConnectionLedger(database)
        deadline = loop.time() + 10.0
        observed = False
        prepared = False
        previous = sys.getprofile()

        def interrupt() -> None:
            nonlocal observed
            assert not observed
            assert (
                database.units_in_flight()
                == ledger.outstanding
                == int(phase != "claim")
            )
            observed = True
            if cancel:
                current = asyncio.current_task()
                assert current is not None
                current.cancel()
            else:
                loop.advance_to(deadline)

        def observe(frame: FrameType, event: str, argument: object) -> None:
            nonlocal prepared
            if (
                event == "return"
                and frame.f_code is PreparedAttemptAdmission.__init__.__code__
            ):
                prepared = True
                if phase == "claim":
                    interrupt()

        def written(
            connection: Connection,
            cursor: DBAPICursor,
            statement: str,
            *arguments: object,
        ) -> None:
            if phase == "write" and statement.lower().startswith(
                "insert into attempt "
            ):
                interrupt()

        def committing(connection: Connection) -> None:
            if phase == "commit" and prepared:
                interrupt()

        engine = database.writer_engine.sync_engine
        try:
            sys.setprofile(observe)
            event.listen(engine, "after_cursor_execute", written)
            event.listen(engine, "commit", committing)
            with pytest.raises(
                asyncio.CancelledError if cancel else AdmissionProcessingFailed
            ) as failure:
                await local.process(node_id, periodic_recheck=True, deadline=deadline)
        finally:
            sys.setprofile(previous)
            event.remove(engine, "after_cursor_execute", written)
            event.remove(engine, "commit", committing)
            await database.close()

        assert observed
        assert failure.value.__cause__ is failure.value.__context__ is None
        assert ledger.checkouts == (1 if phase == "claim" else 2)
        assert ledger.opened == ledger.closed
        assert ledger.outstanding == database.units_in_flight() == 0

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        runner.run(exercise())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_admission_processing_refuses_late_claim_handoff(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture, cancel: bool
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    before = await case.promotion.snapshot(database)

    await asyncio.to_thread(refuse_claim_handoff, owner, case.node_id, cancel)
    after = await case.promotion.snapshot(database)

    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["write", "commit"])
async def test_admission_processing_deadline_covers_writer_and_settlement(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture, phase: str
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    before = await case.promotion.snapshot(database)

    await asyncio.to_thread(refuse_claim_handoff, owner, case.node_id, False, phase)
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as rows:
            attempts = rows.mappings().all()

    if phase == "write":
        assert after == before
        assert attempts == []
    else:
        assert len(attempts) == 1
        assert attempts[0]["status"] == "created"
        candidate = next(node for node in after[1] if node["id"] == case.node_id)
        assert candidate["status"] == "queued"
        assert candidate["current_attempt_id"] == attempts[0]["id"]
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_admission_processing_rolls_back_cancelled_partial_write(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)
    entered, release = asyncio.Event(), asyncio.Event()

    def observe(
        connection: Connection, cursor: DBAPICursor, statement: str, *arguments: object
    ) -> None:
        if (
            statement.lower().startswith("insert into attempt ")
            and not entered.is_set()
        ):
            entered.set()
            await_(release.wait())

    engine = database.writer_engine.sync_engine
    event.listen(engine, "after_cursor_execute", observe)
    pending = asyncio.create_task(process(owner, case.node_id))
    try:
        await asyncio.wait_for(entered.wait(), 10.0)

        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await asyncio.wait_for(pending, 10.0)
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", observe)
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        saved = await reader.scalar(select(attempt.c.id))

    assert before == after
    assert saved is None
    assert failure.value.__cause__ is failure.value.__context__ is None
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
