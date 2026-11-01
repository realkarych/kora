import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Lifecycle
from kora.db.tables import (
    graph_version,
    idempotency_record,
    inbound_event,
    namespace,
    run,
)
from kora.engine.start_records import (
    StartRecordRejected,
    prepare_manual_start,
    start_receipt_document,
)
from kora.engine.start_replay import ReplayOutcome, StartReplay, replay_manual_start
from kora.ir.json import JsonValue
from kora.protocols.authentication import Principal
from tests.db.closing import ConnectionLedger, until
from tests.db.seed import FakeClock, Seeder
from tests.engine.lock_support import DEADLINE, DeliberateRollback
from tests.engine.start_support import KEY, PRIVATE, replay_scenario


@asynccontextmanager
async def observed_statements(
    connection: AsyncConnection,
) -> AsyncGenerator[list[str], None]:
    statements: list[str] = []

    def capture(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        statements.append(statement)

    event.listen(connection.sync_connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("skipped", [False, True])
async def test_manual_start_replay_ignores_current_execution_and_expired_content(
    database: Database, skipped: bool
) -> None:
    scenario = await replay_scenario(database, skipped)
    initial = await scenario.read()
    async with database.transaction() as connection:
        seed = Seeder(connection)
        seed.clock = FakeClock(seed.clock.now())
        seed.clock.advance(172800)
        await connection.execute(
            update(run).values(status="cancelled", finished_at=seed.clock.now())
        )
        await connection.execute(
            update(graph_version).values(status="retired", ir={"invalid_today": True})
        )
        await seed.graph_version(
            scenario.identity.graph_id, number=2, ir={"different": True}
        )
        await connection.execute(
            update(namespace).values(policy={"concurrency": {"max_pending_runs": 1}})
        )
        await connection.execute(delete(idempotency_record))
        await connection.execute(update(inbound_event).values(payload={}))
    before = await scenario.snapshot()
    reopened = Database(database.settings)

    try:
        replay = await replace(scenario, database=reopened).read()
    finally:
        await reopened.close()
    after = await scenario.snapshot()

    assert (
        initial
        == replay
        == StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response)
    )
    assert before == after
    assert replay.response is not None
    if skipped:
        assert after["run"] == []
    else:
        assert replay.response.outcome == "accepted"
        assert replay.response.status == "pending"
        assert replay.response.graph_version_id == scenario.version_id


@pytest.mark.asyncio
async def test_manual_start_replay_reads_only_the_compact_indexed_receipt(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    before = await scenario.snapshot()

    async with database.read_transaction() as connection:
        async with observed_statements(connection) as statements:
            result = await replay_manual_start(
                connection, scenario.principal, scenario.identity, KEY
            )
    after = await scenario.snapshot()

    assert result.outcome is ReplayOutcome.MATCHED
    assert len(statements) == 1
    statement = statements[0]
    assert statement.startswith("SELECT inbound_event.start_outcome")
    for field in ("namespace_id", "name", "idempotency_key"):
        assert f"inbound_event.{field} = " in statement
    for excluded in ("payload", "consumed_by", "JOIN", "graph_version", "policy"):
        assert excluded not in statement
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["params", "version", "secrets", "key", "graph"])
async def test_manual_start_replay_distinguishes_absence_and_changed_request(
    database: Database, change: str
) -> None:
    scenario = await replay_scenario(database)
    identity = prepare_manual_start(
        scenario.identity.namespace_id,
        scenario.principal.owner if change == "graph" else scenario.identity.graph_id,
        {"private": "changed" if change == "params" else PRIVATE},
        graph_version_id=scenario.version_id if change == "version" else None,
        allow_unenforced_secrets=change == "secrets",
    )
    key = "other-key" if change == "key" else KEY
    before = await scenario.snapshot()

    async with database.read_transaction() as connection:
        result = await replay_manual_start(
            connection, scenario.principal, identity, key
        )
    after = await scenario.snapshot()

    assert result == StartReplay(
        ReplayOutcome.ABSENT if change in ("key", "graph") else ReplayOutcome.MISMATCHED
    )
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority", ["service", "no_grant", "foreign_namespace", "second_user"]
)
async def test_manual_start_replay_checks_current_authority_before_disclosure(
    database: Database, authority: str
) -> None:
    scenario = await replay_scenario(database)
    fields: dict[str, object] = {}
    identity = scenario.identity
    if authority == "service":
        fields = {"kind": "service", "runtime": scenario.principal.owner}
    elif authority == "no_grant":
        fields = {"namespaces": ()}
    elif authority == "foreign_namespace":
        identity = prepare_manual_start(
            scenario.principal.owner, identity.graph_id, {"private": PRIVATE}
        )
    else:
        fields = {"owner": scenario.version_id, "token_id": scenario.principal.owner}
    principal = Principal.model_validate(scenario.principal.model_dump() | fields)

    async with database.read_transaction() as connection:
        async with observed_statements(connection) as statements:
            result = await replay_manual_start(connection, principal, identity, KEY)

    expected = (
        ReplayOutcome.MATCHED if authority == "second_user" else ReplayOutcome.REFUSED
    )
    assert result.outcome is expected
    assert len(statements) == (1 if authority == "second_user" else 0)
    assert result.response == (
        scenario.receipt.response if authority == "second_user" else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["null", "version", "scope", "response", "extra"])
async def test_manual_start_replay_never_treats_corrupt_receipts_as_absent(
    database: Database, fault: str
) -> None:
    scenario = await replay_scenario(database)
    document = start_receipt_document(scenario.receipt)
    value: JsonValue = document
    if fault == "null":
        value = None
    elif fault == "version":
        document["version"] = True
    elif fault == "scope":
        document["graph_id"] = scenario.version_id
    elif fault == "response":
        document["response"] = {"outcome": "accepted", "run_id": PRIVATE}
    else:
        document["private"] = PRIVATE
    async with database.transaction() as connection:
        await connection.execute(update(inbound_event).values(start_outcome=value))
    before = await scenario.snapshot()
    denied = replace(
        scenario,
        principal=Principal.model_validate(
            scenario.principal.model_dump() | {"namespaces": ()}
        ),
    )

    with pytest.raises(
        StartRecordRejected, match="^the manual start record is invalid$"
    ):
        await scenario.read()
    refused = await denied.read()
    after = await scenario.snapshot()

    assert refused == StartReplay(ReplayOutcome.REFUSED)
    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "a" * 256, "private\u0000", "private\ud800"])
async def test_manual_start_replay_rejects_invalid_keys_before_database_read(
    database: Database, key: str
) -> None:
    scenario = await replay_scenario(database)

    async with database.read_transaction() as connection:
        async with observed_statements(connection) as statements:
            with pytest.raises(StartRecordRejected):
                await replay_manual_start(
                    connection, scenario.principal, scenario.identity, key
                )

    assert statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "cancel", "shutdown"])
async def test_manual_start_replay_releases_reader_on_caller_exit(
    database: Database, ending: str
) -> None:
    scenario = await replay_scenario(database)
    before = await scenario.snapshot()
    owned = Database(database.settings)
    ledger = ConnectionLedger(owned)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def reader() -> StartReplay:
        async with owned.read_transaction() as connection:
            result = await replay_manual_start(
                connection, scenario.principal, scenario.identity, KEY
            )
            assert result.outcome is ReplayOutcome.MATCHED
            entered.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            if ending == "rollback":
                raise DeliberateRollback()
        return result

    task = asyncio.create_task(reader())
    await asyncio.wait_for(entered.wait(), DEADLINE)

    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif ending == "rollback":
        release.set()
        with pytest.raises(DeliberateRollback):
            await task
    else:
        closing = asyncio.create_task(owned.close())
        await until(lambda: owned.lifecycle is Lifecycle.CLOSING)
        release.set()
        assert await task == StartReplay(
            ReplayOutcome.MATCHED, scenario.receipt.response
        )
        await asyncio.wait_for(closing, DEADLINE)
    await owned.close()
    after = await scenario.snapshot()

    assert before == after
    assert owned.units_in_flight() == 0
    assert ledger.outstanding == 0
    assert ledger.opened == ledger.closed
    assert ledger.closed_while_checked_out == 0
