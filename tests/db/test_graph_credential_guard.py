import asyncio
from datetime import datetime
from traceback import format_exception

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.tables import api_token
from kora.db.tokens import (
    REVOKED,
    GraphCredentialGuardFailed,
    GraphCredentialGuardRejected,
    acquire_graph_credential_guard,
    revoke_token,
)
from kora.db.transactions import WriterTransactionError
from tests.db.closing import ConnectionLedger, until
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries

__all__ = ["kora_capture"]

DEADLINE: float = 10.0
ABSENT_ID: str = "00000000000000000000000000"


class DeliberateRollback(RuntimeError):
    pass


async def seed_tokens(database: Database) -> tuple[str, str]:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        return await seeder.token("1" * 64), await seeder.token("2" * 64)


async def read_expiry(writer: AsyncConnection, token_id: str) -> datetime | None:
    expiry: object = await writer.scalar(
        select(api_token.c.expires_at).where(api_token.c.id == token_id)
    )
    assert expiry is None or isinstance(expiry, datetime)
    return expiry


async def postgres_pid(writer: AsyncConnection, dialect: Dialect) -> int | None:
    if dialect is Dialect.SQLITE:
        return None
    pid: object = await writer.scalar(text("SELECT pg_backend_pid()"))
    assert type(pid) is int
    return pid


async def wait_for_blocked_writer(
    database: Database, blocker: int | None, waiter: asyncio.Future[int | None]
) -> None:
    if database.dialect is Dialect.SQLITE:
        await until(lambda: database.units_in_flight() == 2)
        assert not waiter.done()
        return
    async with asyncio.timeout(DEADLINE):
        pid = await waiter
        assert blocker is not None
        assert pid is not None
        async with database.read_transaction() as observer:
            while not await observer.scalar(
                text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                {"blocker": blocker, "waiter": pid},
            ):
                pass


@pytest.mark.asyncio
async def test_graph_credential_guard_uses_one_fixed_primary_key_probe(
    database: Database,
) -> None:
    token_id, _ = await seed_tokens(database)
    ledger = ConnectionLedger(database)
    sqlite = database.dialect is Dialect.SQLITE
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        await writer.execute(
            api_token.insert(),
            [
                {
                    "id": seeder.new_id(),
                    "kind": "user",
                    "digest": f"{index:064x}",
                    "owner": ABSENT_ID,
                    "namespaces": [],
                    "approve": False,
                    "platform": False,
                    "runtime": None,
                    "expires_at": None,
                    **seeder.stamps(),
                }
                for index in range(1024)
            ],
        )
        await writer.exec_driver_sql("ANALYZE api_token")

    async with database.transaction() as writer:
        before = ledger.checkouts
        with executed_queries(writer) as probes:
            await acquire_graph_credential_guard(writer, token_id)
            with pytest.raises(GraphCredentialGuardRejected):
                await acquire_graph_credential_guard(writer, ABSENT_ID)
        after = ledger.checkouts
        prefix = "EXPLAIN QUERY PLAN " if sqlite else "EXPLAIN (COSTS OFF) "
        plans = [
            str((await writer.exec_driver_sql(prefix + sql, arguments)).all())
            for sql, arguments in probes
        ]

    assert len(probes) == 2
    for (statement, arguments), expected_id in zip(
        probes, (token_id, ABSENT_ID), strict=True
    ):
        assert arguments == (expected_id,)
        assert statement.split()[:7] == [
            "SELECT",
            "1",
            "FROM",
            "api_token",
            "WHERE",
            "api_token.id",
            "=",
        ]
        assert ("FOR SHARE OF api_token" in statement) is not sqlite
    assert before == after
    if sqlite:
        assert all("SEARCH api_token USING COVERING INDEX" in plan for plan in plans)
    else:
        assert all("Index Scan using pk_api_token" in plan for plan in plans)
        assert all("LockRows" in plan for plan in plans)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_credential_guard_refuses_noncurrent_writer_and_noncanonical_id(
    database: Database,
) -> None:
    token_id, _ = await seed_tokens(database)

    async with database.transaction() as writer:
        with executed_queries(writer) as writer_queries:
            for invalid in ("wrong", "8" + ABSENT_ID[1:]):
                with pytest.raises(GraphCredentialGuardRejected):
                    await acquire_graph_credential_guard(writer, invalid)
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    acquire_graph_credential_guard(writer, token_id)
                )
    with pytest.raises(WriterTransactionError, match="managed writer"):
        await acquire_graph_credential_guard(writer, token_id)
    async with database.read_transaction() as reader:
        with executed_queries(reader) as reader_queries:
            with pytest.raises(WriterTransactionError, match="managed writer"):
                await acquire_graph_credential_guard(reader, token_id)

    assert writer_queries == reader_queries == []
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_guard", [False, True])
async def test_graph_credential_guard_precedes_effective_revocation(
    database: Database, rollback_guard: bool
) -> None:
    token_id, disjoint_id = await seed_tokens(database)
    ledger = ConnectionLedger(database)
    waiter_pid: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
    revoker: asyncio.Task[None] | None = None

    async def revoke() -> None:
        async with database.transaction() as writer:
            waiter_pid.set_result(await postgres_pid(writer, database.dialect))
            await revoke_token(writer, token_id)

    async def hold_guard() -> None:
        nonlocal revoker
        async with database.transaction() as holder:
            blocker = await postgres_pid(holder, database.dialect)
            await acquire_graph_credential_guard(holder, token_id)
            if blocker is not None:
                async with asyncio.timeout(DEADLINE), database.transaction() as peer:
                    await acquire_graph_credential_guard(peer, token_id)
                    await revoke_token(peer, disjoint_id)
            revoker = asyncio.create_task(revoke())
            await wait_for_blocked_writer(database, blocker, waiter_pid)
            assert not revoker.done()
            assert await read_expiry(holder, token_id) is None
            if rollback_guard:
                raise DeliberateRollback()

    try:
        if rollback_guard:
            with pytest.raises(DeliberateRollback):
                await hold_guard()
        else:
            await hold_guard()
    finally:
        if revoker is not None:
            await asyncio.wait_for(revoker, DEADLINE)
    async with database.transaction() as writer:
        expiry = await read_expiry(writer, token_id)
        disjoint_expiry = await read_expiry(writer, disjoint_id)

    assert expiry == REVOKED
    assert disjoint_expiry == (
        REVOKED if database.dialect is Dialect.POSTGRESQL else None
    )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["commit", "rollback", "cancel_waiter"])
async def test_graph_credential_guard_observes_the_revocation_outcome_after_wait(
    database: Database, outcome: str
) -> None:
    token_id, _ = await seed_tokens(database)
    ledger = ConnectionLedger(database)
    waiter_pid: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
    waiter: asyncio.Task[datetime | None] | None = None

    async def acquire_then_read() -> datetime | None:
        async with database.transaction() as writer:
            waiter_pid.set_result(await postgres_pid(writer, database.dialect))
            await acquire_graph_credential_guard(writer, token_id)
            return await read_expiry(writer, token_id)

    async def hold_revocation() -> None:
        nonlocal waiter
        async with database.transaction() as holder:
            blocker = await postgres_pid(holder, database.dialect)
            await revoke_token(holder, token_id)
            waiter = asyncio.create_task(acquire_then_read())
            await wait_for_blocked_writer(database, blocker, waiter_pid)
            assert not waiter.done()
            if outcome == "cancel_waiter":
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(waiter, DEADLINE)
                assert ledger.outstanding == database.units_in_flight() == 1
            if outcome != "commit":
                raise DeliberateRollback()

    observed: datetime | None = None
    try:
        if outcome == "commit":
            await hold_revocation()
        else:
            with pytest.raises(DeliberateRollback):
                await hold_revocation()
    finally:
        if waiter is not None and not waiter.cancelled():
            observed = await asyncio.wait_for(waiter, DEADLINE)
    async with asyncio.timeout(DEADLINE), database.transaction() as writer:
        durable_expiry = await read_expiry(writer, token_id)
        await acquire_graph_credential_guard(writer, token_id)
        await revoke_token(writer, token_id)

    assert observed == durable_expiry == (REVOKED if outcome == "commit" else None)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_graph_credential_guard_failure_keeps_native_details_private(
    database: Database, kora_capture: Capture
) -> None:
    token_id, _ = await seed_tokens(database)
    ledger = ConnectionLedger(database)

    async def acquire_after_rename() -> None:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE api_token RENAME TO unavailable_api_token"
            )
            await acquire_graph_credential_guard(writer, token_id)

    with pytest.raises(
        GraphCredentialGuardFailed, match="graph credential guard failed"
    ) as refused:
        await acquire_after_rename()
    async with database.transaction() as writer:
        await acquire_graph_credential_guard(writer, token_id)
    rendered = "".join(format_exception(refused.value))
    records = [
        record
        for record in kora_capture.finish()
        if record["event"] == "db.transaction.failed"
    ]

    assert refused.value.__cause__ is refused.value.__context__ is None
    assert len(records) == 1
    for private in (token_id, "unavailable_api_token", "SELECT 1", "UndefinedTable"):
        assert private not in rendered
        assert private not in repr(records)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
