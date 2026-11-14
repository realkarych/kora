import asyncio
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.util.concurrency import await_

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import (
    attempt,
    attempt_claim_read_admission,
    attempt_dispatcher,
    run,
    worker,
)
from kora.db.worker_claim_poll import advance_claim_cursor
from kora.engine.claim_context import claim_worker
from kora.ir.json import JsonObject
from tests.api.claim_support import admit_expiry_run, poller
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.engine.claim_support import WORKER_BYTES, another_run, claim_case, state
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_poll_commits_cursor_with_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    poll = poller(database, AuthorityClock())

    delivery = await poll.poll(case.query, case.principal)
    empty = await poll.poll(
        case.query.model_copy(update={"claim_id": "full"}), case.principal
    )
    saved = await state(database)

    assert delivery is not None
    assert empty is None
    assert saved["worker"][0]["claim_cursor"] is None


@pytest.mark.asyncio
async def test_cursor_cas_loss_rolls_back_the_complete_tentative_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    poll = poller(database, AuthorityClock())
    observed, release = asyncio.Event(), asyncio.Event()

    def pause_run_read(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if "FROM run \n" in statement and not observed.is_set():
            observed.set()
            await_(release.wait())

    event.listen(
        database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
    )

    try:
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(poll.poll(case.query, case.principal))
            await observed.wait()
            async with database.transaction() as writer:
                await claim_worker(
                    writer, case.query, case.principal, WORKER_BYTES, lock=True
                )
                assert await advance_claim_cursor(
                    writer, case.query.worker_id, None, 0, None, case.clock.now()
                )
            before = await state(database)
            release.set()
            result = await pending
        after = await state(database)
    finally:
        event.remove(
            database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
        )

    assert result is None
    assert after == before
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_poll_traverses_only_32_candidates_then_wraps(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    for _ in range(31):
        await another_run(database, case)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                lease_until=case.clock.now() + timedelta(seconds=120)
            )
        )
    tail = await another_run(database, case)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == tail.binding.attempt_id)
            .values(lease_until=None)
        )
    poll = poller(database, AuthorityClock())

    first = await poll.poll(case.query, case.principal)
    traversed = await state(database)
    second = await poll.poll(case.query, case.principal)
    third = await poll.poll(
        case.query.model_copy(update={"claim_id": "wrap"}), case.principal
    )
    wrapped = await state(database)

    assert first is None
    assert traversed["worker"][0]["claim_cursor"] == traversed["attempt"][-2]["id"]
    assert len(traversed["attempt_dispatcher"]) == 0
    assert second is not None
    assert second.system.attempt_id == tail.binding.attempt_id
    assert third is None
    assert wrapped["worker"][0]["claim_cursor"] is None
    assert len(wrapped["attempt_dispatcher"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance", ["legacy", "history"])
async def test_poll_stops_after_terminal_maintenance_before_valid_tail(
    database: Database, evaluator: Evaluator, maintenance: str
) -> None:
    case = await claim_case(database, evaluator)
    tail = await another_run(database, case)
    await case.claim(database)
    clock = AuthorityClock()
    async with database.transaction() as writer:
        await writer.execute(update(worker).values(capacity=2))
        if maintenance == "legacy":
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.binding.attempt_id)
                .values(current_dispatcher_open=None)
            )
        else:
            await writer.execute(
                update(attempt_dispatcher).values(
                    prefix_count=None,
                    prefix_digest=None,
                    claim_id=None,
                    claim_digest=None,
                )
            )
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.binding.attempt_id)
                .values(**dict.fromkeys((*TIP_FIELDS, *ACK_FIELDS)))
            )
            clock.moment += timedelta(seconds=121)
    before = await state(database)
    poll = poller(database, clock)
    query = case.query.model_copy(update={"claim_id": "maintenance"})

    maintained = await poll.poll(query, case.principal)
    intermediate = await state(database)
    next_grant = await poll.poll(query, case.principal)

    assert maintained is None
    assert len(intermediate["attempt_dispatcher"]) == 1
    assert intermediate["event"] == before["event"]
    assert intermediate["attempt"][-1] == before["attempt"][-1]
    assert intermediate["worker"][0]["claim_cursor"] is None
    assert next_grant is not None
    assert next_grant.system.attempt_id == (
        tail.binding.attempt_id if maintenance == "legacy" else case.binding.attempt_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_source", [False, True])
async def test_poll_replays_concurrent_receipt_on_another_run_without_writes(
    database: Database, evaluator: Evaluator, bad_source: bool
) -> None:
    case = await claim_case(database, evaluator)
    other = await another_run(database, case)
    if bad_source:
        async with database.transaction() as writer:
            await writer.execute(
                delete(attempt_claim_read_admission).where(
                    attempt_claim_read_admission.c.attempt_id == case.binding.attempt_id
                )
            )
    poll = poller(database, AuthorityClock())
    observed, release = asyncio.Event(), asyncio.Event()

    def pause_run_read(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if "FROM run \n" in statement and not observed.is_set():
            observed.set()
            await_(release.wait())

    event.listen(
        database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
    )

    try:
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(poll.poll(case.query, case.principal))
            await observed.wait()
            async with database.transaction() as writer:
                await claim_worker(
                    writer, case.query, case.principal, WORKER_BYTES, lock=True
                )
                assert await advance_claim_cursor(
                    writer,
                    case.query.worker_id,
                    None,
                    0,
                    case.binding.attempt_id,
                    case.clock.now(),
                )
            winner = await poll.poll(case.query, case.principal)
            committed = await state(database)
            release.set()
            replayed = await pending
        after = await state(database)
    finally:
        event.remove(
            database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
        )

    assert winner is not None
    assert replayed is not None
    assert winner.system.attempt_id == other.binding.attempt_id
    assert replayed.encode() == winner.encode()
    assert committed == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_poll_finishes_expiry_observation_before_returning_without_a_grant(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1s")
    tail = await another_run(database, case)
    clock = AuthorityClock()
    clock.moment += timedelta(seconds=1)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == tail.binding.run.run_id)
            .values(started_at=clock.now())
        )
    poll = poller(database, clock)

    expired = await poll.poll(case.query, case.principal)
    after = await state(database)

    assert expired is None
    assert after["run"][0]["deadline_expired_at"] == clock.now()
    assert after["run"][-1]["deadline_expired_at"] is None
    assert after["attempt_dispatcher"] == []
    assert after["worker"][0]["claim_cursor"] is None
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["snapshot", "cursor"])
async def test_poll_cancellation_releases_sources_and_rolls_back_tentative_writes(
    database: Database, evaluator: Evaluator, stage: str
) -> None:
    case = await claim_case(database, evaluator)
    before = await state(database)
    poll = poller(database, AuthorityClock())
    observed, release = asyncio.Event(), asyncio.Event()
    engine = database.reader_engine if stage == "snapshot" else database.writer_engine

    def pause_native_operation(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        matches = (
            "FROM run \n" in statement
            if stage == "snapshot"
            else statement.startswith("UPDATE worker SET")
            and "claim_cursor=" in statement
        )
        if matches and not observed.is_set():
            observed.set()
            await_(release.wait())

    event.listen(engine.sync_engine, "after_cursor_execute", pause_native_operation)

    pending = asyncio.create_task(poll.poll(case.query, case.principal))
    try:
        async with asyncio.timeout(10):
            await observed.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        after = await state(database)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        event.remove(engine.sync_engine, "after_cursor_execute", pause_native_operation)

    assert before == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
