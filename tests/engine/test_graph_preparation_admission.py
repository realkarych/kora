import asyncio
from dataclasses import replace
from traceback import format_exception
from typing import Literal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.tables import (
    graph,
    graph_deployment_preparation,
    graph_deployment_preparation_capacity,
    graph_deployment_stage,
    graph_version,
)
from kora.engine.graph_preparation_admission import (
    GraphPreparationAdmissionFailed,
    GraphPreparationAdmissionOutcome,
    admit_graph_preparation,
)
from kora.engine.locks import LockError, PreparationCapacityUnavailable, TransitionLocks
from kora.protocols.identity_records import MAX_COUNT
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)
from tests.engine.test_graph_publication_target import publication_scopes
from tests.engine.test_preparation_capacity_lock import CAPACITY, insert_ledger
from tests.engine.test_storage_address_lock import DIGEST as ADDRESS_DIGEST
from tests.engine.test_storage_address_lock import STORE, seed_address

__all__ = ["kora_capture"]

GRAPH_NAME: str = "private_admission_subject"
PREPARATION = graph_deployment_preparation
CAPACITY_TABLE = graph_deployment_preparation_capacity


async def seed_binding(
    database: Database, namespace_name: str, *, bound: bool = False
) -> tuple[GraphPreparationBinding, str]:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace(namespace_name)
        selected = (
            await seeder.graph(namespace_id, GRAPH_NAME) if bound else seeder.new_id()
        )
        if bound:
            await seeder.graph_version(selected)
        return (
            GraphPreparationBinding(
                seeder.new_id(),
                namespace_id,
                GRAPH_NAME,
                selected if bound else None,
                DIGEST,
            ),
            selected,
        )


async def admission_locks(
    writer: AsyncConnection, binding: GraphPreparationBinding, selected: str
) -> TransitionLocks:
    locks = TransitionLocks(writer)
    await locks.acquire_scopes((CAPACITY, *publication_scopes(binding, selected)))
    return locks


async def reserve(
    database: Database, binding: GraphPreparationBinding, selected: str, limit: int
) -> GraphPreparationAdmissionOutcome:
    async with database.transaction() as writer:
        locks = await admission_locks(writer, binding, selected)
        return await admit_graph_preparation(writer, locks, binding, selected, limit)


async def durable_state(database: Database) -> tuple[object, list[tuple[object, ...]]]:
    async with database.read_transaction() as reader:
        occupied: object = await reader.scalar(select(CAPACITY_TABLE.c.occupied))
        preparations = (
            await reader.execute(select(PREPARATION).order_by(PREPARATION.c.id))
        ).all()
        return occupied, [tuple(row) for row in preparations]


def saved_preparation(
    binding: GraphPreparationBinding, state: str = "open"
) -> tuple[object, ...]:
    return (
        binding.preparation_id,
        binding.namespace_id,
        binding.graph_name,
        binding.initial_graph_id,
        binding.deployment_digest,
        state,
        None,
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_graph_preparation_admits_one_exact_request_and_charge(
    database: Database, bound: bool
) -> None:
    binding, selected = await seed_binding(database, "original", bound=bound)
    await insert_ledger(database, 0)
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = await admission_locks(writer, binding, selected)
        before = ledger.checkouts
        with executed_queries(writer) as queries:
            outcome = await admit_graph_preparation(writer, locks, binding, selected, 1)
        after = ledger.checkouts
    durable = await durable_state(database)
    async with database.read_transaction() as reader:
        graph_count = await reader.scalar(select(func.count()).select_from(graph))
        version_count = await reader.scalar(
            select(func.count()).select_from(graph_version)
        )
        stage_count = await reader.scalar(
            select(func.count()).select_from(graph_deployment_stage)
        )

    assert outcome is GraphPreparationAdmissionOutcome.PREPARED
    assert durable == (1, [saved_preparation(binding)])
    assert graph_count == version_count == int(bound)
    assert stage_count == 0
    assert before == after
    assert len(queries) == 4
    assert "WHERE graph_deployment_preparation_capacity.id =" in queries[0][0]
    assert "WHERE graph.namespace_id =" in queries[1][0]
    assert queries[2][0].startswith("INSERT INTO graph_deployment_preparation ")
    assert queries[2][0].endswith("RETURNING 1")
    assert queries[3][0].startswith("UPDATE graph_deployment_preparation_capacity ")
    assert "WHERE graph_deployment_preparation_capacity.id =" in queries[3][0]
    assert "AND graph_deployment_preparation_capacity.occupied =" in queries[3][0]
    assert queries[3][1] == (1, 1, 0)
    assert queries[3][0].endswith("RETURNING 1")
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_preparation_full_path_validates_scopes_before_reading(
    database: Database,
) -> None:
    binding, selected = await seed_binding(database, "original")
    other, other_selected = await seed_binding(database, "other")
    await insert_ledger(database, 0)
    await seed_address(database)
    assert (
        await reserve(database, binding, selected, 1)
        is GraphPreparationAdmissionOutcome.PREPARED
    )
    async with database.transaction() as writer:
        await writer.execute(
            PREPARATION.update()
            .where(PREPARATION.c.id == binding.preparation_id)
            .values(state="closed_without_publication")
        )

    async with database.transaction() as writer:
        locks = await admission_locks(writer, other, other_selected)
        with executed_queries(writer) as refused:
            with pytest.raises(LockError, match="required transaction locks"):
                await admit_graph_preparation(writer, locks, binding, other_selected, 1)
            with pytest.raises(LockError, match="required transaction locks"):
                await admit_graph_preparation(writer, locks, other, selected, 1)
            for invalid in (0, True, MAX_COUNT + 1):
                with pytest.raises(ValueError, match="capacity limit"):
                    await admit_graph_preparation(
                        writer, locks, other, other_selected, invalid
                    )
        with executed_queries(writer) as full_queries:
            full = await admit_graph_preparation(
                writer, locks, other, other_selected, 1
            )
        await locks.acquire_existing_storage_address(STORE, ADDRESS_DIGEST)
        with executed_queries(writer) as post_address:
            with pytest.raises(LockError, match="precedes storage addresses"):
                await admit_graph_preparation(writer, locks, other, other_selected, 1)
    async with database.transaction() as writer:
        wrong_locks = TransitionLocks(writer)
        await wrong_locks.acquire_scopes(publication_scopes(other, other_selected))
        with executed_queries(writer) as missing_capacity:
            with pytest.raises(LockError, match="required transaction locks"):
                await admit_graph_preparation(
                    writer, wrong_locks, other, other_selected, 1
                )
    durable = await durable_state(database)

    assert refused == post_address == missing_capacity == []
    assert full is GraphPreparationAdmissionOutcome.FULL
    assert len(full_queries) == 1
    assert "WHERE graph_deployment_preparation_capacity.id =" in full_queries[0][0]
    assert durable == (1, [saved_preparation(binding, "closed_without_publication")])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["name_present", "bound_absent", "bound_replaced"])
async def test_graph_preparation_refuses_an_incompatible_current_target(
    database: Database, mode: Literal["name_present", "bound_absent", "bound_replaced"]
) -> None:
    binding, selected = await seed_binding(database, "original")
    await insert_ledger(database, 0)
    if mode != "bound_absent":
        async with database.transaction() as writer:
            seeder = Seeder(writer)
            current = await seeder.graph(binding.namespace_id, GRAPH_NAME)
            await seeder.graph_version(current)
    if mode != "name_present":
        binding = replace(binding, initial_graph_id=selected)

    async with database.transaction() as writer:
        locks = await admission_locks(writer, binding, selected)
        with executed_queries(writer) as queries:
            outcome = await admit_graph_preparation(writer, locks, binding, selected, 1)
    durable = await durable_state(database)

    assert outcome is GraphPreparationAdmissionOutcome.TARGET_CHANGED
    assert len(queries) == 2
    assert all(statement.startswith("SELECT ") for statement, _ in queries)
    assert durable == (0, [])


@pytest.mark.asyncio
async def test_graph_preparation_does_not_initialize_or_double_charge(
    database: Database, kora_capture: Capture
) -> None:
    binding, selected = await seed_binding(database, "original")
    other, other_selected = await seed_binding(database, "other")
    duplicate = replace(other, preparation_id=binding.preparation_id)

    with pytest.raises(PreparationCapacityUnavailable):
        await reserve(database, binding, selected, 2)
    missing = await durable_state(database)
    await insert_ledger(database, 0)
    original = await reserve(database, binding, selected, 2)
    with pytest.raises(GraphPreparationAdmissionFailed) as refused:
        await reserve(database, duplicate, other_selected, 2)
    durable = await durable_state(database)
    next_admission = await reserve(database, other, other_selected, 2)
    after = await durable_state(database)
    diagnostics = [
        record
        for record in kora_capture.finish()
        if record["event"] == "db.transaction.failed"
    ]

    assert missing == (None, [])
    assert original is GraphPreparationAdmissionOutcome.PREPARED
    assert durable == (1, [saved_preparation(binding)])
    assert next_admission is GraphPreparationAdmissionOutcome.PREPARED
    assert after == (2, [saved_preparation(binding), saved_preparation(other)])
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert len(diagnostics) == 2
    assert (
        repr(refused.value)
        == "GraphPreparationAdmissionFailed('graph preparation admission failed')"
    )
    for private in (binding.preparation_id, other.namespace_id, GRAPH_NAME, DIGEST):
        assert private not in repr(diagnostics)
        assert private not in "".join(format_exception(refused.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_winner", [False, True])
async def test_graph_preparation_orders_the_last_slot_across_namespaces(
    database: Database, rollback_winner: bool
) -> None:
    first, first_id = await seed_binding(database, "first")
    second, second_id = await seed_binding(database, "second")
    await insert_ledger(database, 0)
    ledger = ConnectionLedger(database)
    waiter: asyncio.Task[GraphPreparationAdmissionOutcome] | None = None

    async def hold_last_slot() -> None:
        nonlocal waiter
        async with database.transaction() as writer:
            locks = await admission_locks(writer, first, first_id)
            assert (
                await admit_graph_preparation(writer, locks, first, first_id, 1)
                is GraphPreparationAdmissionOutcome.PREPARED
            )
            waiter = asyncio.create_task(reserve(database, second, second_id, 1))
            await wait_for_lock_waiter(database)
            assert not waiter.done()
            if database.dialect is Dialect.POSTGRESQL:
                assert (*CAPACITY.key, False) in await lock_inventory(database)
            else:
                assert ledger.outstanding == 1
            if rollback_winner:
                raise DeliberateRollback()

    second_outcome: GraphPreparationAdmissionOutcome | None = None
    try:
        if rollback_winner:
            with pytest.raises(DeliberateRollback):
                await hold_last_slot()
        else:
            await hold_last_slot()
    finally:
        if waiter is not None:
            second_outcome = await asyncio.wait_for(waiter, DEADLINE)
    durable = await durable_state(database)

    assert second_outcome is (
        GraphPreparationAdmissionOutcome.PREPARED
        if rollback_winner
        else GraphPreparationAdmissionOutcome.FULL
    )
    assert durable == (1, [saved_preparation(second if rollback_winner else first)])
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


async def refuse_increment_after_insert(
    writer: AsyncConnection, dialect: Dialect, suppress_update: bool
) -> None:
    if dialect is Dialect.SQLITE:
        await writer.exec_driver_sql(
            "CREATE TRIGGER refuse_admission_increment BEFORE UPDATE "
            "ON graph_deployment_preparation_capacity "
            "WHEN EXISTS (SELECT 1 FROM graph_deployment_preparation) "
            "BEGIN SELECT RAISE(IGNORE); END"
            if suppress_update
            else "CREATE TRIGGER refuse_admission_increment BEFORE UPDATE "
            "ON graph_deployment_preparation_capacity "
            "WHEN EXISTS (SELECT 1 FROM graph_deployment_preparation) "
            "BEGIN SELECT RAISE(ABORT, 'private_fixture_increment_failure'); END"
        )
    else:
        await writer.exec_driver_sql(
            "CREATE FUNCTION refuse_admission_increment() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM graph_deployment_preparation) THEN "
            "IF TG_ARGV[0] = 'suppress' THEN RETURN NULL; END IF; "
            "RAISE EXCEPTION 'private_fixture_increment_failure'; END IF; "
            "RETURN NEW; END $$"
        )
        await writer.exec_driver_sql(
            "CREATE TRIGGER refuse_admission_increment BEFORE UPDATE "
            "ON graph_deployment_preparation_capacity FOR EACH ROW "
            "EXECUTE FUNCTION refuse_admission_increment('suppress')"
            if suppress_update
            else "CREATE TRIGGER refuse_admission_increment BEFORE UPDATE "
            "ON graph_deployment_preparation_capacity FOR EACH ROW "
            "EXECUTE FUNCTION refuse_admission_increment('reject')"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_update", [False, True])
async def test_graph_preparation_rolls_back_a_real_post_insert_failure(
    database: Database, kora_capture: Capture, suppress_update: bool
) -> None:
    binding, selected = await seed_binding(database, "original")
    other, other_selected = await seed_binding(database, "after_failure")
    await insert_ledger(database, 0)
    ledger = ConnectionLedger(database)

    async def fail_increment() -> None:
        async with database.transaction() as writer:
            locks = await admission_locks(writer, binding, selected)
            await refuse_increment_after_insert(
                writer, database.dialect, suppress_update
            )
            await admit_graph_preparation(writer, locks, binding, selected, 1)

    with pytest.raises(GraphPreparationAdmissionFailed) as refused:
        await fail_increment()
    unchanged = await durable_state(database)
    recovered = await reserve(database, other, other_selected, 1)
    durable = await durable_state(database)
    diagnostics = [
        record
        for record in kora_capture.finish()
        if record["event"] == "db.transaction.failed"
    ]

    assert unchanged == (0, [])
    assert recovered is GraphPreparationAdmissionOutcome.PREPARED
    assert durable == (1, [saved_preparation(other)])
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert len(diagnostics) == 1
    for private in (
        binding.preparation_id,
        binding.namespace_id,
        GRAPH_NAME,
        DIGEST,
        "private_fixture_increment_failure",
    ):
        assert private not in repr(diagnostics)
        assert private not in "".join(format_exception(refused.value))
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_graph_preparation_cancellation_between_writes_releases_the_writer(
    database: Database,
) -> None:
    binding, selected = await seed_binding(database, "original")
    other, other_selected = await seed_binding(database, "after_cancellation")
    await insert_ledger(database, 0)
    ledger = ConnectionLedger(database)
    incrementing = asyncio.Event()
    unreleased = asyncio.Event()

    def pause_increment(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("UPDATE graph_deployment_preparation_capacity "):
            incrementing.set()
            await_(asyncio.wait_for(unreleased.wait(), DEADLINE))

    async def reserve_until_paused() -> None:
        async with database.transaction() as writer:
            locks = await admission_locks(writer, binding, selected)
            event.listen(
                writer.sync_connection, "before_cursor_execute", pause_increment
            )
            try:
                await admit_graph_preparation(writer, locks, binding, selected, 1)
            finally:
                event.remove(
                    writer.sync_connection, "before_cursor_execute", pause_increment
                )

    task = asyncio.create_task(reserve_until_paused())
    try:
        await asyncio.wait_for(incrementing.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    unchanged = await durable_state(database)
    recovered = await reserve(database, other, other_selected, 1)
    durable = await durable_state(database)

    assert unchanged == (0, [])
    assert recovered is GraphPreparationAdmissionOutcome.PREPARED
    assert durable == (1, [saved_preparation(other)])
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
