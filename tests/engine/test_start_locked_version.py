import asyncio
from traceback import walk_tb

import pytest
from sqlalchemy import text, update

from kora.conformance.capture import captured
from kora.db.connection import Database, Dialect
from kora.db.tables import graph_version
from kora.engine.locks import COMPONENT, LockError
from kora.engine.start_records import StartRecordRejected, prepare_manual_start_request
from kora.engine.start_replay import (
    ManualStartVersionRecheck,
    ReplayOutcome,
    StartReadFailed,
    StartReplay,
    read_manual_start_candidate,
    recheck_locked_manual_start_version,
)
from kora.observability import Level, LoggingSettings
from kora.protocols.authentication import Principal
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, Seeder
from tests.engine.lock_support import (
    DEADLINE,
    RUN_A,
    RUN_B,
    DeliberateRollback,
    lock_inventory,
    release_and_join,
    wait_for_lock_waiter,
)
from tests.engine.start_support import KEY, PRIVATE, replay_scenario
from tests.engine.test_start_locked_replay import (
    MISSING,
    NEW_KEY,
    acquire_locks,
    expected_queries,
    scopes_for,
)
from tests.engine.test_start_replay import observed_statements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("explicit", "commit", "expected"),
    [(False, True, False), (False, False, True), (True, True, True)],
    ids=["latest-commit", "latest-rollback", "exact-commit"],
)
async def test_locked_version_rechecks_the_original_selector_after_publication_wait(
    database: Database, explicit: bool, commit: bool, expected: bool
) -> None:
    scenario = await replay_scenario(database)
    graph_id = scenario.identity.graph_id
    request = prepare_manual_start_request(
        scenario.identity.namespace_id,
        graph_id,
        {"private": PRIVATE},
        graph_version_id=scenario.version_id if explicit else None,
    )
    async with database.read_snapshot() as reader:
        prepared = await read_manual_start_candidate(
            reader, scenario.principal, request, NEW_KEY
        )
    assert prepared.version_id == scenario.version_id
    before = await scenario.snapshot()
    scopes = scopes_for(scenario)
    ledger = ConnectionLedger(database)
    ready, release = asyncio.Event(), asyncio.Event()
    seen: list[ManualStartVersionRecheck] = []
    statements: list[str] = []

    async def publisher() -> None:
        try:
            async with database.transaction() as writer:
                await acquire_locks(writer, RUN_A, scopes)
                await Seeder(writer).graph_version(graph_id, number=2)
                ready.set()
                await asyncio.wait_for(release.wait(), DEADLINE)
                if not commit:
                    raise DeliberateRollback()
        except DeliberateRollback:
            assert not commit

    async def contender() -> None:
        async with database.transaction() as writer:
            if database.dialect is Dialect.POSTGRESQL:
                prior = await read_manual_start_candidate(
                    writer, scenario.principal, request, NEW_KEY
                )
                assert prior.replay == prepared.replay
                assert prior.version_id == prepared.version_id
            locks = await acquire_locks(writer, RUN_B, scopes)
            async with observed_statements(writer) as queries:
                seen.append(
                    await recheck_locked_manual_start_version(
                        writer,
                        locks,
                        scenario.principal,
                        request,
                        NEW_KEY,
                        RUN_B,
                        scenario.version_id,
                    )
                )
            statements.extend(queries)

    publisher_task = asyncio.create_task(publisher())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    contender_task = asyncio.create_task(contender())
    try:
        await wait_for_lock_waiter(database)
        assert not contender_task.done()
    finally:
        await release_and_join(release, publisher_task, contender_task)
    after = await scenario.snapshot()

    assert seen == [ManualStartVersionRecheck(MISSING, expected)]
    expected_queries(database, statements[:-1])
    candidate_query = statements[-1]
    assert "FROM graph_version" in candidate_query
    assert ("ORDER BY graph_version.number DESC" in candidate_query) is not explicit
    for excluded in (".ir", ".resources", ".execution_contract", "INSERT", "UPDATE"):
        assert excluded not in candidate_query
    assert after == before
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_locked_version_compares_the_explicit_pin_to_an_independent_id(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    graph_id = scenario.identity.graph_id
    async with database.transaction() as writer:
        newer_version_id = await Seeder(writer).graph_version(graph_id, number=2)
    request = prepare_manual_start_request(
        scenario.identity.namespace_id,
        graph_id,
        {},
        graph_version_id=scenario.version_id,
    )
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes_for(scenario))
        async with observed_statements(writer) as queries:
            result = await recheck_locked_manual_start_version(
                writer,
                locks,
                scenario.principal,
                request,
                NEW_KEY,
                RUN_A,
                newer_version_id,
            )

    assert newer_version_id != scenario.version_id
    assert result == ManualStartVersionRecheck(MISSING, False)
    expected_queries(database, queries[:-1])
    assert "graph_version.id =" in queries[-1]
    assert "ORDER BY" not in queries[-1]
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["accepted", "skipped", "mismatch", "denied"])
async def test_locked_version_preserves_receipt_precedence(
    database: Database, kind: str
) -> None:
    scenario = await replay_scenario(database, skipped=kind == "skipped")
    request = prepare_manual_start_request(
        scenario.identity.namespace_id,
        scenario.identity.graph_id,
        {} if kind == "mismatch" else {"private": PRIVATE},
    )
    denied = kind == "denied"
    principal = (
        Principal.model_validate(scenario.principal.model_dump() | {"namespaces": ()})
        if denied
        else scenario.principal
    )
    expected = {
        "accepted": StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response),
        "skipped": StartReplay(ReplayOutcome.MATCHED, scenario.receipt.response),
        "mismatch": StartReplay(ReplayOutcome.MISMATCHED),
        "denied": StartReplay(ReplayOutcome.REFUSED),
    }[kind]

    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes_for(scenario))
        async with observed_statements(writer) as queries:
            result = await recheck_locked_manual_start_version(
                writer, locks, principal, request, KEY, RUN_A, PRIVATE
            )

    assert result == ManualStartVersionRecheck(expected, False)
    if denied:
        assert queries == []
    else:
        expected_queries(database, queries)
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_locked_version_refuses_missing_selection_and_invalid_expected_id(
    database: Database,
) -> None:
    scenario = await replay_scenario(database)
    request = prepare_manual_start_request(
        scenario.identity.namespace_id,
        scenario.identity.graph_id,
        {},
        graph_version_id=RUN_B,
    )
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = await acquire_locks(writer, RUN_A, scopes_for(scenario))
        async with observed_statements(writer) as missing_queries:
            missing = await recheck_locked_manual_start_version(
                writer, locks, scenario.principal, request, NEW_KEY, RUN_A, RUN_B
            )
        async with observed_statements(writer) as invalid_queries:
            with pytest.raises(StartRecordRejected) as refused:
                await recheck_locked_manual_start_version(
                    writer, locks, scenario.principal, request, NEW_KEY, RUN_A, PRIVATE
                )
        async with observed_statements(writer) as no_lock_queries:
            with pytest.raises(LockError, match="required transaction locks"):
                await recheck_locked_manual_start_version(
                    writer, locks, scenario.principal, request, NEW_KEY, RUN_B, RUN_B
                )
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }

    assert missing == ManualStartVersionRecheck(MISSING, False)
    assert "FROM graph_version" in missing_queries[-1]
    expected_queries(database, missing_queries[:-1])
    expected_queries(database, invalid_queries)
    assert no_lock_queries == []
    assert {"expected_version_id", "request", "key", "principal"}.isdisjoint(
        frames["recheck_locked_manual_start_version"]
    )
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [True, False], ids=["ordinal", "native-failure"])
async def test_locked_version_failure_is_private_and_releases_the_writer(
    database: Database, malformed: bool
) -> None:
    scenario = await replay_scenario(database)
    request = prepare_manual_start_request(
        scenario.identity.namespace_id, scenario.identity.graph_id, {"private": PRIVATE}
    )
    ledger = ConnectionLedger(database)
    before = await scenario.snapshot()
    exception = StartRecordRejected if malformed else StartReadFailed

    async def fail() -> None:
        async with database.transaction() as writer:
            locks = await acquire_locks(writer, RUN_A, scopes_for(scenario))
            if malformed:
                await writer.execute(update(graph_version).values(number=0))
            else:
                await writer.execute(
                    text(
                        "ALTER TABLE graph_version RENAME COLUMN number "
                        "TO unavailable_ordinal"
                    )
                )
            await recheck_locked_manual_start_version(
                writer,
                locks,
                scenario.principal,
                request,
                NEW_KEY,
                RUN_A,
                scenario.version_id,
            )

    with captured(
        COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "locked-start-version-test",
        FakeClock().now,
    ) as capture:
        with pytest.raises(exception) as refusal:
            await fail()
        async with database.transaction() as writer:
            locks = await acquire_locks(writer, RUN_B, scopes_for(scenario))
            repeated = await recheck_locked_manual_start_version(
                writer,
                locks,
                scenario.principal,
                request,
                NEW_KEY,
                RUN_B,
                scenario.version_id,
            )
        records = capture.finish()
        raw_output = capture.reader.data
    after = await scenario.snapshot()
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }

    assert repeated == ManualStartVersionRecheck(MISSING, True)
    assert before == after
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None
    assert {
        "connection",
        "identity",
        "version_id",
        "statement",
        "result",
        "row",
    }.isdisjoint(frames["_read_candidate_version"])
    assert [
        record["matches"]
        for record in records
        if record["event"] == "engine.start_candidate.rechecked"
    ] == [True]
    for private in (
        PRIVATE,
        NEW_KEY,
        scenario.version_id,
        "unavailable_ordinal",
        "UndefinedColumn",
    ):
        assert private.encode() not in raw_output
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
