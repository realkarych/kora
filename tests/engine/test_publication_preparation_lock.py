import asyncio
from dataclasses import replace
from traceback import format_exception

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationState,
    read_graph_preparation_settlement,
)
from kora.db.tables import graph, graph_deployment_preparation
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    GraphPreparationLockContended,
    GraphPreparationLockFailed,
    GraphPreparationLockRejected,
    LockError,
    Scope,
    ScopeKind,
    StorageAddressMissing,
    TransitionLocks,
)
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_credential_guard import postgres_pid, wait_for_blocked_writer
from tests.db.test_graph_preparation_reads import seed_preparation
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import (
    DEADLINE,
    DeliberateRollback,
    lock_inventory,
    wait_for_lock_waiter,
)
from tests.engine.test_graph_publication_target import publication_scopes
from tests.engine.test_storage_address_set import ABSENT, KEYS, AddressKey, seed_keys

__all__ = ["kora_capture"]

ADDRESSES: tuple[AddressKey, ...] = KEYS[:2]


async def seed_original(
    database: Database,
    *,
    bound: bool = False,
    state: GraphPreparationState = "open",
    namespace_name: str = "original",
) -> tuple[GraphPreparationBinding, str]:
    async with database.transaction() as writer:
        binding, _ = await seed_preparation(
            writer, state, bound=bound, namespace_name=namespace_name
        )
        selected: object = await writer.scalar(
            select(graph.c.id).where(
                graph.c.namespace_id == binding.namespace_id,
                graph.c.name == binding.graph_name,
            )
        )
        assert type(selected) is str
        return binding, selected


async def lock_publication(
    database: Database,
    binding: GraphPreparationBinding,
    selected: str,
    addresses: tuple[AddressKey, ...] = (),
) -> None:
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(publication_scopes(binding, selected))
        await locks.acquire_existing_storage_addresses(
            addresses, max_addresses=len(addresses)
        )
        await locks.acquire_publication_graph_preparation(writer, binding, selected)
        locks.require_publication_graph_preparation(writer, binding, selected)


async def terminal_update(
    writer: AsyncConnection, binding: GraphPreparationBinding
) -> None:
    await writer.execute(
        graph_deployment_preparation.update()
        .where(graph_deployment_preparation.c.id == binding.preparation_id)
        .values(state="closed_without_publication")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("with_addresses", [False, True])
async def test_publication_preparation_guard_pins_selection_after_address_phase(
    database: Database, bound: bool, with_addresses: bool
) -> None:
    binding, selected = await seed_original(database, bound=bound)
    other, other_graph = await seed_original(database, namespace_name="other")
    await seed_keys(database)
    addresses = ADDRESSES if with_addresses else ()
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(
            (
                *publication_scopes(binding, selected),
                Scope(ScopeKind.GRAPH, (other_graph,)),
            )
        )
        with executed_queries(writer) as early:
            with pytest.raises(LockError, match="completed address phase"):
                await locks.acquire_publication_graph_preparation(
                    writer, binding, selected
                )
        await locks.acquire_existing_storage_addresses(
            addresses, max_addresses=len(addresses)
        )
        with executed_queries(writer) as preflight:
            with pytest.raises(LockError, match="required transaction locks"):
                await locks.acquire_publication_graph_preparation(
                    writer, other, other_graph
                )
            if bound:
                with pytest.raises(LockError, match="differs from the bound graph"):
                    await locks.acquire_publication_graph_preparation(
                        writer, binding, other_graph
                    )
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.acquire_publication_graph_preparation(
                        writer, binding, selected
                    )
                )
        async with database.read_transaction() as reader:
            with executed_queries(reader) as foreign:
                with pytest.raises(LockError, match="does not own"):
                    await locks.acquire_publication_graph_preparation(
                        reader, binding, selected
                    )
        before = ledger.checkouts
        with executed_queries(writer) as acquired:
            await locks.acquire_publication_graph_preparation(writer, binding, selected)
        after = ledger.checkouts
        with executed_queries(writer) as required:
            locks.require_publication_graph_preparation(writer, binding, selected)
            with pytest.raises(LockError, match="not held"):
                locks.require_publication_graph_preparation(
                    writer, replace(binding, graph_name="different"), selected
                )
            if not bound:
                with pytest.raises(LockError, match="not held"):
                    locks.require_publication_graph_preparation(
                        writer, binding, other_graph
                    )
            with pytest.raises(LockError, match="acquired once"):
                await locks.acquire_publication_graph_preparation(
                    writer, binding, selected
                )
            for address in addresses:
                locks.require_open_graph_preparation(writer, *address, binding)
                with pytest.raises(LockError, match="acquired once"):
                    await locks.acquire_open_graph_preparation(
                        writer, *address, binding
                    )
    with pytest.raises(WriterTransactionError, match="not current"):
        locks.require_publication_graph_preparation(writer, binding, selected)

    assert early == preflight == foreign == required == []
    assert before == after
    postgres = database.dialect is Dialect.POSTGRESQL
    assert len(acquired) == 1 + int(postgres)
    assert "WHERE graph_deployment_preparation.id =" in acquired[-1][0]
    assert binding.preparation_id in acquired[-1][1]
    assert ("FOR UPDATE NOWAIT" in acquired[-1][0]) is postgres
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_stage_only_and_failed_address_phase_cannot_satisfy_publication_guard(
    database: Database,
) -> None:
    binding, selected = await seed_original(database)
    await seed_keys(database)

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(publication_scopes(binding, selected))
        await locks.acquire_existing_storage_address(*ADDRESSES[0])
        await locks.acquire_open_graph_preparation(writer, *ADDRESSES[0], binding)
        with executed_queries(writer) as stage_only:
            locks.require_open_graph_preparation(writer, *ADDRESSES[0], binding)
            with pytest.raises(LockError, match="not held"):
                locks.require_publication_graph_preparation(writer, binding, selected)
            with pytest.raises(LockError, match="acquired once"):
                await locks.acquire_publication_graph_preparation(
                    writer, binding, selected
                )

    async def attempt_partial() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes(publication_scopes(binding, selected))
            try:
                await locks.acquire_existing_storage_addresses(
                    (ABSENT, ADDRESSES[0]), max_addresses=2
                )
            except StorageAddressMissing:
                with executed_queries(writer) as rejected:
                    with pytest.raises(LockError, match="cannot be used"):
                        await locks.acquire_publication_graph_preparation(
                            writer, binding, selected
                        )
                    with pytest.raises(LockError, match="cannot be used"):
                        locks.require_publication_graph_preparation(
                            writer, binding, selected
                        )
                assert rejected == []
                raise

    with pytest.raises(StorageAddressMissing):
        await attempt_partial()
    await lock_publication(database, binding, selected, ADDRESSES)

    assert stage_only == []
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_preparation_guard_refuses_absent_settled_or_changed_binding(
    database: Database,
) -> None:
    binding, selected = await seed_original(database)
    settled, settled_graph = await seed_original(
        database, state="closed_without_publication", namespace_name="settled"
    )
    changed = (
        (replace(binding, preparation_id="00000000000000000000000000"), selected),
        (replace(binding, graph_name="different"), selected),
        (replace(binding, deployment_digest="b" * 64), selected),
        (replace(binding, initial_graph_id=selected), selected),
        (replace(binding, namespace_id=settled.namespace_id), settled_graph),
        (settled, settled_graph),
    )

    for candidate, graph_id in changed:
        with pytest.raises(GraphPreparationLockRejected):
            await lock_publication(database, candidate, graph_id)
    await lock_publication(database, binding, selected)
    async with database.read_transaction() as reader:
        saved = await read_graph_preparation_settlement(reader, binding)

    assert saved is not None
    assert saved.state == "open"
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_settlement", [False, True])
async def test_publication_preparation_guard_observes_terminal_writer_order(
    database: Database, commit_settlement: bool
) -> None:
    binding, selected = await seed_original(database)
    ledger = ConnectionLedger(database)
    ready = asyncio.Event()
    release = asyncio.Event()
    contender: asyncio.Task[None] | None = None

    async def settle_first() -> None:
        async with database.transaction() as writer:
            await terminal_update(writer, binding)
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if not commit_settlement:
                raise DeliberateRollback()

    holder = asyncio.create_task(settle_first())
    try:
        await asyncio.wait_for(ready.wait(), DEADLINE)
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(GraphPreparationLockContended):
                await lock_publication(database, binding, selected)
        else:
            contender = asyncio.create_task(
                lock_publication(database, binding, selected)
            )
            await wait_for_lock_waiter(database)
            assert ledger.outstanding == 1
            assert not contender.done()
    finally:
        release.set()
        if commit_settlement:
            await asyncio.wait_for(holder, DEADLINE)
        else:
            with pytest.raises(DeliberateRollback):
                await asyncio.wait_for(holder, DEADLINE)

    async def observe() -> None:
        if contender is None:
            await lock_publication(database, binding, selected)
        else:
            await asyncio.wait_for(contender, DEADLINE)

    if commit_settlement:
        with pytest.raises(GraphPreparationLockRejected):
            await observe()
    else:
        await observe()
    async with database.read_transaction() as reader:
        result = await read_graph_preparation_settlement(reader, binding)

    assert result is not None
    assert result.state == (
        "closed_without_publication" if commit_settlement else "open"
    )
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_preparation_guard_precedes_a_waiting_terminal_writer(
    database: Database,
) -> None:
    binding, selected = await seed_original(database)
    await seed_keys(database)
    ledger = ConnectionLedger(database)
    waiter_pid: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
    waiter: asyncio.Task[None] | None = None

    async def settle() -> None:
        async with database.transaction() as writer:
            waiter_pid.set_result(await postgres_pid(writer, database.dialect))
            await terminal_update(writer, binding)

    try:
        async with database.transaction() as holder:
            blocker = await postgres_pid(holder, database.dialect)
            locks = TransitionLocks(holder)
            await locks.acquire_scopes(publication_scopes(binding, selected))
            await locks.acquire_existing_storage_addresses(ADDRESSES, max_addresses=2)
            await locks.acquire_publication_graph_preparation(holder, binding, selected)
            waiter = asyncio.create_task(settle())
            await wait_for_blocked_writer(database, blocker, waiter_pid)
            assert not waiter.done()
            locks.require_publication_graph_preparation(holder, binding, selected)
    finally:
        if waiter is not None:
            await asyncio.wait_for(waiter, DEADLINE)
    with pytest.raises(GraphPreparationLockRejected):
        await lock_publication(database, binding, selected, ADDRESSES)

    assert waiter is not None
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_cancelled_publication_preparation_releases_its_addresses_and_writer(
    database: Database,
) -> None:
    binding, selected = await seed_original(database)
    await seed_keys(database)
    ledger = ConnectionLedger(database)
    reading = asyncio.Event()
    unreleased = asyncio.Event()

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if "FROM graph_deployment_preparation" in statement:
            reading.set()
            await_(asyncio.wait_for(unreleased.wait(), DEADLINE))

    async def acquire_until_cancelled() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes(publication_scopes(binding, selected))
            await locks.acquire_existing_storage_addresses(ADDRESSES, max_addresses=2)
            event.listen(writer.sync_connection, "before_cursor_execute", pause)
            try:
                await locks.acquire_publication_graph_preparation(
                    writer, binding, selected
                )
            finally:
                event.remove(writer.sync_connection, "before_cursor_execute", pause)

    task = asyncio.create_task(acquire_until_cancelled())
    try:
        await asyncio.wait_for(reading.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    await lock_publication(database, binding, selected, ADDRESSES)

    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_publication_preparation_native_failure_is_content_free(
    database: Database, kora_capture: Capture
) -> None:
    binding, selected = await seed_original(database)
    ledger = ConnectionLedger(database)
    statements: list[tuple[str, tuple[object, ...]]] = []

    async def unavailable_preparation() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_scopes(publication_scopes(binding, selected))
            await locks.acquire_existing_storage_addresses((), max_addresses=0)
            await writer.exec_driver_sql(
                "ALTER TABLE graph_deployment_preparation "
                "RENAME COLUMN deployment_digest "
                "TO unavailable_publication_preparation_digest"
            )
            with executed_queries(writer) as queries:
                try:
                    await locks.acquire_publication_graph_preparation(
                        writer, binding, selected
                    )
                finally:
                    statements.extend(queries)

    with pytest.raises(GraphPreparationLockFailed) as refused:
        await unavailable_preparation()
    await lock_publication(database, binding, selected)
    rendered = "".join(format_exception(refused.value))
    records = [
        record
        for record in kora_capture.finish()
        if record["event"] == "db.transaction.failed"
    ]

    assert refused.value.__cause__ is refused.value.__context__ is None
    assert len(records) == 1
    [(statement, parameters)] = [
        (sql, args)
        for sql, args in statements
        if "FROM graph_deployment_preparation" in sql
    ]
    assert "graph_deployment_preparation.deployment_digest" in statement
    assert binding.preparation_id in parameters
    assert binding.deployment_digest in parameters
    for private in (
        binding.preparation_id,
        binding.namespace_id,
        binding.graph_name,
        selected,
        binding.deployment_digest,
        "unavailable_publication_preparation_digest",
        "graph_deployment_preparation.deployment_digest",
        "FROM graph_deployment_preparation",
        "UndefinedColumn",
    ):
        assert private not in rendered
        assert private not in repr(records)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
