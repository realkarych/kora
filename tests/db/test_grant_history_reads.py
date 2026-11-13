import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Connection, Table, event, func, literal, select, update
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.util.concurrency import await_

from kora.db.connection import Database, Dialect
from kora.db.grant_history import GrantHistoryRejected, read_history_rows
from kora.db.run_input_parent import sqlite_storage_width
from kora.db.tables import attempt, attempt_dispatcher
from kora.db.types import JsonObject, JsonValue, as_json_value
from kora.engine.grant_history import acquire_history, append_grant, save_history_batch
from kora.engine.locks import TransitionLocks
from kora.protocols.history import GrantHistorySnapshot, record_bytes
from kora.protocols.worker_claim_system import PROCESS_BYTES
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.db.test_run_input_parent import open_sqlite
from tests.engine.test_grant_history import (
    LIMITS,
    acquired,
    append,
    grant,
    legacy,
    save,
    seed,
)


class PausedHistory:
    def __init__(self, database: Database) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.engine = database.reader_engine.sync_engine

    def __enter__(self) -> "PausedHistory":
        event.listen(self.engine, "before_cursor_execute", self.before)
        return self

    def __exit__(self, *arguments: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self.before)

    def before(
        self,
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        *arguments: object,
    ) -> None:
        if "attempt_dispatcher" in statement and not self.entered.is_set():
            self.entered.set()
            await_(self.release.wait())


class PausedPrefixWrite:
    def __init__(self, database: Database) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.engine = database.writer_engine.sync_engine

    def __enter__(self) -> "PausedPrefixWrite":
        event.listen(self.engine, "after_cursor_execute", self.after)
        return self

    def __exit__(self, *arguments: object) -> None:
        event.remove(self.engine, "after_cursor_execute", self.after)

    def after(
        self,
        connection: Connection,
        cursor: DBAPICursor,
        statement: str,
        *arguments: object,
    ) -> None:
        if statement.startswith("UPDATE attempt_dispatcher SET prefix_count"):
            self.entered.set()
            await_(self.release.wait())


@pytest.mark.asyncio
async def test_cancelled_prefix_write_rolls_back_the_entire_batch(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 4))
    prepared = await acquired(database, scope)
    ledger = ConnectionLedger(database)

    with PausedPrefixWrite(database) as pause:
        pending = asyncio.create_task(save(database, prepared))
        try:
            await asyncio.wait_for(pause.entered.wait(), 10)
        finally:
            pending.cancel()
            pause.release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending

    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    async with database.read_transaction() as reader:
        counts = (
            (await reader.execute(select(attempt_dispatcher.c.prefix_count)))
            .scalars()
            .all()
        )
        tip = await reader.scalar(select(attempt.c.grant_history_count))
    assert counts == [None, None, None]
    assert tip is None
    restored = await save(database, prepared)
    assert restored is not None
    assert restored.count == 3


async def row_allowance(database: Database, table: Table) -> int:
    width = sum(
        (sqlite_storage_width(field) for field in table.c),
        literal(9 * (len(table.c) + 1)),
    )
    async with database.read_transaction() as reader:
        value = await reader.scalar(select(width))
    assert type(value) is int
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["prefix", "tip"])
async def test_history_write_admits_resulting_utf16_row_width(
    tmp_path: Path,
    owner: str,
) -> None:
    database = await open_sqlite(tmp_path, "UTF-16le")
    try:
        scope = await seed(database)
        await legacy(database, scope, range(1, 2))
        prepared = await acquired(database, scope)
        table = attempt_dispatcher if owner == "prefix" else attempt
        field = "dispatcher_row" if owner == "prefix" else "attempt_row"
        before = await row_allowance(database, table)
        after = before + (136 if owner == "prefix" else 144)

        async def persist(maximum: int) -> GrantHistorySnapshot | None:
            async with database.transaction() as writer:
                locks = TransitionLocks(writer)
                await locks.acquire_run(scope.run_id)
                return await save_history_batch(
                    writer, locks, prepared, replace(LIMITS, **{field: maximum})
                )

        with pytest.raises(GrantHistoryRejected):
            await persist(after - 1)
        unchanged = await row_allowance(database, table)
        result = await persist(after)

        assert unchanged == before
        assert result is not None
        assert result.count == 1
        assert await row_allowance(database, table) == after
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_append_row_allowance_refuses_before_insert(
    database: Database,
) -> None:
    scope = await seed(database)
    binding = grant(scope, 1, process=True)
    minimum = 2 * len(record_bytes(binding)) + 1024

    async def persist(maximum: int) -> GrantHistorySnapshot | None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(scope.run_id)
            return await append_grant(
                writer,
                locks,
                scope,
                binding,
                "execute",
                START,
                replace(LIMITS, dispatcher_row=maximum),
            )

    with pytest.raises(GrantHistoryRejected):
        await persist(minimum - 1)
    async with database.read_transaction() as reader:
        refused_count = await reader.scalar(
            select(func.count()).select_from(attempt_dispatcher)
        )
    result = await persist(minimum)

    assert refused_count == 0
    assert result is not None
    assert result.count == 1
    if database.dialect is Dialect.SQLITE:
        assert await row_allowance(database, attempt_dispatcher) <= minimum


@pytest.mark.asyncio
async def test_cancelled_history_read_releases_its_connection(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 3))
    ledger = ConnectionLedger(database)
    with PausedHistory(database) as pause:
        pending = asyncio.create_task(
            acquire_history(database, scope, LIMITS, deadline=time.monotonic() + 30)
        )
        await asyncio.wait_for(pause.entered.wait(), 10)

        pending.cancel()
        pause.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    assert ledger.outstanding == database.units_in_flight() == 0
    assert len((await acquired(database, scope)).grants) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatcher_row", -1),
        ("attempt_row", True),
        ("native_bytes", 1),
        ("raw_bytes", 1),
    ],
)
async def test_history_reservation_refuses_before_checkout(
    database: Database, field: str, value: int
) -> None:
    scope = await seed(database)
    limits = replace(LIMITS, **{field: value})
    ledger = ConnectionLedger(database)

    with pytest.raises(GrantHistoryRejected):
        await acquire_history(database, scope, limits, deadline=time.monotonic() + 30)

    assert ledger.checkouts == ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_history_scalar_byte_guard_refuses_embedded_nul_suffix(
    tmp_path: Path, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        scope = await seed(database)
        await legacy(database, scope, range(1, 3))
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "UPDATE attempt_dispatcher SET incarnation_id = ? "
                "WHERE lease_epoch = 3",
                ("x\0" + "z" * 20_000,),
            )

        with pytest.raises(GrantHistoryRejected):
            await acquired(database, scope)
        async with database.read_snapshot() as reader:
            with executed_queries(reader) as statements:
                with pytest.raises(GrantHistoryRejected):
                    await read_history_rows(
                        reader, scope, LIMITS, PROCESS_BYTES, maximum=2
                    )
            sql, args = statements[0]
            rows = (await reader.exec_driver_sql(sql, args)).all()

        assert len(rows) == 2
        assert rows[0][0] is False or rows[0][0] == 0
        assert all(value is None for value in rows[0][1:])
        assert rows[1][0] is True or rows[1][0] == 1
    finally:
        await database.close()


def plan_nodes(value: JsonValue) -> list[JsonObject]:
    if isinstance(value, list):
        return [node for item in value for node in plan_nodes(item)]
    if isinstance(value, dict):
        own = [value] if "Node Type" in value else []
        return own + [node for item in value.values() for node in plan_nodes(item)]
    return []


@pytest.mark.asyncio
async def test_history_queries_use_bounded_native_index_ranges(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 2049))
    await save(database, await acquired(database, scope))
    async with database.transaction() as writer:
        await writer.exec_driver_sql("ANALYZE attempt_dispatcher")

    async with database.read_snapshot() as reader:
        with executed_queries(reader) as statements:
            prefix = await read_history_rows(
                reader,
                scope,
                LIMITS,
                PROCESS_BYTES,
                maximum=1,
                verified=True,
                newest=True,
            )
            rows = await read_history_rows(
                reader, scope, LIMITS, PROCESS_BYTES, maximum=129, after=384
            )
        if database.dialect is Dialect.POSTGRESQL:
            plans = [
                as_json_value(
                    (
                        await reader.exec_driver_sql(
                            "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, args
                        )
                    ).scalar_one()
                )
                for sql, args in statements
            ]
        else:
            plans = [
                str(
                    list(
                        await reader.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args)
                    )
                )
                for sql, args in statements
            ]

    assert len(prefix) == 1
    assert prefix[0].epoch == 384
    assert len(rows) == 129
    assert rows[0].epoch == 387
    if database.dialect is Dialect.POSTGRESQL:
        nodes = [node for plan in plans for node in plan_nodes(plan)]
        assert all(node["Node Type"] in ("Limit", "Index Scan") for node in nodes)
        assert all(
            type(node["Actual Rows"]) is int and node["Actual Rows"] <= 129
            for node in nodes
        )
        assert any(
            node.get("Index Name") == "ix_dispatcher_verified_prefix" for node in nodes
        )
        assert all(node.get("Rows Removed by Filter", 0) == 0 for node in nodes)
    else:
        assert all(
            "SEARCH attempt_dispatcher USING INDEX" in str(plan) for plan in plans
        )
        assert all("TEMP B-TREE" not in str(plan) for plan in plans)


@pytest.mark.asyncio
async def test_history_rechecks_mutable_row_before_prefix_update(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path, "UTF-16le")
    try:
        scope = await seed(database)
        await legacy(database, scope, range(1, 3))
        prepared = await acquired(database, scope)
        async with database.transaction() as writer:
            await writer.execute(
                update(attempt_dispatcher)
                .where(attempt_dispatcher.c.lease_epoch == 3)
                .values(evidence={"value": "x" * 40_000})
            )

        async def refused_update() -> None:
            async with database.transaction() as writer:
                locks = TransitionLocks(writer)
                await locks.acquire_run(scope.run_id)
                await save_history_batch(writer, locks, prepared, LIMITS)

        with pytest.raises(GrantHistoryRejected):
            await refused_update()

        async with database.read_transaction() as reader:
            counts = (
                (await reader.execute(select(attempt_dispatcher.c.prefix_count)))
                .scalars()
                .all()
            )
            tip = await reader.scalar(select(attempt.c.grant_history_count))
        assert counts == [None, None]
        assert tip is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_history_sqlite_native_seek_and_evidence_metadata_guard(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path)
    try:
        scope = await seed(database)
        await append(database, scope, 1)
        await legacy(database, scope, range(2, 1026))
        async with database.read_snapshot() as reader:
            with executed_queries(reader) as statements:
                prefix = await read_history_rows(
                    reader,
                    scope,
                    LIMITS,
                    PROCESS_BYTES,
                    maximum=1,
                    verified=True,
                    newest=True,
                )
                batch = await read_history_rows(
                    reader, scope, LIMITS, PROCESS_BYTES, maximum=129, after=3
                )
            plans = [
                list(await reader.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args))
                for sql, args in statements
            ]
            native = await sqlite_instructions(reader, *statements[1])

        assert len(prefix) == 1
        assert len(batch) == 129
        assert batch[0].epoch == 6
        assert "ix_dispatcher_verified_prefix" in str(plans[0])
        assert all(
            "SEARCH attempt_dispatcher USING INDEX" in str(plan) for plan in plans
        )
        assert all("TEMP B-TREE" not in str(plan) for plan in plans)
        evidence_column = attempt_dispatcher.c.keys().index("evidence")
        reads = [
            row for row in native if row[1] == "Column" and row[3] == evidence_column
        ]
        assert reads
        assert all(row[6] in (0x80, 0xC0) for row in reads)
    finally:
        await database.close()
