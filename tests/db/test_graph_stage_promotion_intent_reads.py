import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from traceback import format_exception, walk_tb
from typing import Protocol, cast

import pytest
from aiosqlite import Connection as SQLiteDriver
from sqlalchemy import event
from sqlalchemy.engine.interfaces import ExceptionContext
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.capture import captured
from kora.db.connection import Database, Dialect
from kora.db.graph_stage_promotion_snapshot import (
    GraphStagePromotionSnapshotFailure,
    GraphStagePromotionSnapshotRejected,
    read_graph_stage_promotion_snapshot,
)
from kora.db.migrations import upgrade_database
from kora.db.tables import graph_deployment_stage_promotion_intent as intent
from kora.observability import Level, LoggingSettings
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    REFUSAL_DEADLINE_SECONDS,
    ConnectionLedger,
    until,
)
from tests.db.test_graph_stage_promotion_snapshot import (
    DOCUMENT_BYTES,
    FIRST,
    expected_intent,
    read,
    seed_documents,
)
from tests.support.databases import sqlite_settings

SCALAR_QUERY: str = "SELECT store_id, payload_digest, fence_incarnation"
DOCUMENT_QUERY: str = " AS intent_present"
SQLITE_METADATA_QUERY: str = "SELECT rowid,"
WIDE_DIGEST_BYTES: int = 4 * 1024 * 1024
PRIVATE_MARKER: str = "PRIVATE-SCALAR-FIXTURE"


class SQLiteTraceDriver(Protocol):
    async def set_trace_callback(
        self, handler: Callable[[str], None] | None
    ) -> None: ...

    async def set_progress_handler(
        self, handler: Callable[[], int] | None, n: int
    ) -> None: ...


def captured_sqlite_debug(records: list[dict[str, object]]) -> bool:
    return any(
        record.get("event") == "thirdparty.record"
        and record.get("logger") == "aiosqlite"
        and record.get("level") == "DEBUG"
        for record in records
    )


@contextmanager
def observed_values(database: Database) -> Generator[list[str]]:
    observed: list[str] = []

    def observe(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if SCALAR_QUERY in statement:
            observed.append("scalars")
        elif DOCUMENT_QUERY in statement:
            observed.append("documents")

    engine = database.reader_engine.sync_engine
    event.listen(engine, "after_cursor_execute", observe)
    try:
        yield observed
    finally:
        event.remove(engine, "after_cursor_execute", observe)


@asynccontextmanager
async def damage_writer(database: Database) -> AsyncGenerator[AsyncConnection]:
    async with database.transaction() as writer:
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        try:
            yield writer
        finally:
            if database.dialect is Dialect.SQLITE:
                await writer.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")


async def allow_wide_digest(database: Database) -> None:
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage_promotion_intent "
                "DROP CONSTRAINT ck_promotion_intent_request_digest_domain"
            )


async def damage_native_intent(database: Database, stage_id: str, damage: str) -> None:
    if damage == "width":
        await allow_wide_digest(database)
    async with damage_writer(database) as writer:
        selected = intent.update().where(intent.c.stage_id == stage_id)
        if damage == "width":
            await writer.execute(
                selected.values(request_digest="x" * WIDE_DIGEST_BYTES)
            )
        elif damage == "address":
            await writer.execute(selected.values(store_id="different-store"))
        elif damage == "payload":
            await writer.execute(selected.values(payload_digest="sha256:" + "0" * 64))
        elif damage == "fence":
            await writer.execute(selected.values(fence_incarnation=17))
        elif damage == "operation":
            if database.dialect is Dialect.POSTGRESQL:
                await writer.exec_driver_sql(
                    "ALTER TABLE graph_deployment_stage_promotion_intent "
                    "DROP CONSTRAINT ck_promotion_intent_operation_id_domain"
                )
            await writer.execute(selected.values(operation_id=" "))
        elif damage == "generation":
            if database.dialect is Dialect.POSTGRESQL:
                await writer.exec_driver_sql(
                    "ALTER TABLE graph_deployment_stage_promotion_intent "
                    "DROP CONSTRAINT ck_promotion_intent_generation_nonnegative"
                )
            await writer.execute(selected.values(content_generation=-1))
        elif database.dialect is Dialect.SQLITE:
            values = (
                selected.values(operation_id=b"first")
                if damage == "text-storage"
                else selected.values(content_generation=b"7")
            )
            await writer.execute(values)
        elif damage == "text-storage":
            await writer.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage_promotion_intent "
                "DROP CONSTRAINT ck_promotion_intent_operation_id_domain, "
                "ALTER COLUMN operation_id TYPE bytea "
                "USING convert_to(operation_id, 'UTF8')"
            )
        else:
            await writer.exec_driver_sql(
                "ALTER TABLE graph_deployment_stage_promotion_intent "
                "DROP CONSTRAINT ck_promotion_intent_generation_nonnegative, "
                "ALTER COLUMN content_generation TYPE TEXT "
                "USING content_generation::TEXT"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "width",
        "address",
        "payload",
        "fence",
        "operation",
        "generation",
        "text-storage",
        "integer-storage",
    ],
)
async def test_unverified_intent_refuses_before_document_projection(
    database: Database, damage: str
) -> None:
    binding, stage_ids = await seed_documents(database)
    await damage_native_intent(database, stage_ids[0], damage)
    ledger = ConnectionLedger(database)

    with observed_values(database) as observed:
        with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
            await read(database, binding)

    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert observed == (
        ["scalars"]
        if damage in ("address", "payload", "fence", "operation", "generation")
        else []
    )
    assert ledger.checkouts == 1
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_native_width_and_scalar_seeks_keep_the_same_snapshot(
    database: Database,
) -> None:
    binding, stage_ids = await seed_documents(database)
    await allow_wide_digest(database)
    ledger = ConnectionLedger(database)
    before_scalar = asyncio.Event()
    resume = asyncio.Event()
    engine = database.reader_engine.sync_engine

    def pause_before_scalar(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if SCALAR_QUERY in statement and not before_scalar.is_set():
            before_scalar.set()
            await_(resume.wait())

    event.listen(engine, "before_cursor_execute", pause_before_scalar)
    try:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(read(database, binding))
            try:
                await asyncio.wait_for(before_scalar.wait(), CONDITION_DEADLINE_SECONDS)
                async with damage_writer(database) as writer:
                    await writer.execute(
                        intent.update()
                        .where(intent.c.stage_id == stage_ids[0])
                        .values(
                            request_digest="x" * WIDE_DIGEST_BYTES, request_bytes=b"new"
                        )
                    )
            finally:
                resume.set()
        prior = task.result()
    finally:
        event.remove(engine, "before_cursor_execute", pause_before_scalar)
    with observed_values(database) as observed:
        with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
            await read(database, binding)

    assert prior.documents[0].intent_scalars == expected_intent(prior, 0, "first")
    assert prior.documents[0].request_bytes == FIRST
    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert observed == []
    assert ledger.checkouts == 3
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding_statement",
    [
        "PRAGMA encoding='UTF-8'",
        "PRAGMA encoding='UTF-16le'",
        "PRAGMA encoding='UTF-16be'",
    ],
)
async def test_sqlite_operation_width_follows_its_exact_database_encoding(
    tmp_path: Path, encoding_statement: str
) -> None:
    path = tmp_path / "encoding.sqlite3"
    with closing(sqlite3.connect(path)) as seed:
        seed.execute(encoding_statement)
        seed.execute("CREATE TABLE encoding_anchor (id INTEGER)")
    settings = sqlite_settings(path)
    await upgrade_database(settings)
    database = Database(settings)
    operation = "x" * 256

    try:
        binding, stage_ids = await seed_documents(database)
        async with database.transaction() as writer:
            await writer.execute(
                intent.update()
                .where(intent.c.stage_id == stage_ids[0])
                .values(operation_id=operation)
            )
        admitted = await read(database, binding)
        async with damage_writer(database) as writer:
            await writer.execute(
                intent.update()
                .where(intent.c.stage_id == stage_ids[0])
                .values(operation_id=operation + "x")
            )
        with observed_values(database) as observed:
            with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
                await read(database, binding)
    finally:
        await database.close()

    assert admitted.documents[0].intent_scalars == expected_intent(
        admitted, 0, operation
    )
    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert observed == []
    assert database.units_in_flight() == 0


async def sqlite_worker(database: Database) -> SQLiteDriver:
    async with database.read_transaction() as connection:
        driver: object = (await connection.get_raw_connection()).driver_connection
        assert isinstance(driver, SQLiteDriver)
        return driver


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
async def test_sqlite_metadata_cursor_keeps_ownership_until_native_release(
    tmp_path: Path, interrupt: str
) -> None:
    settings = sqlite_settings(tmp_path / "worker.sqlite3")
    await upgrade_database(settings)
    database = Database(settings)
    entered = asyncio.Event()
    resumed = threading.Event()
    loop = asyncio.get_running_loop()
    timed = interrupt == "deadline"
    driver: SQLiteDriver | None = None
    task: asyncio.Task[object] | None = None

    def barrier(statement: str) -> None:
        if SQLITE_METADATA_QUERY in statement:
            loop.call_soon_threadsafe(entered.set)
            if not resumed.wait(CONDITION_DEADLINE_SECONDS):
                raise TimeoutError("native worker barrier was not released")

    try:
        binding, _ = await seed_documents(database)
        driver = await sqlite_worker(database)
        trace_driver = cast(SQLiteTraceDriver, driver)
        await trace_driver.set_trace_callback(barrier)
        ledger = ConnectionLedger(database)
        interval = REFUSAL_DEADLINE_SECONDS if timed else CONDITION_DEADLINE_SECONDS
        with observed_values(database) as observed:
            task = asyncio.create_task(
                read_graph_stage_promotion_snapshot(
                    database,
                    binding,
                    max_stages=3,
                    max_document_bytes=DOCUMENT_BYTES,
                    deadline=loop.time() + interval,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
                if not timed:
                    assert task.cancel()
                await until(lambda: task.cancelling() > 0)
                assert not task.done()
                assert ledger.outstanding == database.units_in_flight() == 1
            finally:
                resumed.set()
            if timed:
                with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
                    await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
                assert (
                    refused.value.reason is GraphStagePromotionSnapshotFailure.DEADLINE
                )
            else:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        await trace_driver.set_trace_callback(None)
        with closing(sqlite3.connect(settings.sqlite_path, timeout=0)) as native:
            checkpoint = native.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert checkpoint == (0, 0, 0)
        assert await sqlite_worker(database) is driver
        recovered = await read(database, binding)
    finally:
        resumed.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await database.close()

    assert observed == []
    assert len(recovered.documents) == 3
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_native_intent_failure_keeps_driver_debug_private(
    database: Database,
) -> None:
    binding, stage_ids = await seed_documents(database)
    await allow_wide_digest(database)
    marker = PRIVATE_MARKER.encode()
    padding = b"x" * (70 - len(marker))
    async with damage_writer(database) as writer:
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql(
                "UPDATE graph_deployment_stage_promotion_intent "
                "SET request_digest = CAST(? AS TEXT) WHERE stage_id = ?",
                (marker + padding + b"\xff", stage_ids[0]),
            )
        else:
            await writer.execute(
                intent.update()
                .where(intent.c.stage_id == stage_ids[0])
                .values(request_digest=PRIVATE_MARKER + padding[:-1].decode() + "é")
            )
            await writer.exec_driver_sql("SET client_encoding TO 'SQL_ASCII'")
    settings = LoggingSettings(
        level=Level.DEBUG, third_party={"aiosqlite": Level.DEBUG}
    )

    faults: list[bool] = []

    def observe_fault(context: ExceptionContext) -> None:
        error = context.original_exception
        if isinstance(error, UnicodeDecodeError):
            faults.append(marker in error.object)
        else:
            faults.append(PRIVATE_MARKER in str(error))

    engine = database.reader_engine.sync_engine
    event.listen(engine, "handle_error", observe_fault)
    try:
        with captured(
            "db", settings, "promotion-native-test", lambda: datetime.now(UTC)
        ) as capture:
            with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
                await read(database, binding)
            records = capture.finish()
    finally:
        event.remove(engine, "handle_error", observe_fault)
        if database.dialect is Dialect.POSTGRESQL:
            async with database.transaction() as writer:
                await writer.exec_driver_sql("SET client_encoding TO 'UTF8'")

    rendered = json.dumps(records)
    assert faults == [True]
    if database.dialect is Dialect.SQLITE:
        assert captured_sqlite_debug(records)
    assert PRIVATE_MARKER not in rendered
    assert stage_ids[0] not in rendered
    assert SQLITE_METADATA_QUERY not in rendered
    assert PRIVATE_MARKER not in "".join(format_exception(refused.value))
    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert refused.value.__cause__ is refused.value.__context__ is None
    frames = {frame.f_code.co_name for frame, _ in walk_tb(refused.value.__traceback__)}
    assert not {"_selected_row", "_collect", "_read_current"} & frames
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_raw_sqlite_metadata_failure_refuses_privately_and_reuses_worker(
    tmp_path: Path,
) -> None:
    settings = sqlite_settings(tmp_path / "native-error.sqlite3")
    await upgrade_database(settings)
    database = Database(settings)
    selected = threading.Event()
    interrupted = threading.Event()

    def trace(statement: str) -> None:
        if SQLITE_METADATA_QUERY in statement:
            selected.set()

    def interrupt_once() -> int:
        if selected.is_set() and not interrupted.is_set():
            interrupted.set()
            return 1
        return 0

    try:
        binding, stage_ids = await seed_documents(database)
        driver = await sqlite_worker(database)
        trace_driver = cast(SQLiteTraceDriver, driver)
        await trace_driver.set_trace_callback(trace)
        await trace_driver.set_progress_handler(interrupt_once, 1)
        logging_settings = LoggingSettings(
            level=Level.DEBUG, third_party={"aiosqlite": Level.DEBUG}
        )

        try:
            with captured(
                "db",
                logging_settings,
                "promotion-native-test",
                lambda: datetime.now(UTC),
            ) as capture:
                with observed_values(database) as observed:
                    with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
                        await read(database, binding)
                records = capture.finish()
        finally:
            await trace_driver.set_progress_handler(None, 0)
            await trace_driver.set_trace_callback(None)
        assert await sqlite_worker(database) is driver
        recovered = await read(database, binding)
    finally:
        await database.close()

    assert selected.is_set()
    assert interrupted.is_set()
    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert "interrupted" not in "".join(format_exception(refused.value))
    assert captured_sqlite_debug(records)
    output = json.dumps(records)
    assert stage_ids[0] not in output
    assert SQLITE_METADATA_QUERY not in output
    assert observed == []
    assert len(recovered.documents) == 3
    assert database.units_in_flight() == 0
