from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.connection import Database, Dialect
from kora.db.migrations import upgrade_database
from kora.db.run_event_page_v1 import (
    ACTOR_BYTES,
    SQLITE_EVENT_PAGE_INDEX,
    sqlite_run_event_page_available,
)
from kora.db.run_event_reads import (
    CORRUPT_KEY,
    RunEventReadRejected,
    decode_run_event_page,
    read_run_event_page,
    run_event_scope,
    run_event_statement,
)
from kora.db.tables import event
from kora.db.types import JsonObject
from tests.db.seed import START, Seeder
from tests.support.databases import encoded_sqlite_settings

SYSTEM: JsonObject = {"kind": "system", "component": "scheduler"}
USER: JsonObject = {"kind": "user", "owner": '"' * 256, "token_id": "\\" * 256}


@dataclass(frozen=True)
class EventScope:
    namespace_id: str
    run_id: str
    node_id: str
    attempt_id: str

    def bounds(self, ceiling: str, after: str) -> dict[str, str | int]:
        return {
            "namespace_id": self.namespace_id,
            "run_id": self.run_id,
            "take": 4,
            "ceiling": ceiling,
            "after": after,
        }


async def seed_run(connection: AsyncConnection) -> EventScope:
    seeder = Seeder.at(connection, START)
    namespace_id = await seeder.namespace()
    graph_id = await seeder.graph(namespace_id)
    graph_version = await seeder.graph_version(graph_id)
    run_id = await seeder.run(namespace_id, graph_id, graph_version)
    node_id = await seeder.node(run_id, "internal")
    attempt_id = await seeder.attempt(node_id)
    return EventScope(namespace_id, run_id, node_id, attempt_id)


async def seed_event(
    connection: AsyncConnection,
    scope: EventScope,
    actor: JsonObject,
    payload_size: int,
    *,
    event_id: str | None = None,
    node_id: str | None = None,
) -> str:
    seeder = Seeder(connection)
    event_id = seeder.new_id() if event_id is None else event_id
    await connection.execute(
        insert(event).values(
            id=event_id,
            namespace_id=scope.namespace_id,
            run_id=scope.run_id,
            node_instance_id=scope.node_id if node_id is None else node_id,
            attempt_id=scope.attempt_id,
            kind="run.succeeded",
            actor=actor,
            payload={"private": "p" * payload_size},
            at=START,
            **seeder.stamps(),
        )
    )
    return event_id


async def assert_index_only(
    connection: AsyncConnection,
    statement: TextClause,
    parameters: dict[str, str | int],
) -> None:
    root = await connection.scalar(
        text("SELECT rootpage FROM main.sqlite_schema WHERE name = 'event'")
    )
    trace = (
        await connection.execute(text("EXPLAIN " + str(statement)), parameters)
    ).all()
    plan = (
        await connection.execute(
            text("EXPLAIN QUERY PLAN " + str(statement)), parameters
        )
    ).all()
    sources = {item[2] for item in trace if item[1] == "OpenRead" and item[3] == root}
    operations = {item[1] for item in trace if item[2] in sources}

    assert any(
        f"USING COVERING INDEX {SQLITE_EVENT_PAGE_INDEX}" in item[3] for item in plan
    )
    assert not operations.intersection(
        {"Column", "FinishSeek", "SeekRowid", "SeekGE", "SeekLE", "SeekGT", "SeekLT"}
    )


async def bounded_record_geometry(connection: AsyncConnection) -> dict[str, int] | None:
    if not await connection.scalar(
        text("SELECT sqlite_compileoption_used('ENABLE_DBSTAT_VTAB')")
    ):
        return None
    rows = (
        await connection.execute(
            text("""
                SELECT name, max(mx_payload) FROM dbstat
                WHERE name IN ('event', :bounded_index) GROUP BY name
                """),
            {"bounded_index": SQLITE_EVENT_PAGE_INDEX},
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_sqlite_native_event_query_uses_only_bounded_index(
    tmp_path: Path, encoding: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "events.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        async with database.transaction() as writer:
            scope = await seed_run(writer)
            small = await seed_event(writer, scope, USER, 16)
            wide = await seed_event(writer, scope, USER, 1024 * 1024)
            bad = await seed_event(writer, scope, {"owner": "z" * (1024 * 1024)}, 16)
            adjacent = await seed_event(
                writer, scope, {"owner": "v" * (1024 * 1024)}, 16
            )

        async with database.read_snapshot() as reader:
            assert await sqlite_run_event_page_available(reader)
            for ceiling, after in ((None, None), (adjacent, adjacent)):
                await assert_index_only(
                    reader,
                    run_event_statement(True, ceiling, after),
                    scope.bounds(adjacent, adjacent),
                )
            await assert_index_only(
                reader, CORRUPT_KEY, scope.bounds(adjacent, adjacent)
            )
            valid_rows = await read_run_event_page(
                reader, scope.namespace_id, scope.run_id, adjacent, bad, 2
            )
            invalid_rows = await read_run_event_page(
                reader, scope.namespace_id, scope.run_id, adjacent, adjacent, 3
            )
        async with database.transaction() as writer:
            await seed_event(writer, scope, SYSTEM, 8, event_id="P" * (1024 * 1024))
        async with database.read_snapshot() as reader:
            with pytest.raises(
                RunEventReadRejected, match="invalid retained run event"
            ):
                await read_run_event_page(
                    reader, scope.namespace_id, scope.run_id, adjacent, bad, 2
                )
            geometry = await bounded_record_geometry(reader)

        decoded = decode_run_event_page(valid_rows)
        assert [row.id for row in decoded] == [wide, small]
        assert {row.at for row in decoded} == {START}
        assert all(row.actor == USER for row in decoded)
        assert invalid_rows[0].id == bad
        assert invalid_rows[0].actor is None
        if geometry is not None:
            assert geometry[SQLITE_EVENT_PAGE_INDEX] < 4096
            assert geometry["event"] > 1024 * 1024
        with pytest.raises(RunEventReadRejected, match="invalid retained run event"):
            decode_run_event_page(invalid_rows)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_event_page_scoped_historical_actor_and_sibling(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        scope = await seed_run(writer)
        sibling = await Seeder(writer).node(scope.run_id, "sibling")
        event_id = await seed_event(writer, scope, USER, 1024 * 1024)

    async with database.read_snapshot() as reader:
        admitted = await run_event_scope(reader, scope.namespace_id, scope.run_id)
        rows = await read_run_event_page(
            reader, scope.namespace_id, scope.run_id, None, None, 1
        )
    async with database.transaction() as writer:
        await writer.execute(
            update(event).where(event.c.id == event_id).values(node_instance_id=sibling)
        )
    async with database.read_snapshot() as reader:
        invalid = await read_run_event_page(
            reader, scope.namespace_id, scope.run_id, None, None, 1
        )

    assert admitted
    assert [row.actor for row in decode_run_event_page(rows)] == [USER]
    with pytest.raises(RunEventReadRejected, match="invalid retained run event"):
        decode_run_event_page(invalid)


@pytest.mark.asyncio
async def test_run_event_actor_admission_refuses_wide_saved_documents(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        scope = await seed_run(writer)
        await seed_event(writer, scope, USER, 1024 * 1024)
        compressed_id = await seed_event(
            writer, scope, {"owner": "z" * (1024 * 1024)}, 16
        )
        if database.dialect is Dialect.POSTGRESQL:
            await writer.execute(
                text("ALTER TABLE event ALTER COLUMN actor SET STORAGE EXTERNAL")
            )
        external_id = await seed_event(
            writer, scope, {"owner": "v" * (1024 * 1024)}, 16
        )

    async with database.read_snapshot() as reader:
        logical = (
            (
                await reader.execute(
                    text("""
                        SELECT pg_column_compression(actor), pg_column_size(actor)
                        FROM event WHERE id IN (:compressed, :external) ORDER BY id
                        """),
                    {"compressed": compressed_id, "external": external_id},
                )
            ).all()
            if database.dialect is Dialect.POSTGRESQL
            else None
        )
        rows = await read_run_event_page(
            reader, scope.namespace_id, scope.run_id, None, None, 3
        )

    if logical is not None:
        assert logical[0][0] is not None
        assert logical[1][0] is None
        assert logical[1][1] > ACTOR_BYTES
    assert [row.actor for row in rows[:2]] == [None, None]
    assert [row.actor for row in decode_run_event_page(rows[2:])] == [USER]
