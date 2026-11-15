import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Connection, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.util import await_

from kora.db.connection import Database, DatabaseSettings
from kora.db.custody_models import (
    CustodyNativeOutcome,
    CustodyTransactionBinding,
    CustodyUnavailable,
)
from kora.db.custody_transaction import OriginalCustodyTransaction
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger


def binding() -> CustodyTransactionBinding:
    return CustodyTransactionBinding(
        metadata_id=b"m" * 16,
        issuer_sequence=1,
        connection_sequence=1,
        transaction_sequence=1,
        settings_digest=b"s" * 32,
        postgres=None,
    )


@pytest_asyncio.fixture
async def custody_database(
    database_settings: DatabaseSettings,
) -> AsyncIterator[Database]:
    database = Database(database_settings)
    try:
        async with database.transaction() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE custody_parent (id INTEGER PRIMARY KEY)"
            )
            await connection.exec_driver_sql(
                "CREATE TABLE custody_child ("
                "id INTEGER REFERENCES custody_parent(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "deferred_failure"),
    [("commit", False), ("rollback", False), ("commit", True)],
)
async def test_original_native_outcome_survives_repeated_cancellation(
    custody_database: Database, operation: str, deferred_failure: bool
) -> None:
    database = custody_database
    ledger = ConnectionLedger(database)
    parked = asyncio.Event()
    release = asyncio.Event()
    entered: list[Connection] = []

    def park_original(connection: Connection) -> None:
        entered.append(connection)
        parked.set()
        await_(release.wait())

    async with database.writer_engine.connect() as connection:
        await connection.begin()
        await connection.exec_driver_sql(
            "INSERT INTO custody_child VALUES (17)"
            if deferred_failure
            else "INSERT INTO custody_parent VALUES (17)"
        )
        original = OriginalCustodyTransaction(connection, binding())
        event.listen(connection.sync_connection, operation, park_original)
        task = asyncio.create_task(
            original.commit() if operation == "commit" else original.rollback()
        )

        try:
            await asyncio.wait_for(parked.wait(), CONDITION_DEADLINE_SECONDS)
            for _ in range(2):
                task.cancel()
                turn = asyncio.Event()
                asyncio.get_running_loop().call_soon(turn.set)
                await asyncio.wait_for(turn.wait(), CONDITION_DEADLINE_SECONDS)
                assert not task.done()
                assert original.outcome is None
                assert ledger.outstanding == 1
            with pytest.raises(CustodyUnavailable):
                await original.commit()
            with pytest.raises(CustodyUnavailable):
                await original.rollback()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            event.remove(connection.sync_connection, operation, park_original)
        await connection.rollback()
    async with database.read_transaction() as reader:
        rows = (await reader.exec_driver_sql("SELECT id FROM custody_parent")).all()

    assert original.outcome is (
        CustodyNativeOutcome.UNKNOWN
        if deferred_failure
        else CustodyNativeOutcome.COMMITTED
        if operation == "commit"
        else CustodyNativeOutcome.ABORTED
    )
    assert len(entered) == 1
    assert rows == ([(17,)] if operation == "commit" and not deferred_failure else [])
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_failed_original_commit_stays_unknown_after_rollback_and_close(
    custody_database: Database,
) -> None:
    database = custody_database
    ledger = ConnectionLedger(database)
    async with database.writer_engine.connect() as connection:
        await connection.begin()
        await connection.exec_driver_sql("INSERT INTO custody_child VALUES (17)")
        original = OriginalCustodyTransaction(connection, binding())

        with pytest.raises(IntegrityError):
            await original.commit()
        unresolved = original.outcome
        await connection.rollback()
        with pytest.raises(CustodyUnavailable):
            await original.rollback()
        with pytest.raises(CustodyUnavailable):
            await original.commit()
    async with database.read_transaction() as reader:
        rows = (await reader.exec_driver_sql("SELECT id FROM custody_child")).all()

    assert unresolved is original.outcome is CustodyNativeOutcome.UNKNOWN
    assert rows == []
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit", "rollback"])
async def test_original_completion_never_adopts_replacement_transaction(
    custody_database: Database, operation: str
) -> None:
    database = custody_database
    ledger = ConnectionLedger(database)
    async with database.writer_engine.connect() as connection:
        await connection.begin()
        await connection.exec_driver_sql("INSERT INTO custody_parent VALUES (17)")
        original = OriginalCustodyTransaction(connection, binding())
        await connection.rollback()
        replacement = await connection.begin()
        await connection.exec_driver_sql("INSERT INTO custody_parent VALUES (23)")
        finish = original.commit if operation == "commit" else original.rollback

        with pytest.raises(CustodyUnavailable):
            await finish()
        still_owned = replacement.is_active
        await replacement.rollback()
    async with database.read_transaction() as reader:
        rows = (await reader.exec_driver_sql("SELECT id FROM custody_parent")).all()

    assert original.outcome is CustodyNativeOutcome.UNKNOWN
    assert still_owned
    assert rows == []
    assert ledger.outstanding == 0
