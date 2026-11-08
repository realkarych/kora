import asyncio
from dataclasses import replace
from traceback import format_exception

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import Database, Dialect
from kora.db.graph_deployment_stage_reads import (
    GraphStageScope,
    read_graph_stage_inventory,
)
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.tables import (
    graph_deployment_stage,
    graph_deployment_stage_occurrence,
    storage_address_fence,
)
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    LockError,
    Scope,
    ScopeKind,
    StorageAddressPublicationFenceReadFailed,
    StorageAddressPublicationFenceUnavailable,
    StorageAddressStageUnavailable,
    TransitionLocks,
)
from kora.protocols.identity_records import MAX_COUNT
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    PreparedScope,
    occurrence_values,
    stage_values,
)
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, lock_inventory
from tests.engine.test_graph_publication_target import publication_scopes
from tests.engine.test_graph_stage_fence import (
    address_statement_key,
    set_generation,
)
from tests.engine.test_publication_preparation_lock import seed_original
from tests.engine.test_storage_address_lock import (
    DIGEST,
    OTHER_DIGEST,
    STORE,
    seed_address,
)

ADDRESS = (STORE, DIGEST)
FENCE_KEY = (
    storage_address_fence.c.store_id == STORE,
    storage_address_fence.c.payload_digest == DIGEST,
)


async def seed_publication_fence(
    database: Database,
) -> tuple[GraphPreparationBinding, str, int]:
    binding, selected = await seed_original(database)
    await seed_address(database)
    async with database.transaction() as connection:
        incarnation: object = await connection.scalar(
            select(storage_address_fence.c.fence_incarnation).where(*FENCE_KEY)
        )
        assert type(incarnation) is int
        scope = PreparedScope(binding.namespace_id, binding.preparation_id)
        stage_id = Seeder(connection).new_id()
        await connection.execute(
            graph_deployment_stage.insert().values(
                **stage_values(scope, stage_id, incarnation)
            )
        )
        await connection.execute(
            graph_deployment_stage_occurrence.insert().values(
                **occurrence_values(scope, stage_id, 0)
            )
        )
    return binding, selected, incarnation


async def lock_publication_fence(
    writer: AsyncConnection,
    binding: GraphPreparationBinding,
    selected: str,
    *,
    additional_graph: str | None = None,
    staging_only: bool = False,
) -> TransitionLocks:
    locks = TransitionLocks(writer)
    scopes = publication_scopes(binding, selected)
    if additional_graph is None:
        await locks.acquire_scopes(scopes)
    else:
        await locks.acquire_scopes(
            (*scopes, Scope(ScopeKind.GRAPH, (additional_graph,)))
        )
    await locks.acquire_existing_storage_addresses((ADDRESS,), max_addresses=1)
    if staging_only:
        await locks.acquire_open_graph_preparation(writer, *ADDRESS, binding)
    else:
        await locks.acquire_publication_graph_preparation(writer, binding, selected)
    return locks


@pytest.mark.asyncio
async def test_publication_fence_rechecks_current_idle_state_with_shared_indexed_read(
    database: Database,
) -> None:
    binding, selected, incarnation = await seed_publication_fence(database)
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = await lock_publication_fence(writer, binding, selected)
        with executed_queries(writer) as publication:
            await locks.require_publication_stage_fence(
                writer, binding, selected, *ADDRESS, incarnation
            )
        with executed_queries(writer) as staging:
            admitted = await locks.read_stage_fence_incarnation(
                writer, *ADDRESS, binding
            )
        await set_generation(writer, MAX_COUNT, MAX_COUNT)
        with executed_queries(writer) as pending:
            with pytest.raises(StorageAddressPublicationFenceUnavailable):
                await locks.require_publication_stage_fence(
                    writer, binding, selected, *ADDRESS, incarnation
                )
        await set_generation(writer, MAX_COUNT, None)
        await locks.require_publication_stage_fence(
            writer, binding, selected, *ADDRESS, incarnation
        )
        with pytest.raises(StorageAddressStageUnavailable):
            await locks.read_stage_fence_incarnation(writer, *ADDRESS, binding)

    [(statement, args)] = publication
    [(staging_statement, staging_args)] = staging
    assert staging_statement == statement
    assert pending == publication
    assert [
        (left, right)
        for left, right in zip(args, staging_args, strict=True)
        if left != right
    ] == [(MAX_COUNT, MAX_COUNT - 1)]
    assert address_statement_key(statement)
    assert STORE in args
    assert DIGEST in args
    assert "FOR UPDATE" not in statement
    assert admitted == incarnation
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_fence_refuses_saved_stage_after_address_recreation(
    database: Database,
) -> None:
    binding, selected, original = await seed_publication_fence(database)
    async with database.transaction() as writer:
        await writer.execute(storage_address_fence.delete().where(*FENCE_KEY))
        replacement: object = await writer.scalar(
            storage_address_fence.insert()
            .values(store_id=STORE, payload_digest=DIGEST, last_deletion_generation=0)
            .returning(storage_address_fence.c.fence_incarnation)
        )
        assert type(replacement) is int
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = await lock_publication_fence(writer, binding, selected)
        inventory = await read_graph_stage_inventory(
            writer, GraphStageScope(binding.preparation_id, binding.namespace_id)
        )
        (stage,) = inventory.stages
        with executed_queries(writer) as stale:
            with pytest.raises(StorageAddressPublicationFenceUnavailable) as refused:
                await locks.require_publication_stage_fence(
                    writer,
                    binding,
                    selected,
                    stage.store_id,
                    stage.payload_digest,
                    stage.fence_incarnation,
                )
        await locks.require_publication_stage_fence(
            writer, binding, selected, *ADDRESS, replacement
        )

    assert stage.fence_incarnation == original < replacement
    assert len(stale) == 1
    assert (
        str(refused.value) == "the graph publication stage fence could not be verified"
    )
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_fence_refuses_invalid_guards_before_sql(
    database: Database,
) -> None:
    binding, selected, incarnation = await seed_publication_fence(database)
    async with database.transaction() as writer:
        other_graph = await Seeder(writer).graph(binding.namespace_id, "other")
        wrong_binding = replace(binding, preparation_id=Seeder(writer).new_id())

    async with database.transaction() as writer:
        locks = await lock_publication_fence(
            writer, binding, selected, staging_only=True
        )
        with executed_queries(writer) as stage_only:
            with pytest.raises(
                LockError, match="publication preparation lock is not held"
            ):
                await locks.require_publication_stage_fence(
                    writer, binding, selected, *ADDRESS, incarnation
                )
    async with database.read_transaction() as reader, database.transaction() as writer:
        locks = await lock_publication_fence(
            writer, binding, selected, additional_graph=other_graph
        )
        with executed_queries(writer) as rejected, executed_queries(reader) as foreign:
            with pytest.raises(
                LockError, match="publication preparation lock is not held"
            ):
                await locks.require_publication_stage_fence(
                    writer, binding, other_graph, *ADDRESS, incarnation
                )
            with pytest.raises(
                LockError, match="publication preparation lock is not held"
            ):
                await locks.require_publication_stage_fence(
                    writer, wrong_binding, selected, *ADDRESS, incarnation
                )
            with pytest.raises(LockError, match="address lock is not held"):
                await locks.require_publication_stage_fence(
                    writer, binding, selected, STORE, OTHER_DIGEST, incarnation
                )
            with pytest.raises(LockError, match="does not own"):
                await locks.require_publication_stage_fence(
                    reader, binding, selected, *ADDRESS, incarnation
                )
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.require_publication_stage_fence(
                        writer, binding, selected, *ADDRESS, incarnation
                    )
                )
            for invalid in (True, 0, MAX_COUNT + 1):
                with pytest.raises(StorageAddressPublicationFenceUnavailable):
                    await locks.require_publication_stage_fence(
                        writer, binding, selected, *ADDRESS, invalid
                    )

    assert stage_only == rejected == foreign == []
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_fence_masks_bad_native_generation_and_refuses_missing_row(
    database: Database,
) -> None:
    binding, selected, incarnation = await seed_publication_fence(database)
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE storage_address_fence DROP CONSTRAINT "
                "ck_storage_address_fence_last_generation_nonnegative"
            )

    async with database.transaction() as writer:
        locks = await lock_publication_fence(writer, binding, selected)
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            try:
                await writer.exec_driver_sql(
                    "UPDATE storage_address_fence SET last_deletion_generation=? "
                    "WHERE store_id=? AND payload_digest=?",
                    (b"x" * 8192, *ADDRESS),
                )
            finally:
                await writer.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        else:
            await set_generation(writer, -1, None)
        with executed_queries(writer) as malformed:
            with pytest.raises(StorageAddressPublicationFenceUnavailable):
                await locks.require_publication_stage_fence(
                    writer, binding, selected, *ADDRESS, incarnation
                )
        [(statement, args)] = malformed
        raw = (await writer.exec_driver_sql(statement, args)).one()
        await writer.execute(storage_address_fence.delete().where(*FENCE_KEY))
        with executed_queries(writer) as missing:
            with pytest.raises(StorageAddressPublicationFenceUnavailable):
                await locks.require_publication_stage_fence(
                    writer, binding, selected, *ADDRESS, incarnation
                )
        with pytest.raises(LockError, match="cannot be used"):
            locks.require_existing_storage_address(writer, *ADDRESS)

    assert tuple(raw) == (None,)
    assert missing == malformed
    assert database.writer_is_free()


async def fail_publication_fence_query(
    database: Database,
    binding: GraphPreparationBinding,
    selected: str,
    incarnation: int,
) -> None:
    async with database.transaction() as writer:
        locks = await lock_publication_fence(writer, binding, selected)
        await writer.exec_driver_sql(
            "ALTER TABLE storage_address_fence RENAME COLUMN last_deletion_generation "
            "TO private_unavailable_generation"
        )
        try:
            await locks.require_publication_stage_fence(
                writer, binding, selected, *ADDRESS, incarnation
            )
        except StorageAddressPublicationFenceReadFailed:
            with pytest.raises(LockError, match="cannot be used"):
                locks.require_existing_storage_address(writer, *ADDRESS)
            raise


@pytest.mark.asyncio
async def test_publication_fence_failure_keeps_private_query_out_of_error(
    database: Database,
) -> None:
    binding, selected, incarnation = await seed_publication_fence(database)
    ledger = ConnectionLedger(database)

    with pytest.raises(StorageAddressPublicationFenceReadFailed) as refused:
        await fail_publication_fence_query(database, binding, selected, incarnation)
    async with database.transaction() as writer:
        locks = await lock_publication_fence(writer, binding, selected)
        await locks.require_publication_stage_fence(
            writer, binding, selected, *ADDRESS, incarnation
        )

    error = refused.value
    rendered = "".join(format_exception(error))
    assert error.__cause__ is error.__context__ is None
    assert str(error) == "the graph publication stage fence read failed"
    assert DIGEST not in rendered
    assert "private_unavailable_generation" not in rendered
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_cancelled_publication_fence_check_invalidates_guard_and_releases_writer(
    database: Database,
) -> None:
    binding, selected, incarnation = await seed_publication_fence(database)
    ledger = ConnectionLedger(database)
    entered = asyncio.Event()
    unreleased = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if address_statement_key(statement):
            entered.set()
            await_(asyncio.wait_for(unreleased.wait(), DEADLINE))

    async def check_until_cancelled() -> None:
        async with database.transaction() as writer:
            locks = await lock_publication_fence(writer, binding, selected)
            event.listen(writer.sync_connection, "before_cursor_execute", pause)
            try:
                await locks.require_publication_stage_fence(
                    writer, binding, selected, *ADDRESS, incarnation
                )
            except asyncio.CancelledError:
                with pytest.raises(
                    (LockError, WriterTransactionError),
                    match="cannot be used|not current",
                ):
                    locks.require_existing_storage_address(writer, *ADDRESS)
                raise
            finally:
                event.remove(writer.sync_connection, "before_cursor_execute", pause)

    task = asyncio.create_task(check_until_cancelled())
    try:
        await asyncio.wait_for(entered.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    async with database.transaction() as writer:
        locks = await lock_publication_fence(writer, binding, selected)
        await locks.require_publication_stage_fence(
            writer, binding, selected, *ADDRESS, incarnation
        )

    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
