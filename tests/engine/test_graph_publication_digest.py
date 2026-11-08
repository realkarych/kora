import asyncio
from dataclasses import replace
from traceback import format_exception

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.util import await_

from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_publication_digest_v1 import DIGEST_INDEX, INVALID_ID_INDEX
from kora.db.tables import graph_version
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    GraphPublicationDigestReadFailed,
    GraphPublicationDigestUnavailable,
    LockError,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, lock_inventory
from tests.engine.test_graph_publication_target import (
    publication_locks,
    publication_scopes,
    seed_target,
)

OTHER_DIGEST = "e" * 64
PRIVATE_MARKER = "PRIVATE-MATCH-863"


async def read_digest(
    database: Database, binding: GraphPreparationBinding, selected: str
) -> str | None:
    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        assert (
            await locks.read_graph_publication_target(writer, binding, selected)
            == selected
        )
        return await locks.read_graph_publication_digest_match(
            writer, binding, selected
        )


@pytest.mark.asyncio
async def test_publication_digest_probes_only_the_exact_current_graph(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        version_id = await seeder.graph_version(
            selected, ir={"x": PRIVATE_MARKER * 4096}
        )
        other_namespace = await seeder.namespace("other")
        other_graph = await seeder.graph(other_namespace)
        await seeder.graph_version(other_graph)
        alternative = await seeder.graph(binding.namespace_id, "alternative")
        await seeder.graph_version(alternative)
        template = (
            (
                await writer.execute(
                    select(graph_version).where(graph_version.c.id == version_id)
                )
            )
            .mappings()
            .one()
        )
        await writer.execute(
            graph_version.insert(),
            [
                dict(template)
                | {
                    "id": seeder.new_id(),
                    "number": number,
                    "deployment_digest": f"{number:064x}",
                    "ir": {},
                }
                for number in range(2, 1026)
            ],
        )
        await writer.exec_driver_sql("ANALYZE graph_version")
    bound = replace(binding, initial_graph_id=selected)
    different = replace(bound, deployment_digest=OTHER_DIGEST)
    missing = replace(binding, graph_name="absent")
    mismatch = replace(binding, graph_name="alternative")
    forged = replace(binding, namespace_id=other_namespace)
    explain = (
        "EXPLAIN QUERY PLAN "
        if database.dialect is Dialect.SQLITE
        else "EXPLAIN (ANALYZE, COSTS OFF) "
    )

    async with database.transaction() as writer:
        locks = await publication_locks(writer, bound, selected)
        with executed_queries(writer) as without_target:
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_digest_match(writer, bound, selected)
        assert (
            await locks.read_graph_publication_target(writer, bound, selected)
            == selected
        )
        mutable_source = replace(bound)
        assert (
            await locks.read_graph_publication_target(writer, mutable_source, selected)
            == selected
        )
        mutable_source.__init__(
            bound.preparation_id,
            bound.namespace_id,
            bound.graph_name,
            selected,
            OTHER_DIGEST,
        )
        with executed_queries(writer) as refused:
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_digest_match(
                    writer, mutable_source, selected
                )
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_digest_match(
                    writer, different, selected
                )
            with pytest.raises(LockError, match="required transaction locks"):
                await locks.read_graph_publication_digest_match(
                    writer, forged, selected
                )
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.read_graph_publication_digest_match(writer, bound, selected)
                )
        async with database.read_transaction() as reader:
            with executed_queries(reader) as foreign:
                with pytest.raises(LockError, match="does not own"):
                    await locks.read_graph_publication_digest_match(
                        reader, bound, selected
                    )
        with executed_queries(writer) as hit_queries:
            found = await locks.read_graph_publication_digest_match(
                writer, bound, selected
            )
        assert (
            await locks.read_graph_publication_target(writer, different, selected)
            == selected
        )
        with executed_queries(writer) as miss_queries:
            absent = await locks.read_graph_publication_digest_match(
                writer, different, selected
            )
        assert (
            await locks.read_graph_publication_target(writer, bound, selected)
            == selected
        )
        assert (
            await locks.read_graph_publication_target(writer, missing, selected) is None
        )
        with executed_queries(writer) as stale:
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_digest_match(writer, bound, selected)
        assert (
            await locks.read_graph_publication_target(writer, mismatch, selected)
            == alternative
        )
        with executed_queries(writer) as mismatched:
            with pytest.raises(LockError, match="target was not observed"):
                await locks.read_graph_publication_digest_match(
                    writer, mismatch, selected
                )
        assert (
            await locks.read_graph_publication_target(writer, bound, selected)
            == selected
        )
        await locks.acquire_existing_storage_addresses((), max_addresses=0)
        with executed_queries(writer) as late:
            with pytest.raises(LockError, match="precedes storage addresses"):
                await locks.read_graph_publication_digest_match(writer, bound, selected)
        plans = [
            (await writer.exec_driver_sql(explain + query, values)).all()
            for query, values in (*hit_queries, *miss_queries)
        ]
    with pytest.raises(WriterTransactionError, match="not current"):
        await locks.read_graph_publication_digest_match(writer, bound, selected)

    assert without_target == refused == foreign == stale == mismatched == late == []
    assert found == version_id
    assert absent is None
    assert len(hit_queries) == len(miss_queries) == 1
    assert hit_queries[0][0] == miss_queries[0][0]
    assert DIGEST in hit_queries[0][1]
    assert OTHER_DIGEST in miss_queries[0][1]
    for plan in plans:
        rendered = str(plan)
        assert DIGEST_INDEX in rendered
        if database.dialect is Dialect.SQLITE:
            assert INVALID_ID_INDEX in rendered
            assert "SCAN graph_version" not in rendered
            assert rendered.count("(graph_id=? AND deployment_digest=?)") == 2
        else:
            assert "Index Only Scan" in rendered
            assert "Seq Scan" not in rendered
            assert "Bitmap" not in rendered
            [condition] = [
                str(row[0]).partition("Index Cond: ")[2]
                for row in plan
                if "Index Cond: " in str(row[0])
            ]
            assert "graph_id" in condition
            assert "deployment_digest" in condition
    assert await lock_inventory(database) == []
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True], ids=["ambiguous", "malformed"])
async def test_publication_digest_refuses_ambiguous_or_malformed_hits(
    database: Database, invalid: bool
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        original = await seeder.graph_version(selected)
        if invalid:
            await writer.execute(
                text("UPDATE graph_version SET id = :bad WHERE id = :version"),
                {"bad": "8" + "Z" * 25, "version": original},
            )
        else:
            for number in range(2, 5):
                await seeder.graph_version(selected, number)

    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        assert (
            await locks.read_graph_publication_target(writer, binding, selected)
            == selected
        )
        with executed_queries(writer) as queries:
            with pytest.raises(GraphPublicationDigestUnavailable) as refused:
                await locks.read_graph_publication_digest_match(
                    writer, binding, selected
                )
        [(statement, values)] = queries
        native = (await writer.exec_driver_sql(statement, values)).all()
        with pytest.raises(LockError, match="cannot be used"):
            locks.require(None, publication_scopes(binding, selected))

    if invalid:
        assert native == [(None,)]
    else:
        assert len(native) == 2
    assert refused.value.__context__ is refused.value.__cause__ is None
    assert original not in "".join(format_exception(refused.value))
    assert DIGEST not in "".join(format_exception(refused.value))
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["failed", "cancelled"])
async def test_publication_digest_failure_or_cancellation_releases_writer(
    database: Database, cancel: bool
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        expected = await Seeder(writer).graph_version(selected)
    ledger = ConnectionLedger(database)
    reading = asyncio.Event()
    unfinished = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if "FROM graph_version" in statement:
            reading.set()
            await_(asyncio.wait_for(unfinished.wait(), DEADLINE))

    async def interrupted() -> None:
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            assert (
                await locks.read_graph_publication_target(writer, binding, selected)
                == selected
            )
            if cancel:
                event.listen(writer.sync_connection, "before_cursor_execute", pause)
            else:
                await writer.exec_driver_sql(
                    "ALTER TABLE graph_version RENAME TO unavailable_graph_version"
                )
            try:
                await locks.read_graph_publication_digest_match(
                    writer, binding, selected
                )
            except (asyncio.CancelledError, GraphPublicationDigestReadFailed):
                with pytest.raises(
                    (LockError, WriterTransactionError),
                    match="cannot be used|not current",
                ):
                    locks.require(None, publication_scopes(binding, selected))
                raise
            finally:
                if cancel:
                    event.remove(writer.sync_connection, "before_cursor_execute", pause)

    if cancel:
        task = asyncio.create_task(interrupted())
        try:
            await asyncio.wait_for(reading.wait(), DEADLINE)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, DEADLINE)
    else:
        with pytest.raises(GraphPublicationDigestReadFailed) as failed:
            await interrupted()
        rendered = "".join(format_exception(failed.value))
        assert failed.value.__cause__ is failed.value.__context__ is None
        assert DIGEST not in rendered
        assert "SELECT" not in rendered
    recovered = await read_digest(database, binding, selected)

    assert recovered == expected
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()
