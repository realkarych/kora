import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from traceback import format_exception

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.tables import storage_address_fence
from kora.db.transactions import WriterTransactionError
from kora.engine.graph_preparation_admission import admit_graph_preparation
from kora.engine.locks import (
    LockError,
    StorageAddressContended,
    StorageAddressLockFailed,
    StorageAddressMissing,
    TransitionLocks,
)
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, lock_inventory, wait_for_lock_waiter
from tests.engine.test_graph_preparation_admission import admission_locks
from tests.engine.test_graph_publication_target import seed_target
from tests.engine.test_storage_address_lock import (
    DIGEST,
    OTHER_DIGEST,
    STORE,
    scoped_locks,
)

__all__ = ["kora_capture"]

AddressKey = tuple[str, str]
KEYS: tuple[AddressKey, ...] = (
    (STORE, DIGEST),
    (STORE, OTHER_DIGEST),
    (STORE.lower(), DIGEST),
    (STORE.lower(), OTHER_DIGEST),
)
ABSENT: AddressKey = ("zz-private-missing-address", DIGEST)


class KeyBarrier:
    def __init__(self, key: AddressKey) -> None:
        self._key = key
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    def pause(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        *arguments: object,
    ) -> None:
        if "FROM storage_address_fence" in statement and parameters[1:3] == self._key:
            self.entered.set()
            await_(asyncio.wait_for(self.resume.wait(), DEADLINE))

    @contextmanager
    def installed(self, writer: AsyncConnection) -> Generator[None]:
        event.listen(writer.sync_connection, "before_cursor_execute", self.pause)
        try:
            yield
        finally:
            event.remove(writer.sync_connection, "before_cursor_execute", self.pause)


async def seed_keys(database: Database) -> None:
    async with database.transaction() as writer:
        await writer.execute(
            storage_address_fence.insert(),
            [
                {
                    "store_id": store,
                    "payload_digest": digest,
                    "last_deletion_generation": 0,
                }
                for store, digest in KEYS
            ],
        )


async def acquire_set(
    database: Database,
    keys: tuple[AddressKey, ...],
    scope: str,
    barrier: KeyBarrier | None = None,
) -> None:
    async with database.transaction() as writer:
        locks = await scoped_locks(writer, scope)
        if barrier is None:
            await locks.acquire_existing_storage_addresses(
                keys, max_addresses=len(keys)
            )
        else:
            with barrier.installed(writer):
                await locks.acquire_existing_storage_addresses(
                    keys, max_addresses=len(keys)
                )
        for address in keys:
            locks.require_existing_storage_address(writer, *address)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [5, 1 << 64])
async def test_existing_address_set_acquires_each_exact_key_once_in_byte_order(
    database: Database, bound: int
) -> None:
    await seed_keys(database)
    ledger = ConnectionLedger(database)
    supplied = (KEYS[3], KEYS[1], KEYS[0], KEYS[2], KEYS[0])
    postgres = database.dialect is Dialect.POSTGRESQL

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        with executed_queries(writer) as premature:
            with pytest.raises(LockError, match="once after scopes"):
                await locks.acquire_existing_storage_addresses(
                    supplied, max_addresses=bound
                )
        await locks.acquire_scopes(())
        checkouts = ledger.checkouts
        with executed_queries(writer) as acquired:
            await locks.acquire_existing_storage_addresses(
                supplied, max_addresses=bound
            )
        later_checkouts = ledger.checkouts
        with executed_queries(writer) as refused:
            for address in KEYS:
                locks.require_existing_storage_address(writer, *address)
            for missing in (ABSENT, (STORE.upper(), DIGEST)):
                with pytest.raises(LockError, match="not held"):
                    locks.require_existing_storage_address(writer, *missing)
            with pytest.raises(LockError, match="once after scopes"):
                await locks.acquire_existing_storage_address(*ABSENT)
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.acquire_existing_storage_addresses((), max_addresses=0)
                )
        async with database.read_transaction() as reader:
            with pytest.raises(LockError, match="does not own"):
                locks.require_existing_storage_address(reader, *KEYS[0])
    with pytest.raises(WriterTransactionError, match="not current"):
        locks.require_existing_storage_address(writer, *KEYS[0])

    assert premature == refused == []
    assert checkouts == later_checkouts
    assert len(acquired) == len(KEYS) + int(postgres)
    if postgres:
        assert acquired[0] == (
            "LOCK TABLE storage_address_fence IN ROW SHARE MODE NOWAIT",
            (),
        )
    assert [parameters[1:3] for _, parameters in acquired[int(postgres) :]] == list(
        KEYS
    )
    for statement, parameters in acquired[int(postgres) :]:
        assert parameters[0] == 1
        assert "WHERE storage_address_fence.store_id =" in statement
        assert "AND storage_address_fence.payload_digest =" in statement
        assert ("FOR UPDATE NOWAIT" in statement) is postgres
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_empty_address_set_completes_phase_without_sql_or_membership(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)

    async with database.transaction() as writer:
        locks = await admission_locks(writer, binding, selected)
        with executed_queries(writer) as observed:
            await locks.acquire_existing_storage_addresses((), max_addresses=0)
            with pytest.raises(LockError, match="not held"):
                locks.require_existing_storage_address(writer, *KEYS[0])
            with pytest.raises(LockError, match="once after scopes"):
                await locks.acquire_existing_storage_addresses((), max_addresses=0)
            with pytest.raises(LockError, match="once after scopes"):
                await locks.acquire_existing_storage_address(*KEYS[0])
            with pytest.raises(LockError, match="precedes storage addresses"):
                await locks.read_graph_publication_target(writer, binding, selected)
            with pytest.raises(LockError, match="precedes storage addresses"):
                await admit_graph_preparation(writer, locks, binding, selected, 1)

    assert observed == []
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supplied", "bound"),
    [
        ((KEYS[0], KEYS[0]), 1),
        ((KEYS[0], ("invalid store", DIGEST)), 2),
        ((KEYS[0], (STORE,)), 2),
        ((), True),
        ((), -1),
    ],
)
async def test_address_set_refuses_invalid_raw_inventory_before_sql(
    database: Database, supplied: tuple[AddressKey, ...], bound: int
) -> None:
    async with database.transaction() as writer:
        locks = await scoped_locks(writer, "invalid")

        with executed_queries(writer) as observed:
            with pytest.raises(LockError, match="invalid storage address"):
                await locks.acquire_existing_storage_addresses(
                    supplied, max_addresses=bound
                )
            with pytest.raises(LockError, match="cannot be used"):
                await locks.acquire_existing_storage_address(*KEYS[0])

        assert observed == []


@pytest.mark.asyncio
async def test_missing_address_poisoning_prevents_use_of_a_real_earlier_member(
    database: Database,
) -> None:
    await seed_keys(database)
    ledger = ConnectionLedger(database)
    statements: list[tuple[str, tuple[object, ...]]] = []

    async def attempt_partial_set() -> None:
        async with database.transaction() as writer:
            locks = await scoped_locks(writer, "missing")
            with executed_queries(writer) as observed:
                try:
                    await locks.acquire_existing_storage_addresses(
                        (ABSENT, KEYS[0]), max_addresses=2
                    )
                except StorageAddressMissing:
                    with pytest.raises(LockError, match="cannot be used"):
                        locks.require_existing_storage_address(writer, *KEYS[0])
                    statements.extend(observed)
                    raise

    with pytest.raises(StorageAddressMissing):
        await attempt_partial_set()
    await acquire_set(database, KEYS, "after_missing")

    assert [
        args[1:3] for sql, args in statements if "FROM storage_address_fence" in sql
    ] == [KEYS[0], ABSENT]
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_late_address_contention_releases_the_earlier_native_lock(
    database: Database,
) -> None:
    await seed_keys(database)
    ledger = ConnectionLedger(database)
    early, late = KEYS[0], KEYS[-1]
    postgres = database.dialect is Dialect.POSTGRESQL
    barrier = KeyBarrier(late) if postgres else None
    waiter: asyncio.Task[None] | None = None

    try:
        async with database.transaction() as holder:
            locks = await scoped_locks(holder, "late_holder")
            await locks.acquire_existing_storage_address(*late)
            waiter = asyncio.create_task(
                acquire_set(database, (late, early), "waiter", barrier)
            )
            if barrier is None:
                await wait_for_lock_waiter(database)
                assert ledger.outstanding == 1
                assert not waiter.done()
            else:
                await asyncio.wait_for(barrier.entered.wait(), DEADLINE)
                with pytest.raises(StorageAddressContended):
                    await acquire_set(database, (early,), "first_key_observer")
                barrier.resume.set()
                with pytest.raises(StorageAddressContended):
                    await asyncio.wait_for(waiter, DEADLINE)
                await acquire_set(database, (early,), "after_refusal")
                locks.require_existing_storage_address(holder, *late)
        if barrier is None:
            await asyncio.wait_for(waiter, DEADLINE)
    finally:
        if barrier is not None:
            barrier.resume.set()
        if waiter is not None:
            if not waiter.done():
                waiter.cancel()
            await asyncio.wait_for(
                asyncio.gather(waiter, return_exceptions=True), DEADLINE
            )

    assert waiter is not None
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_address_set_cancellation_releases_the_real_earlier_member(
    database: Database,
) -> None:
    await seed_keys(database)
    ledger = ConnectionLedger(database)
    early, late = KEYS[0], KEYS[-1]
    barrier = KeyBarrier(late)

    task = asyncio.create_task(acquire_set(database, (late, early), "cancel", barrier))
    try:
        await asyncio.wait_for(barrier.entered.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    await acquire_set(database, KEYS, "after_cancellation")

    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_address_set_native_failure_is_content_free_and_releases_writer(
    database: Database, kora_capture: Capture
) -> None:
    await seed_keys(database)
    ledger = ConnectionLedger(database)

    async def unavailable_fence() -> None:
        async with database.transaction() as writer:
            locks = await scoped_locks(writer, "unavailable")
            await writer.exec_driver_sql(
                "ALTER TABLE storage_address_fence RENAME TO unavailable_address_fence"
            )
            await locks.acquire_existing_storage_addresses(
                (ABSENT, KEYS[0]), max_addresses=2
            )

    with pytest.raises(StorageAddressLockFailed) as refused:
        await unavailable_fence()
    await acquire_set(database, KEYS, "after_statement_failure")
    records = [
        record
        for record in kora_capture.finish()
        if record["event"] == "db.transaction.failed"
    ]
    rendered = "".join(format_exception(refused.value))

    assert refused.value.__cause__ is refused.value.__context__ is None
    assert len(records) == 1
    for private in (
        STORE,
        ABSENT[0],
        DIGEST,
        "unavailable_address_fence",
        "UndefinedTable",
    ):
        assert private not in rendered
        assert private not in repr(records)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
