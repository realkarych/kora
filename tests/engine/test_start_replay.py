import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from traceback import walk_tb

import pytest
from sqlalchemy import delete, event, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, Dialect, Lifecycle
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
    prepare_manual_start_request,
    start_receipt_document,
)
from kora.engine.start_replay import (
    ReplayOutcome,
    StartReadFailed,
    StartReplay,
    read_manual_start_candidate,
    replay_manual_start,
)
from kora.ir.json import JsonValue
from kora.protocols.authentication import NamespaceGrant, Principal
from tests.db.closing import ConnectionLedger, until
from tests.db.seed import FakeClock, Seeder
from tests.engine.lock_support import DEADLINE, DeliberateRollback
from tests.engine.start_support import KEY, PRIVATE, replay_scenario
from tests.support.databases import copy_sqlite_template

NEW_KEY: str = "unrecorded-manual-start"


@asynccontextmanager
async def observed_statements(
    connection: AsyncConnection,
    *,
    bound_parameters: list[tuple[object, ...]] | None = None,
) -> AsyncGenerator[list[str], None]:
    statements: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        *arguments: object,
    ) -> None:
        statements.append(statement)
        if bound_parameters is not None:
            bound_parameters.append(parameters)

    event.listen(connection.sync_connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_manual_start_candidate_selects_exact_or_highest_version(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    scope = scenario.identity
    async with database.transaction() as connection:
        seed = Seeder(connection)
        newest = await seed.graph_version(scope.graph_id, number=9)
        older = await seed.graph_version(scope.graph_id, number=2)
        empty_graph = await seed.graph(scope.namespace_id, "empty")
        foreign_namespace = await seed.namespace("foreign")
        foreign_graph = await seed.graph(foreign_namespace)
        foreign_version = await seed.graph_version(foreign_graph)
    principal = Principal.model_validate(
        scenario.principal.model_dump()
        | {
            "namespaces": (
                *scenario.principal.namespaces,
                NamespaceGrant(namespace_id=foreign_namespace, transcripts=False),
            )
        }
    )
    queries = (
        (scope.namespace_id, scope.graph_id, None),
        (scope.namespace_id, scope.graph_id, older),
        (scope.namespace_id, scope.graph_id, foreign_version),
        (scope.namespace_id, foreign_graph, None),
        (scope.namespace_id, foreign_graph, foreign_version),
        (scope.namespace_id, empty_graph, None),
    )

    async with database.read_snapshot() as connection:
        results = [
            await read_manual_start_candidate(
                connection,
                principal,
                prepare_manual_start_request(
                    namespace_id, graph_id, {}, graph_version_id=selected
                ),
                NEW_KEY,
            )
            for namespace_id, graph_id, selected in queries
        ]

    assert newest < older
    assert all(result.replay == StartReplay(ReplayOutcome.ABSENT) for result in results)
    assert [result.version_id for result in results] == [
        newest,
        older,
        None,
        None,
        None,
        None,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending", ["accepted", "skipped", "changed", "denied", "corrupt"]
)
async def test_manual_start_candidate_preserves_replay_precedence(
    database: Database, ending: str
) -> None:
    scenario = await replay_scenario(database, skipped=ending == "skipped")
    scope = scenario.identity
    principal = scenario.principal
    params = {} if ending == "changed" else {"private": PRIVATE}
    request = prepare_manual_start_request(scope.namespace_id, scope.graph_id, params)
    if ending == "denied":
        principal = Principal.model_validate(
            principal.model_dump() | {"namespaces": ()}
        )
    if ending == "corrupt":
        async with database.transaction() as connection:
            await connection.execute(update(inbound_event).values(start_outcome={}))

    async with database.read_snapshot() as connection:
        async with observed_statements(connection) as statements:
            if ending == "corrupt":
                with pytest.raises(StartRecordRejected):
                    await read_manual_start_candidate(
                        connection, principal, request, KEY
                    )
                result = None
            else:
                key = "" if ending == "denied" else KEY
                result = await read_manual_start_candidate(
                    connection, principal, request, key
                )

    if ending == "corrupt":
        assert result is None
    else:
        assert result is not None
        assert result.version_id is None
        assert not result.is_new_for(request.identity.request_digest, KEY)
        if ending in ("accepted", "skipped"):
            assert result.replay == StartReplay(
                ReplayOutcome.MATCHED, scenario.receipt.response
            )
            assert result.replay.response is not result.replay.response
        elif ending == "changed":
            assert result.replay == StartReplay(ReplayOutcome.MISMATCHED)
        else:
            assert result.replay == StartReplay(ReplayOutcome.REFUSED)
    assert len(statements) == (0 if ending == "denied" else 1)
    assert all("FROM inbound_event" in statement for statement in statements)


@pytest.mark.asyncio
async def test_manual_start_candidate_uses_only_scoped_scalar_reads(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    scope = scenario.identity
    request = prepare_manual_start_request(scope.namespace_id, scope.graph_id, {})
    async with database.transaction() as connection:
        newest = await Seeder(connection).graph_version(
            scope.graph_id, number=2, ir={"private": PRIVATE * 50_000}
        )
    before = await scenario.snapshot()

    async with database.read_snapshot() as connection:
        async with observed_statements(connection) as statements:
            result = await read_manual_start_candidate(
                connection, scenario.principal, request, NEW_KEY
            )
    after = await scenario.snapshot()

    assert result.replay == StartReplay(ReplayOutcome.ABSENT)
    assert result.version_id == newest
    assert result.is_new_for(request.identity.request_digest, NEW_KEY)
    assert len(statements) == 2
    assert statements[0].startswith("SELECT inbound_event.start_outcome")
    candidate = statements[1]
    assert candidate.startswith("SELECT CASE")
    assert "ORDER BY graph_version.number DESC" in candidate
    assert "LIMIT" in candidate
    assert "graph.namespace_id = " in candidate
    for excluded in (
        "graph_version.ir",
        "execution_contract",
        "resources",
        "graph_version.status",
    ):
        assert excluded not in candidate
    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_field", "exact"), [("id", False), ("number", False), ("number", True)]
)
async def test_manual_start_candidate_refuses_malformed_winning_version(
    database: Database, bad_field: str, exact: bool
) -> None:
    scenario = await replay_scenario(database)
    scope = scenario.identity
    async with database.transaction() as connection:
        seed = Seeder(connection)
        if bad_field == "number" and database.dialect is Dialect.POSTGRESQL:
            version_id = scenario.version_id
            await connection.execute(update(graph_version).values(number=-1))
        else:
            version_id = await seed.graph_version(scope.graph_id, number=2)
            malformed = (
                "0" * 25 + "I"
                if database.dialect is Dialect.POSTGRESQL
                else PRIVATE * 40_000
            )
            await connection.execute(
                update(graph_version)
                .where(graph_version.c.id == version_id)
                .values({bad_field: malformed})
            )
    requested = version_id if exact else None
    request = prepare_manual_start_request(
        scope.namespace_id, scope.graph_id, {}, graph_version_id=requested
    )

    bound_parameters: list[tuple[object, ...]] = []
    async with database.read_snapshot() as connection:
        async with observed_statements(
            connection, bound_parameters=bound_parameters
        ) as statements:
            with pytest.raises(StartRecordRejected) as refused:
                await read_manual_start_candidate(
                    connection, scenario.principal, request, NEW_KEY
                )
        native_row = None
        if database.dialect is Dialect.SQLITE:
            native = await connection.exec_driver_sql(
                statements[-1], bound_parameters[-1]
            )
            try:
                native_row = tuple(native.one())
            finally:
                native.close()

    assert len(statements) == 2
    if database.dialect is Dialect.SQLITE:
        assert native_row == (None,)
    assert str(refused.value) == "the manual start record is invalid"
    assert refused.value.__cause__ is None
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }
    assert {"key", "key_digest", "request", "principal"}.isdisjoint(
        frames["read_manual_start_candidate"]
    )
    assert refused.value.__context__ is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_manual_start_candidate_observes_the_owned_snapshot(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    scope = scenario.identity
    request = prepare_manual_start_request(
        scope.namespace_id, scope.graph_id, {"private": PRIVATE}
    )
    ledger = ConnectionLedger(database)

    async with database.read_snapshot() as reader:
        prior = await replay_manual_start(reader, scenario.principal, scope, NEW_KEY)
        async with database.transaction() as writer:
            newest = await Seeder(writer).graph_version(scope.graph_id, number=2)
            await writer.execute(update(inbound_event).values(idempotency_key=NEW_KEY))
        frozen = await read_manual_start_candidate(
            reader, scenario.principal, request, NEW_KEY
        )
    async with database.read_snapshot() as reader:
        replayed = await read_manual_start_candidate(
            reader, scenario.principal, request, NEW_KEY
        )
        fresh = await read_manual_start_candidate(
            reader, scenario.principal, request, "another-start"
        )

    assert prior == StartReplay(ReplayOutcome.ABSENT)
    assert frozen.replay == prior
    assert frozen.version_id == scenario.version_id
    assert replayed.replay == StartReplay(
        ReplayOutcome.MATCHED, scenario.receipt.response
    )
    assert replayed.version_id is None
    assert fresh.replay == prior
    assert fresh.version_id == newest
    assert database.units_in_flight() == 0
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["json", "utf8", "blob", "depth"])
async def test_manual_start_candidate_refuses_raw_sqlite_receipt_corruption(
    sqlite_template: Path, tmp_path: Path, corruption: str
) -> None:
    database = Database(copy_sqlite_template(sqlite_template, tmp_path / "raw.db"))
    try:
        scenario = await replay_scenario(database)
        scope = scenario.identity
        request = prepare_manual_start_request(
            scope.namespace_id, scope.graph_id, {"private": PRIVATE}
        )
        expected: type[StartReadFailed] | type[StartRecordRejected] = (
            StartRecordRejected
        )
        if corruption == "utf8":
            update_receipt = text(
                "UPDATE inbound_event SET start_outcome = CAST(X'FF' AS TEXT)"
            )
            expected = StartReadFailed
        else:
            corrupt = {
                "json": f"{{{PRIVATE}",
                "blob": PRIVATE.encode(),
                "depth": "[" * 2000 + "0" + "]" * 2000,
            }[corruption]
            update_receipt = text(
                "UPDATE inbound_event SET start_outcome = :raw"
            ).bindparams(raw=corrupt)
        async with database.transaction() as connection:
            await connection.execute(update_receipt)

        async with database.read_snapshot() as connection:
            async with observed_statements(connection) as statements:
                with pytest.raises(expected) as refused:
                    await read_manual_start_candidate(
                        connection, scenario.principal, request, KEY
                    )
    finally:
        await database.close()

    assert str(refused.value) == str(expected())
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert len(statements) == 1
    assert "FROM inbound_event" in statements[0]
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_manual_start_candidate_separates_query_failure_from_absence(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    scope = scenario.identity
    request = prepare_manual_start_request(scope.namespace_id, scope.graph_id, {})
    async with database.transaction() as connection:
        await connection.execute(
            text("ALTER TABLE graph_version RENAME TO graph_version_unavailable")
        )

    with pytest.raises(StartReadFailed) as failed:
        async with database.read_snapshot() as connection:
            await read_manual_start_candidate(
                connection, scenario.principal, request, NEW_KEY
            )

    assert str(failed.value) == "the manual start metadata read failed"
    assert failed.value.__cause__ is None
    assert failed.value.__context__ is None
    assert database.units_in_flight() == 0


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
