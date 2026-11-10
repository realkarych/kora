import asyncio
import sqlite3
from pathlib import Path
from traceback import format_exception, walk_tb
from typing import cast

import pytest
from sqlalchemy import event, text

from kora.api.graph_publication_metadata import (
    PreparedGraphPublicationMetadata,
    prepare_graph_publication_metadata,
)
from kora.api.graph_publication_sqlite import (
    SQLiteGraphPublicationMeasurementUnavailable,
    observe_sqlite_graph_publication,
)
from kora.api.graph_publication_values import (
    PreparedGraphPublicationBinds,
    prepare_graph_publication_binds,
)
from kora.conformance.capture import captured
from kora.db import DB_COMPONENT
from kora.db.connection import Database, Dialect
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.migrations import upgrade_database
from kora.db.types import canonical_json
from kora.observability import Level, LoggingSettings
from tests.api.test_graph_publication_metadata import personal_principal
from tests.api.test_graph_publication_values import (
    PRIVATE_WIRE,
    checked_attachment,
    publication_limits,
)
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import FakeClock
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.support.databases import sqlite_settings

RESOLVED = (("clé", "private-resolved-width-1229"),)
ACTOR = "private-deployer-width-1229"
METADATA_BYTES = len(canonical_json(dict(RESOLVED)).encode()) + len(ACTOR.encode())
CREATE_CANARY = text("CREATE TABLE publication_canary (value INTEGER)")
INSERT_CANARY = text("INSERT INTO publication_canary VALUES (7)")
READ_CANARY = text("SELECT value FROM publication_canary")
SHADOW_LAYOUT = text("CREATE TABLE pragma_table_xinfo (value INTEGER)")


@pytest.fixture(scope="module")
def measurement_binds() -> PreparedGraphPublicationBinds:
    candidate = checked_attachment(with_files=False)
    return prepare_graph_publication_binds(
        candidate, limits=publication_limits(candidate)
    )


@pytest.fixture(scope="module")
def measurement_metadata() -> PreparedGraphPublicationMetadata:
    return prepare_graph_publication_metadata(
        Dialect.SQLITE,
        personal_principal(ACTOR),
        RESOLVED,
        max_metadata_bytes=METADATA_BYTES,
    )


@pytest.mark.asyncio
async def test_sqlite_publication_measurement_requires_the_current_sqlite_writer(
    database: Database,
    measurement_binds: PreparedGraphPublicationBinds,
    measurement_metadata: PreparedGraphPublicationMetadata,
) -> None:
    connection_unit = (
        database.read_snapshot()
        if database.dialect is Dialect.SQLITE
        else database.transaction()
    )
    ledger = ConnectionLedger(database)

    async with connection_unit as connection:
        with (
            executed_queries(connection) as queries,
            pytest.raises(SQLiteGraphPublicationMeasurementUnavailable),
        ):
            await observe_sqlite_graph_publication(
                connection,
                measurement_binds,
                measurement_metadata,
            )

    assert queries == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_dialect", [True, False])
async def test_sqlite_publication_measurement_refuses_unprepared_metadata_before_sql(
    database: Database,
    measurement_binds: PreparedGraphPublicationBinds,
    foreign_dialect: bool,
) -> None:
    ledger = ConnectionLedger(database)
    metadata = (
        prepare_graph_publication_metadata(
            Dialect.POSTGRESQL,
            personal_principal(ACTOR),
            RESOLVED,
            max_metadata_bytes=METADATA_BYTES,
        )
        if foreign_dialect
        else cast(PreparedGraphPublicationMetadata, cast(object, {"owner": ACTOR}))
    )

    async with database.transaction() as writer:
        with (
            executed_queries(writer) as queries,
            pytest.raises(GraphValueRejected) as refused,
        ):
            await observe_sqlite_graph_publication(writer, measurement_binds, metadata)

    assert refused.value.reason is GraphValueFailure.INVALID
    assert queries == []
    for frame, _ in walk_tb(refused.value.__traceback__):
        if frame.f_code.co_name == "observe_sqlite_graph_publication":
            assert "metadata" not in frame.f_locals
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["layout", "measurement", "cancel"])
async def test_sqlite_publication_measurement_refusal_and_cancellation_release_writer(
    tmp_path: Path,
    measurement_binds: PreparedGraphPublicationBinds,
    measurement_metadata: PreparedGraphPublicationMetadata,
    mode: str,
) -> None:
    settings = sqlite_settings(tmp_path / "publication.db")
    await upgrade_database(settings)
    database = Database(settings)
    ledger = ConnectionLedger(database)
    dispatched: list[bool] = []
    denied: list[bool] = []

    def deny_measurement(
        action: int,
        name: str | None,
        argument: str | None,
        schema: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_FUNCTION and argument == "length":
            denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def observe_measurement_dispatch(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if "resolved_digests_native_bytes" in statement:
            dispatched.append(True)
            if mode == "cancel":
                task = asyncio.current_task()
                assert task is not None
                asyncio.get_running_loop().call_soon(task.cancel)

    async def fail_on_writer() -> None:
        async with database.transaction() as writer:
            await writer.execute(INSERT_CANARY)
            driver = await writer_driver(writer)
            if mode == "layout":
                await writer.execute(SHADOW_LAYOUT)
            elif mode == "measurement":
                await driver.set_authorizer(deny_measurement)
            event.listen(
                writer.sync_connection,
                "before_cursor_execute",
                observe_measurement_dispatch,
            )
            try:
                await observe_sqlite_graph_publication(
                    writer,
                    measurement_binds,
                    measurement_metadata,
                )
            finally:
                if mode == "measurement":
                    await driver.set_authorizer(None)
                event.remove(
                    writer.sync_connection,
                    "before_cursor_execute",
                    observe_measurement_dispatch,
                )

    try:
        async with database.transaction() as writer:
            await writer.execute(CREATE_CANARY)
        error_type = (
            asyncio.CancelledError
            if mode == "cancel"
            else SQLiteGraphPublicationMeasurementUnavailable
        )

        with captured(
            DB_COMPONENT,
            LoggingSettings(level=Level.DEBUG),
            "sqlite-publication-test",
            FakeClock().now,
        ) as capture:
            with pytest.raises(error_type) as refused:
                await asyncio.wait_for(
                    fail_on_writer(), timeout=CONDITION_DEADLINE_SECONDS
                )
        records = capture.finish()
        logged = capture.reader.data
        released = (
            ledger.outstanding == database.units_in_flight() == 0
            and database.writer_is_free()
        )
        async with database.transaction() as writer:
            canary = (await writer.execute(READ_CANARY)).all()
            recovered = await observe_sqlite_graph_publication(
                writer,
                measurement_binds,
                measurement_metadata,
            )
    finally:
        await database.close()

    assert released
    assert canary == []
    assert recovered.resource_transport_bytes > 0
    assert denied == ([True] if mode == "measurement" else [])
    assert dispatched == ([] if mode == "layout" else [True])
    assert refused.value.__cause__ is refused.value.__context__ is None
    for frame, _ in walk_tb(refused.value.__traceback__):
        if frame.f_code.co_name == "observe_sqlite_graph_publication":
            assert {
                "binds",
                "metadata",
                "parameters",
                "lease",
            }.isdisjoint(frame.f_locals)
        assert not frame.f_code.co_filename.endswith("/sqlalchemy/engine/base.py")
    assert [
        record["event"]
        for record in records
        if record["event"] == "db.transaction.failed"
    ] == ([] if mode == "cancel" else ["db.transaction.failed"])
    rendered = "".join(format_exception(refused.value))
    for marker in (
        PRIVATE_WIRE,
        "private-resolved-width-1229",
        "private-deployer-width-1229",
    ):
        assert marker not in rendered
        assert marker.encode() not in logged
    assert ledger.outstanding == database.units_in_flight() == 0
    assert ledger.opened == ledger.closed > 0
