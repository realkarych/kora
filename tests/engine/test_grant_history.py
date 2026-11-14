import asyncio
import time
from dataclasses import replace

import pytest
from sqlalchemy import func, insert, select, update

from kora.db.connection import Database
from kora.db.grant_history import GrantHistoryRejected, HistoryReadLimits, HistoryScope
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import attempt, attempt_dispatcher
from kora.engine.grant_history import (
    PreparedHistory,
    acquire_history,
    append_grant,
    prepare_history,
    save_history_batch,
)
from kora.engine.locks import TransitionLocks
from kora.protocols.history import GrantHistorySnapshot, chain_step, record_bytes
from kora.protocols.identity import GrantBinding, ProcessIdentity
from kora.protocols.identity_records import MAX_COUNT
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.engine.lock_support import wait_for_lock_waiter

LIMITS = HistoryReadLimits(65_536, 65_536, 65_536, 65_536, 100_000_000, 10_000_000)


async def seed(database: Database) -> HistoryScope:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await seeder.graph_version(graph)
        run = await seeder.run(namespace, graph, version)
        node = await seeder.node(run, "task")
        physical = await seeder.attempt(node)
        token = await seeder.token("a" * 64)
        await seeder.worker(token)
        domain = "d" * 256
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == physical)
            .values(execution_domain_id=domain)
        )
    return HistoryScope(namespace, run, node, physical, domain)


def grant(scope: HistoryScope, number: int, *, process: bool = False) -> GrantBinding:
    return GrantBinding(
        dispatcher_id=f"{number:026d}",
        worker_id="worker-1",
        incarnation_id="i" * 256,
        execution_domain_id=scope.execution_domain_id,
        lease_epoch=number * 3,
        process=ProcessIdentity(
            host_id="h" * 256,
            boot_id="b",
            pid_namespace=None,
            pid=123,
            start_time=456,
            leads_group=True,
        )
        if process
        else None,
    )


async def legacy(database: Database, scope: HistoryScope, numbers: range) -> None:
    async with database.transaction() as writer:
        rows: list[dict[str, object]] = []
        for number in numbers:
            binding = grant(scope, number)
            rows.append(
                {
                    "id": binding.dispatcher_id,
                    "attempt_id": scope.attempt_id,
                    "worker_id": binding.worker_id,
                    "incarnation_id": binding.incarnation_id,
                    "execution_domain_id": binding.execution_domain_id,
                    "lease_epoch": binding.lease_epoch,
                    "mode": "execute",
                    "granted_at": START,
                    "created_at": START,
                    "updated_at": START,
                }
            )
        await writer.execute(insert(attempt_dispatcher), rows)


async def acquired(database: Database, scope: HistoryScope) -> PreparedHistory:
    raw = await acquire_history(database, scope, LIMITS, deadline=time.monotonic() + 30)
    return prepare_history(raw)


async def save(
    database: Database, prepared: PreparedHistory
) -> GrantHistorySnapshot | None:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(prepared.scope.run_id)
        return await save_history_batch(writer, locks, prepared, LIMITS)


async def append(
    database: Database, scope: HistoryScope, number: int, *, process: bool = False
) -> GrantHistorySnapshot | None:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(scope.run_id)
        return await append_grant(
            writer,
            locks,
            scope,
            grant(scope, number, process=process),
            "execute",
            START,
            LIMITS,
        )


@pytest.mark.asyncio
async def test_grant_append_preserves_process_and_epoch_gaps(
    database: Database,
) -> None:
    scope = await seed(database)
    first = await append(database, scope, 1, process=True)
    assert first is not None
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == scope.attempt_id)
            .values(
                **dict(
                    zip(
                        ACK_FIELDS,
                        (first.upper_epoch, first.count, first.digest),
                        strict=True,
                    )
                )
            )
        )

    second = await append(database, scope, 5)

    assert second is not None
    assert (first.upper_epoch, first.count, second.upper_epoch, second.count) == (
        3,
        1,
        15,
        2,
    )
    assert first.digest == chain_step(
        first.seed(), record_bytes(grant(scope, 1, process=True))
    )
    assert second.digest == chain_step(first.digest, record_bytes(grant(scope, 5)))
    async with database.read_transaction() as reader:
        rows = (
            (
                await reader.execute(
                    select(attempt_dispatcher).order_by(
                        attempt_dispatcher.c.lease_epoch
                    )
                )
            )
            .mappings()
            .all()
        )
        ack = (
            await reader.execute(select(*(attempt.c[name] for name in ACK_FIELDS)))
        ).one()
    assert rows[0]["process"]["leads_group"] is True
    assert tuple(ack) == (None, None, None)


@pytest.mark.asyncio
async def test_grant_catchup_is_bounded_and_resumes_after_append(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 131))
    ledger = ConnectionLedger(database)

    first = await acquired(database, scope)
    released = ledger.outstanding
    await legacy(database, scope, range(131, 132))
    pending = await save(database, first)
    second = await acquired(database, scope)
    complete = await save(database, second)

    assert released == 0
    assert len(first.grants) == 128
    assert pending is None
    assert second.start == first.grants[-1][1]
    assert len(second.grants) == 3
    assert complete is not None
    assert (complete.count, complete.upper_epoch) == (131, 393)
    digest = complete.seed()
    for number in range(1, 132):
        digest = chain_step(digest, record_bytes(grant(scope, number)))
    assert complete.digest == digest


@pytest.mark.asyncio
async def test_legacy_append_invalidates_previously_complete_tip(
    database: Database,
) -> None:
    scope = await seed(database)
    first = await append(database, scope, 1)
    assert first is not None
    await legacy(database, scope, range(2, 3))

    pending = await append(database, scope, 3)
    async with database.read_transaction() as reader:
        tip = (
            await reader.execute(select(*(attempt.c[name] for name in TIP_FIELDS)))
        ).one()
    restored = await save(database, await acquired(database, scope))

    assert pending is None
    assert tuple(tip) == (None, None, None)
    assert restored is not None
    assert restored.count == 3


@pytest.mark.asyncio
async def test_empty_catchup_cannot_hide_concurrent_legacy_grant(
    database: Database,
) -> None:
    scope = await seed(database)
    prepared = await acquired(database, scope)

    await legacy(database, scope, range(1, 2))
    result = await save(database, prepared)

    assert result is None
    async with database.read_transaction() as reader:
        count = await reader.scalar(select(attempt.c.grant_history_count))
    assert count is None


@pytest.mark.asyncio
async def test_stale_catchup_does_not_overwrite_committed_prefix(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 4))
    prepared = await acquired(database, scope)

    completed = await save(database, prepared)
    stale = await save(database, prepared)

    assert completed is not None
    assert stale is None
    async with database.read_transaction() as reader:
        digest = await reader.scalar(select(attempt.c.grant_history_digest))
    assert digest == completed.digest


@pytest.mark.asyncio
async def test_history_append_rolls_back_with_authority_transaction(
    database: Database,
) -> None:
    scope = await seed(database)

    async def refused_authority() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(scope.run_id)
            await append_grant(
                writer, locks, scope, grant(scope, 1), "execute", START, LIMITS
            )
            raise ValueError("authority refused")

    with pytest.raises(ValueError, match="authority refused"):
        await refused_authority()

    async with database.read_transaction() as reader:
        rows = await reader.scalar(select(func.count()).select_from(attempt_dispatcher))
        count = await reader.scalar(select(attempt.c.grant_history_count))
    assert rows == 0
    assert count is None


@pytest.mark.asyncio
async def test_history_refuses_foreign_scope(database: Database) -> None:
    scope = await seed(database)
    foreign = replace(scope, execution_domain_id="foreign")
    ledger = ConnectionLedger(database)

    with pytest.raises(GrantHistoryRejected):
        await acquire_history(database, foreign, LIMITS, deadline=time.monotonic() + 30)

    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_history_count_overflow_refuses_append_and_catchup(
    database: Database,
) -> None:
    scope = await seed(database)
    await append(database, scope, 1)
    async with database.transaction() as writer:
        await writer.execute(update(attempt_dispatcher).values(prefix_count=MAX_COUNT))
        await writer.execute(update(attempt).values(grant_history_count=MAX_COUNT))

    with pytest.raises(GrantHistoryRejected):
        await append(database, scope, 2)
    await legacy(database, scope, range(2, 3))
    with pytest.raises(GrantHistoryRejected):
        await acquired(database, scope)

    async with database.read_transaction() as reader:
        counts = (
            (
                await reader.execute(
                    select(attempt_dispatcher.c.prefix_count).order_by(
                        attempt_dispatcher.c.lease_epoch
                    )
                )
            )
            .scalars()
            .all()
        )
        tip = await reader.scalar(select(attempt.c.grant_history_count))
    assert counts == [MAX_COUNT, None]
    assert tip == MAX_COUNT


@pytest.mark.asyncio
async def test_concurrent_history_appends_extend_the_committed_chain(
    database: Database,
) -> None:
    scope = await seed(database)
    first_written = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first_append() -> GrantHistorySnapshot | None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(scope.run_id)
            result = await append_grant(
                writer, locks, scope, grant(scope, 1), "execute", START, LIMITS
            )
            first_written.set()
            await release_first.wait()
            return result

    async def second_append() -> GrantHistorySnapshot | None:
        second_started.set()
        return await append(database, scope, 2)

    async with asyncio.timeout(10):
        async with asyncio.TaskGroup() as group:
            first = group.create_task(first_append())
            await first_written.wait()
            second = group.create_task(second_append())
            await second_started.wait()
            await wait_for_lock_waiter(database)
            assert not second.done()
            release_first.set()

    first_snapshot, second_snapshot = first.result(), second.result()
    assert first_snapshot is not None
    assert second_snapshot is not None
    assert first_snapshot.count == 1
    assert second_snapshot.count == 2
    assert second_snapshot.digest == chain_step(
        first_snapshot.digest, record_bytes(grant(scope, 2))
    )
    assert database.units_in_flight() == 0
