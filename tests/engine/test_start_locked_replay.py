import asyncio
from traceback import walk_tb
from typing import Literal

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.conformance.capture import captured
from kora.db.connection import Database, Dialect
from kora.db.tables import inbound_event
from kora.db.transactions import WriterTransactionError, managed_writer
from kora.engine.locks import COMPONENT, LockError, Scope, ScopeKind, TransitionLocks
from kora.engine.start_capacity import manual_start_scopes
from kora.engine.start_records import PreparedManualStartRequest, StartRecordRejected
from kora.engine.start_records import prepare_manual_start_request as prepare_request
from kora.engine.start_replay import (
    ReplayOutcome,
    StartReadFailed,
    StartReplay,
    recheck_locked_manual_start_receipt,
    replay_manual_start,
)
from kora.engine.writer_mode import WRITABLE_POSTGRES_MODE
from kora.observability import Level, LoggingSettings
from kora.protocols.authentication import Principal
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock
from tests.engine.lock_support import (
    DEADLINE,
    RUN_A,
    RUN_B,
    DeliberateRollback,
    lock_inventory,
    release_and_join,
    wait_for_lock_waiter,
)
from tests.engine.start_support import KEY, PRIVATE, ReplayScenario, replay_scenario
from tests.engine.test_start_replay import observed_statements

NEW_KEY = "manual-locked-race-key"
MISSING = StartReplay(ReplayOutcome.ABSENT)


def request_for(
    scenario: ReplayScenario, *, same_digest: bool = True
) -> PreparedManualStartRequest:
    return prepare_request(
        scenario.identity.namespace_id,
        scenario.identity.graph_id,
        {"private": PRIVATE} if same_digest else {},
    )


def scopes_for(scenario: ReplayScenario) -> tuple[Scope, ...]:
    return manual_start_scopes(
        scenario.identity.namespace_id, scenario.identity.graph_id
    )


async def acquire_locks(
    connection: AsyncConnection, run_id: str, scopes: tuple[Scope, ...]
) -> TransitionLocks:
    locks = TransitionLocks(connection)
    await locks.acquire_run(run_id)
    await locks.acquire_scopes(scopes)
    return locks


def expected_queries(database: Database, queries: list[str]) -> None:
    expected_mode = (
        [WRITABLE_POSTGRES_MODE.text] if database.dialect is Dialect.POSTGRESQL else []
    )
    assert queries[:-1] == expected_mode
    assert queries[-1].startswith("SELECT inbound_event.start_outcome ")
    assert "FROM inbound_event" in queries[-1]
    assert "graph_version" not in queries[-1]
    assert "payload" not in queries[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["accepted", "skipped", "conflict", "rollback"])
async def test_locked_replay_rechecks_after_a_competing_writer(
    database: Database, ending: str
) -> None:
    scenario = await replay_scenario(database, skipped=ending == "skipped")
    request = request_for(scenario, same_digest=ending != "conflict")
    scopes = scopes_for(scenario)
    before = await scenario.snapshot()
    ledger = ConnectionLedger(database)
    ready, release = asyncio.Event(), asyncio.Event()
    seen: list[StartReplay] = []
    statements: list[str] = []
    async with database.read_snapshot() as reader:
        detached = await replay_manual_start(
            reader, scenario.principal, request.identity, NEW_KEY
        )

    async def owner() -> None:
        try:
            async with database.transaction() as connection:
                await acquire_locks(connection, RUN_A, scopes)
                await connection.execute(
                    update(inbound_event)
                    .where(inbound_event.c.id == scenario.receipt_id)
                    .values(idempotency_key=NEW_KEY)
                )
                ready.set()
                await asyncio.wait_for(release.wait(), DEADLINE)
                if ending == "rollback":
                    raise DeliberateRollback()
        except DeliberateRollback:
            assert ending == "rollback"

    async def contender() -> None:
        async with database.transaction() as connection:
            if database.dialect is Dialect.POSTGRESQL:
                prior = await replay_manual_start(
                    connection, scenario.principal, request.identity, NEW_KEY
                )
                assert prior == MISSING
            locks = await acquire_locks(connection, RUN_B, scopes)
            async with observed_statements(connection) as queries:
                seen.append(
                    await recheck_locked_manual_start_receipt(
                        connection, locks, scenario.principal, request, NEW_KEY, RUN_B
                    )
                )
            statements.extend(queries)

    winner = asyncio.create_task(owner())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    follower = asyncio.create_task(contender())
    try:
        await wait_for_lock_waiter(database)
        assert not follower.done()
    finally:
        await release_and_join(release, winner, follower)
    after = await scenario.snapshot()

    assert detached == MISSING
    if ending == "rollback":
        assert seen == [MISSING]
        assert after == before
    else:
        expected = (
            StartReplay(ReplayOutcome.MISMATCHED)
            if ending == "conflict"
            else StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
        )
        assert seen == [expected]
        assert after == before | {
            "inbound_event": [before["inbound_event"][0] | {"idempotency_key": NEW_KEY}]
        }
    expected_queries(database, statements)
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["global", "namespace", "graph", "foreign_namespace", "foreign_graph", "run"],
)
async def test_locked_replay_requires_each_exact_lock(
    database: Database, fault: str
) -> None:
    scenario = await replay_scenario(database)
    request = request_for(scenario)
    scopes = scopes_for(scenario)
    if fault == "global":
        scopes = scopes[1:]
    elif fault == "namespace":
        scopes = (scopes[0], scopes[2])
    elif fault == "graph":
        scopes = scopes[:2]
    elif fault == "foreign_namespace":
        scopes = (scopes[0], Scope(ScopeKind.NAMESPACE, (RUN_A,)), scopes[2])
    elif fault == "foreign_graph":
        scopes = (*scopes[:2], Scope(ScopeKind.GRAPH, (RUN_A,)))

    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes)
        async with observed_statements(writer) as queries:
            with pytest.raises(LockError, match="required transaction locks"):
                await recheck_locked_manual_start_receipt(
                    writer,
                    locks,
                    scenario.principal,
                    request,
                    KEY,
                    RUN_B if fault == "run" else RUN_A,
                )

    assert queries == []
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_locked_replay_rejects_foreign_connection_and_expired_lease(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    request = request_for(scenario)

    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes_for(scenario))
        async with database.read_transaction() as other:
            async with observed_statements(other) as queries:
                with pytest.raises(LockError, match="does not own the locks"):
                    await recheck_locked_manual_start_receipt(
                        other, locks, scenario.principal, request, KEY, RUN_A
                    )
    with pytest.raises(WriterTransactionError, match="not current"):
        await recheck_locked_manual_start_receipt(
            writer, locks, scenario.principal, request, KEY, RUN_A
        )

    assert queries == []
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_locked_replay_preserves_grant_and_key_precedence(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    request = request_for(scenario)
    denied = Principal.model_validate(
        scenario.principal.model_dump() | {"namespaces": ()}
    )
    second = Principal.model_validate(
        scenario.principal.model_dump() | {"token_id": RUN_A, "owner": RUN_B}
    )
    bad_key = "private-key\0"

    async with database.transaction() as writer:
        locks = await acquire_locks(
            writer, RUN_A, (*scopes_for(scenario), Scope(ScopeKind.RUNTIME, ("local",)))
        )
        async with observed_statements(writer) as denied_queries:
            refused = await recheck_locked_manual_start_receipt(
                writer, locks, denied, request, bad_key, RUN_A
            )
            with pytest.raises(StartRecordRejected) as invalid:
                await recheck_locked_manual_start_receipt(
                    writer, locks, scenario.principal, request, bad_key, RUN_A
                )
        async with observed_statements(writer) as accepted_queries:
            replay = await recheck_locked_manual_start_receipt(
                writer, locks, second, request, KEY, RUN_A
            )
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(invalid.value.__traceback__)
    }

    assert refused == StartReplay(ReplayOutcome.REFUSED)
    assert replay == StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
    assert denied_queries == []
    expected_queries(database, accepted_queries)
    assert {"request", "identity", "key", "principal"}.isdisjoint(
        frames["recheck_locked_manual_start_receipt"]
    )


@pytest.mark.asyncio
async def test_locked_replay_refuses_corrupt_receipt_without_other_reads(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    request = request_for(scenario)
    async with database.transaction() as writer:
        await writer.execute(
            update(inbound_event).values(start_outcome={"private": PRIVATE})
        )
    before = await scenario.snapshot()

    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes_for(scenario))
        async with observed_statements(writer) as queries:
            with pytest.raises(StartRecordRejected) as invalid:
                await recheck_locked_manual_start_receipt(
                    writer, locks, scenario.principal, request, KEY, RUN_A
                )
    after = await scenario.snapshot()

    assert PRIVATE not in str(invalid.value)
    expected_queries(database, queries)
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readonly", "repeatable", "autocommit"])
async def test_locked_replay_refuses_unsupported_postgres_writer_mode(
    database: Database, mode: Literal["readonly", "repeatable", "autocommit"]
) -> None:
    scenario = await replay_scenario(database)
    request = request_for(scenario)
    scopes = scopes_for(scenario)
    ledger = ConnectionLedger(database)
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            locks = await acquire_locks(writer, RUN_A, scopes)
            async with observed_statements(writer) as queries:
                result = await recheck_locked_manual_start_receipt(
                    writer, locks, scenario.principal, request, KEY, RUN_A
                )

        assert result == StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
        expected_queries(database, queries)
        assert ledger.outstanding == database.units_in_flight() == 0
        return

    async def assert_mode_refusal(writer: AsyncConnection) -> None:
        locks = await acquire_locks(writer, RUN_A, scopes)
        async with observed_statements(writer) as queries:
            with pytest.raises(
                StartReadFailed, match="manual start metadata read failed"
            ):
                await recheck_locked_manual_start_receipt(
                    writer, locks, scenario.principal, request, KEY, RUN_A
                )
        assert queries == (
            [] if mode == "autocommit" else [WRITABLE_POSTGRES_MODE.text]
        )

    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "locked-start-test",
        FakeClock().now,
    ) as capture:
        if mode == "autocommit":
            async with database.writer_engine.connect() as writer:
                await writer.execution_options(isolation_level="AUTOCOMMIT")
                async with writer.begin():
                    with managed_writer(writer):
                        await assert_mode_refusal(writer)
                        assert await lock_inventory(database) == []
        else:
            async with database.transaction() as writer:
                if mode == "readonly":
                    await writer.execute(text("SET TRANSACTION READ ONLY"))
                else:
                    await writer.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    )
                await assert_mode_refusal(writer)
        records = capture.finish()
    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes)
        reused = await recheck_locked_manual_start_receipt(
            writer, locks, scenario.principal, request, KEY, RUN_A
        )

    assert reused == StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
    assert not any(
        record["event"] == "engine.start_replay.checked" for record in records
    )
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_locked_replay_mode_query_failure_releases_the_writer(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    request = request_for(scenario)
    scopes = scopes_for(scenario)
    ledger = ConnectionLedger(database)
    if database.dialect is Dialect.SQLITE:
        async with database.transaction() as writer:
            locks = await acquire_locks(writer, RUN_A, scopes)
            async with observed_statements(writer) as queries:
                result = await recheck_locked_manual_start_receipt(
                    writer, locks, scenario.principal, request, KEY, RUN_A
                )

        assert result == StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
        expected_queries(database, queries)
        assert ledger.outstanding == database.units_in_flight() == 0
        return

    failed_statements: list[str] = []

    async def failed_writer() -> None:
        async with database.transaction() as writer:
            locks = await acquire_locks(writer, RUN_A, scopes)
            with pytest.raises(DBAPIError):
                await writer.execute(text("SELECT 1 / 0"))
            async with observed_statements(writer) as queries:
                try:
                    await recheck_locked_manual_start_receipt(
                        writer, locks, scenario.principal, request, KEY, RUN_A
                    )
                finally:
                    failed_statements.extend(queries)

    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "locked-start-test",
        FakeClock().now,
    ) as capture:
        with pytest.raises(
            StartReadFailed, match="manual start metadata read failed"
        ) as error:
            await failed_writer()
        records = capture.finish()
        raw_output = capture.reader.data
    assert ledger.outstanding == database.units_in_flight() == 0
    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_B, scopes)
        result = await recheck_locked_manual_start_receipt(
            writer, locks, scenario.principal, request, KEY, RUN_B
        )

    assert failed_statements == [WRITABLE_POSTGRES_MODE.text]
    assert not any(
        record["event"] == "engine.start_replay.checked" for record in records
    )
    assert sum(record["event"] == "db.transaction.failed" for record in records) == 1
    assert "InFailedSQLTransaction" not in str(error.value)
    for marker in (KEY, PRIVATE, "transaction_read_only", "InFailedSQLTransaction"):
        assert marker.encode() not in raw_output
    assert result == StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
