import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.migrations import upgrade_database
from kora.db.tables import graph
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    GraphPublicationTargetReadFailed,
    GraphPublicationTargetUnavailable,
    LockError,
    Scope,
    ScopeKind,
    TransitionLocks,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)
from tests.engine.test_storage_address_lock import DIGEST as ADDRESS_DIGEST
from tests.engine.test_storage_address_lock import STORE, seed_address
from tests.support.databases import sqlite_settings


def publication_scopes(
    binding: GraphPreparationBinding, selected: str
) -> tuple[Scope, Scope]:
    return (
        Scope(ScopeKind.NAMESPACE, (binding.namespace_id,)),
        Scope(ScopeKind.GRAPH, (selected,)),
    )


async def publication_locks(
    writer: AsyncConnection, binding: GraphPreparationBinding, selected: str
) -> TransitionLocks:
    locks = TransitionLocks(writer)
    await locks.acquire_scopes(publication_scopes(binding, selected))
    return locks


async def seed_target(database: Database) -> tuple[GraphPreparationBinding, str]:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        return (
            GraphPreparationBinding(
                seeder.new_id(), namespace_id, "pipeline", None, DIGEST
            ),
            await seeder.graph(namespace_id),
        )


async def read_target(
    database: Database, binding: GraphPreparationBinding, selected: str
) -> str | None:
    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        return await locks.read_graph_publication_target(writer, binding, selected)


@pytest.mark.asyncio
async def test_publication_target_reads_one_exact_scalar_after_scopes(
    database: Database,
) -> None:
    await seed_address(database)
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        other_namespace = await seeder.namespace("other")
        other_graph = await seeder.graph(other_namespace)
        await writer.execute(
            graph.insert(),
            [
                {
                    "id": seeder.new_id(),
                    "namespace_id": other_namespace,
                    "name": f"unrelated_{index}",
                    **seeder.stamps(),
                }
                for index in range(1024)
            ],
        )
        await writer.exec_driver_sql("ANALYZE graph")
    bound = replace(binding, initial_graph_id=selected)
    missing = replace(binding, graph_name="absent")
    disjoint = replace(binding, namespace_id=other_namespace)
    explain = (
        "EXPLAIN QUERY PLAN "
        if database.dialect is Dialect.SQLITE
        else "EXPLAIN (COSTS OFF) "
    )

    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        with executed_queries(writer) as refused:
            with pytest.raises(LockError, match="required transaction locks"):
                await locks.read_graph_publication_target(writer, disjoint, selected)
            with pytest.raises(LockError, match="required transaction locks"):
                await locks.read_graph_publication_target(writer, binding, other_graph)
            with pytest.raises(LockError, match="differs from the bound graph"):
                await locks.read_graph_publication_target(writer, bound, other_graph)
            with pytest.raises(LockError, match="invalid selected graph identity"):
                await locks.read_graph_publication_target(writer, binding, "invalid")
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.read_graph_publication_target(writer, binding, selected)
                )
        async with database.read_transaction() as reader:
            with executed_queries(reader) as foreign_queries:
                with pytest.raises(LockError, match="does not own"):
                    await locks.read_graph_publication_target(reader, binding, selected)
        with executed_queries(writer) as hit_queries:
            found = await locks.read_graph_publication_target(writer, bound, selected)
        with executed_queries(writer) as miss_queries:
            absent = await locks.read_graph_publication_target(
                writer, missing, selected
            )
        [(statement, args)] = hit_queries
        [(miss_statement, miss_args)] = miss_queries
        raw_hit = (await writer.exec_driver_sql(statement, args)).one()
        raw_miss = (
            await writer.exec_driver_sql(miss_statement, miss_args)
        ).one_or_none()
        plans = [
            str((await writer.exec_driver_sql(explain + query, values)).all())
            for query, values in (hit_queries[0], miss_queries[0])
        ]
        await locks.acquire_existing_storage_address(STORE, ADDRESS_DIGEST)
        with executed_queries(writer) as late:
            with pytest.raises(LockError, match="precedes storage addresses"):
                await locks.read_graph_publication_target(writer, binding, selected)
    with pytest.raises(WriterTransactionError, match="not current"):
        await locks.read_graph_publication_target(writer, binding, selected)
    observed_other = await read_target(database, disjoint, other_graph)

    assert refused == foreign_queries == late == []
    assert found == selected
    assert absent is None
    assert raw_hit == (selected,)
    assert raw_miss is None
    assert observed_other == other_graph
    assert statement == miss_statement
    assert binding.namespace_id in args
    assert binding.graph_name in args
    assert missing.graph_name in miss_args
    assert binding.graph_name not in statement
    assert all("namespace_id" in plan and "name" in plan for plan in plans)
    index = (
        "sqlite_autoindex_graph_2"
        if database.dialect is Dialect.SQLITE
        else "uq_graph_namespace_id_name"
    )
    assert all(index in plan for plan in plans)
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["commit", "rollback", "cancel"])
async def test_publication_target_observes_competing_writer_after_wait(
    database: Database, ending: Literal["commit", "rollback", "cancel"]
) -> None:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace()
        binding = GraphPreparationBinding(
            seeder.new_id(), namespace_id, "pipeline", None, DIGEST
        )
        winner_id = seeder.new_id()
        prospective_id = seeder.new_id()
    ledger = ConnectionLedger(database)
    ready = asyncio.Event()
    release = asyncio.Event()

    async def publish_candidate() -> None:
        async with database.transaction() as writer:
            await publication_locks(writer, binding, winner_id)
            await writer.execute(
                graph.insert().values(
                    id=winner_id,
                    namespace_id=namespace_id,
                    name=binding.graph_name,
                    **Seeder(writer).stamps(),
                )
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()

    holder = asyncio.create_task(publish_candidate())
    follower: asyncio.Task[str | None] | None = None
    target: str | None = None
    try:
        await asyncio.wait_for(ready.wait(), DEADLINE)
        follower = asyncio.create_task(read_target(database, binding, prospective_id))
        await wait_for_lock_waiter(database)
        assert not follower.done()
    finally:
        if ending == "cancel":
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(holder, DEADLINE)
        else:
            release.set()
            if ending == "rollback":
                with pytest.raises(DeliberateRollback):
                    await asyncio.wait_for(holder, DEADLINE)
            else:
                await asyncio.wait_for(holder, DEADLINE)
        if follower is not None:
            target = await asyncio.wait_for(follower, DEADLINE)

    assert follower is not None
    assert target == (winner_id if ending == "commit" else None)
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()


async def check_native_projection(database: Database) -> None:
    binding, selected = await seed_target(database)
    valid = await read_target(database, binding, selected)
    invalid: tuple[str | bytes, ...] = ("8" + "Z" * 25,)
    if database.dialect is Dialect.SQLITE:
        invalid = (*invalid, b"B" * 26, "M" * (1 << 20), b"B" * (1 << 20))
    current: str | bytes = selected

    for value in invalid:
        async with database.transaction() as writer:
            await writer.execute(
                text("UPDATE graph SET id = :value WHERE id = :current"),
                {"value": value, "current": current},
            )
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            with executed_queries(writer) as queries:
                with pytest.raises(GraphPublicationTargetUnavailable) as refused:
                    await locks.read_graph_publication_target(writer, binding, selected)
            [(statement, args)] = queries
            raw = (await writer.exec_driver_sql(statement, args)).one()
            with pytest.raises(LockError, match="cannot be used"):
                locks.require(None, publication_scopes(binding, selected))

        assert raw == ((None,) if value != invalid[0] else (value,))
        assert refused.value.__cause__ is None
        assert refused.value.__context__ is None
        current = value
    missing = await read_target(
        database, replace(binding, graph_name="absent"), selected
    )

    assert valid == selected
    assert missing is None


@pytest.mark.asyncio
async def test_publication_target_masks_bad_native_values_before_driver(
    empty_database_settings: DatabaseSettings, tmp_path: Path
) -> None:
    if empty_database_settings.dialect is Dialect.POSTGRESQL:
        settings = (empty_database_settings,)
    else:
        settings = tuple(
            sqlite_settings(tmp_path / f"graph-target-{encoding}.db")
            for encoding in ("UTF-8", "UTF-16le", "UTF-16be")
        )
        for database_settings, encoding in zip(
            settings, ("UTF-8", "UTF-16le", "UTF-16be"), strict=True
        ):
            with closing(sqlite3.connect(database_settings.sqlite_path)) as native:
                native.execute(f"PRAGMA encoding = '{encoding}'")
                native.execute("CREATE TABLE encoding_seed (value INTEGER)")
                native.execute("DROP TABLE encoding_seed")
                native.commit()

    for database_settings in settings:
        await upgrade_database(database_settings)
        database = Database(database_settings)
        try:
            await check_native_projection(database)
        finally:
            await database.close()


@pytest.mark.asyncio
async def test_publication_target_failure_rolls_back_and_releases_writer(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    ledger = ConnectionLedger(database)

    async def read_unavailable_table() -> None:
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            await writer.exec_driver_sql(
                "ALTER TABLE graph RENAME TO unavailable_graph"
            )
            try:
                await locks.read_graph_publication_target(writer, binding, selected)
            except GraphPublicationTargetReadFailed:
                with pytest.raises(LockError, match="cannot be used"):
                    locks.require(None, publication_scopes(binding, selected))
                raise

    with pytest.raises(GraphPublicationTargetReadFailed) as refused:
        await read_unavailable_table()
    recovered = await read_target(database, binding, selected)

    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert binding.namespace_id not in repr(refused.value)
    assert binding.graph_name not in repr(refused.value)
    assert recovered == selected
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_target_cancellation_poisons_guard_and_releases_writer(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    ledger = ConnectionLedger(database)
    reading = asyncio.Event()
    unfinished = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if "FROM graph" in statement:
            reading.set()
            await_(asyncio.wait_for(unfinished.wait(), DEADLINE))

    async def read_until_cancelled() -> None:
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            event.listen(writer.sync_connection, "before_cursor_execute", pause)
            try:
                await locks.read_graph_publication_target(writer, binding, selected)
            except asyncio.CancelledError:
                with pytest.raises(
                    (LockError, WriterTransactionError),
                    match="cannot be used|not current",
                ):
                    locks.require(None, publication_scopes(binding, selected))
                raise
            finally:
                event.remove(writer.sync_connection, "before_cursor_execute", pause)

    task = asyncio.create_task(read_until_cancelled())
    try:
        await asyncio.wait_for(reading.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    recovered = await read_target(database, binding, selected)

    assert recovered == selected
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()
