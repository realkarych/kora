import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from kora.conformance.capture import captured
from kora.db.connection import Database, Dialect
from kora.db.identifiers import MAX_RANDOM, IdentifierGenerator, decode
from kora.db.migrations import upgrade_database
from kora.db.pending_runs import PendingRunSweep
from kora.db.reads import events_of_run
from kora.db.tables import (
    attempt,
    event,
    graph_version,
    inbound_event,
    run,
    run_inputs,
    run_promotion_admission,
)
from kora.engine.locks import COMPONENT
from kora.engine.run_promotion import PromotionFailed, PromotionOutcome
from kora.engine.run_promotion_read import sqlite_record_width
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder, fixed_randomness
from tests.engine.lock_support import (
    DEADLINE,
    lock_inventory,
    release_and_join,
    wait_for_lock_waiter,
)
from tests.engine.promotion_support import (
    PRIVATE,
    append_pending,
    insert_pending,
    promotion_case,
)
from tests.engine.test_start_replay import observed_statements
from tests.support.databases import encoded_sqlite_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, "01h", "0" * 4096 + "1s"])
async def test_pending_promotion_is_atomic(
    database: Database, timeout: str | None
) -> None:
    case = await promotion_case(database, timeout=timeout)
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        await Seeder(writer).graph_version(case.scope.graph_id, 2)
        await writer.execute(
            update(run).where(run.c.id == case.run_id).values(needs_scheduling=False)
        )
    case.clock.advance(60)

    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "promotion-test", case.clock.now
    ) as capture:
        outcome = await case.promote(database)
        records = capture.finish()
    saved, nodes, events = await case.snapshot(database)

    assert outcome is PromotionOutcome.APPLIED
    assert saved["status"] == "running"
    assert saved["started_at"] == case.clock.now()
    assert saved["needs_scheduling"] is True
    assert saved["limits"] == ({} if timeout is None else {"timeout": timeout})
    assert [node["node_path"] for node in nodes] == [
        node.path for node in case.materialization.nodes
    ]
    assert all(
        node["status"] == "pending"
        and node["current_attempt_id"] is None
        and node["started_at"] is None
        for node in nodes
    )
    assert len(events) == 1
    assert events[0]["actor"] == {"kind": "system", "component": "scheduler"}
    assert events[0]["at"] == saved["started_at"]
    assert events[0]["payload"] == {
        "trigger": saved["trigger"],
        "graph_version": case.scope.version_id,
        "rerun_of": None,
    }
    assert [
        row["outcome"]
        for row in records
        if row["event"] == "engine.run_promotion.prepared"
    ] == ["applied"]
    assert PRIVATE not in repr(records)
    assert ledger.outstanding == database.units_in_flight() == 0
    async with database.read_transaction() as reader:
        projected = await events_of_run(reader, case.scope.namespace_id, case.run_id)
        assert projected[0].actor == {"kind": "system", "component": "scheduler"}
        assert projected[0].at == saved["started_at"]
        assert await reader.scalar(select(attempt.c.id)) is None
        if (
            database.dialect is Dialect.POSTGRESQL
            and timeout is not None
            and len(timeout) > 4000
        ):
            assert (
                await reader.scalar(
                    text(
                        "SELECT pg_column_compression(limits) FROM run WHERE id = :id"
                    ),
                    {"id": case.run_id},
                )
                is not None
            )


@pytest.mark.asyncio
async def test_promotion_replay_and_competing_ticks_start_once(
    database: Database,
) -> None:
    case = await promotion_case(database)
    ready, release = asyncio.Event(), asyncio.Event()
    outcomes: list[PromotionOutcome] = []

    async def first() -> None:
        async with database.transaction() as writer:
            locks = await case.locks(writer)
            outcomes.append(await case.apply(writer, locks))
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    async def second() -> None:
        outcomes.append(await case.promote(database))

    owner = asyncio.create_task(first())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    contender = asyncio.create_task(second())
    try:
        await wait_for_lock_waiter(database)
    finally:
        await release_and_join(release, owner, contender)
    outcomes.append(await case.promote(database))
    saved, nodes, events = await case.snapshot(database)

    assert outcomes == [
        PromotionOutcome.APPLIED,
        PromotionOutcome.UNCHANGED,
        PromotionOutcome.UNCHANGED,
    ]
    assert saved["status"] == "running"
    assert len(nodes) == 3
    assert len(events) == 1
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_queue_promotes_oldest_after_predecessor_finishes(
    database: Database,
) -> None:
    case = await promotion_case(database, overlap="queue")
    later = await append_pending(database, case)
    sweep = PendingRunSweep(1)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        predecessor = await seed.run(
            case.scope.namespace_id,
            case.scope.graph_id,
            case.scope.version_id,
            "running",
        )
        await writer.execute(
            update(run).where(run.c.id == case.run_id).values(needs_scheduling=False)
        )

    waiting = await case.promote(database)
    async with database.read_transaction() as reader:
        page = await sweep.next_page(reader)
    async with database.transaction() as writer:
        await writer.execute(
            update(run).where(run.c.id == predecessor).values(status="succeeded")
        )
    oldest_wait = await later.promote(database)
    started = await case.promote(database)
    newer_wait = await later.promote(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(run).where(run.c.id == case.run_id).values(status="succeeded")
        )
    newer_started = await later.promote(database)

    assert oldest_wait is PromotionOutcome.WAITING
    assert newer_wait is PromotionOutcome.WAITING
    assert newer_started is PromotionOutcome.APPLIED
    assert waiting is PromotionOutcome.WAITING
    assert list(page.run_ids) == [case.run_id]
    assert started is PromotionOutcome.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    [
        "trigger",
        "timeout",
        "receipt",
        "key",
        "version",
        "digest",
        "inputs",
        "tag",
        "sentinel",
        "clock",
        "nodes",
    ],
)
async def test_promotion_refuses_mismatched_header_or_version(
    database: Database, conflict: str
) -> None:
    case = await promotion_case(database)
    async with database.transaction() as writer:
        if conflict == "trigger":
            await writer.execute(
                update(run)
                .where(run.c.id == case.run_id)
                .values(trigger={"kind": "manual"})
            )
        elif conflict == "timeout":
            await writer.execute(
                update(run)
                .where(run.c.id == case.run_id)
                .values(limits={"timeout": "1h"})
            )
        elif conflict == "receipt":
            await writer.execute(
                update(inbound_event)
                .where(inbound_event.c.id == case.receipt_id)
                .values(start_outcome={})
            )
        elif conflict == "key":
            await writer.execute(
                update(inbound_event)
                .where(inbound_event.c.id == case.receipt_id)
                .values(idempotency_key="different")
            )
        elif conflict == "version":
            other = await Seeder(writer).graph_version(case.scope.graph_id, 2)
            await writer.execute(
                update(run)
                .where(run.c.id == case.run_id)
                .values(graph_version_id=other)
            )
        elif conflict == "digest":
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == case.scope.version_id)
                .values(execution_contract_digest="f" * 64)
            )
        elif conflict == "inputs":
            await writer.execute(
                delete(run_inputs).where(run_inputs.c.run_id == case.run_id)
            )
        elif conflict == "tag":
            await writer.execute(
                update(run)
                .where(run.c.id == case.run_id)
                .values(input_encoding_version=None)
            )
        elif conflict == "sentinel":
            await writer.execute(
                update(run).where(run.c.id == case.run_id).values(params={})
            )
        elif conflict == "clock":
            await writer.execute(
                update(run)
                .where(run.c.id == case.run_id)
                .values(started_at=case.clock.now())
            )
        else:
            await Seeder(writer).node(case.run_id, "existing", "command")
    before = await case.snapshot(database)

    outcome = await case.promote(database)
    after = await case.snapshot(database)

    assert outcome is PromotionOutcome.REFUSED
    assert after == before
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sql", "cancel"])
async def test_promotion_rollback_releases_writer(
    database: Database, failure: str
) -> None:
    case = await promotion_case(database)
    ledger = ConnectionLedger(database)
    if failure == "sql":
        case.clock.advance(1)
        randomness = fixed_randomness(decode(case.scope.namespace_id) & MAX_RANDOM)
        generator = IdentifierGenerator(case.clock.now, randomness)
        probe = IdentifierGenerator(case.clock.now, randomness)
        for _ in case.materialization.nodes:
            probe.new()
        collision = probe.new()
        async with database.transaction() as writer:
            await writer.execute(
                insert(event).values(
                    id=collision,
                    namespace_id=case.scope.namespace_id,
                    run_id=case.run_id,
                    kind="run.created",
                    actor={},
                    payload={"private": PRIVATE},
                    at=case.clock.now(),
                    **Seeder(writer).stamps(),
                )
            )
        case = replace(case, identifiers=generator)
    before = await case.snapshot(database)
    ready, release = asyncio.Event(), asyncio.Event()
    executed: list[str] = []

    async def interrupted() -> None:
        async with database.transaction() as writer:
            async with observed_statements(writer) as statements:
                try:
                    await case.apply(writer, await case.locks(writer))
                finally:
                    executed.extend(statements)
            ready.set()
            await release.wait()

    task = asyncio.create_task(interrupted())
    if failure == "cancel":
        await asyncio.wait_for(ready.wait(), DEADLINE)
        task.cancel()
    with pytest.raises(PromotionFailed if failure == "sql" else asyncio.CancelledError):
        await task
    after = await case.snapshot(database)
    repeated = await case.promote(database)

    assert after == before
    if failure == "sql":
        writes = [
            statement.split()[2 if statement.startswith("INSERT") else 1]
            for statement in executed
            if statement.startswith(("INSERT", "UPDATE"))
        ]
        assert writes == ["node_instance", "run", "event"]
    assert repeated is PromotionOutcome.APPLIED
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_pending_discovery_rotates_independently_of_markers(
    database: Database,
) -> None:
    case = await promotion_case(database)
    sweep = PendingRunSweep()
    async with database.transaction() as writer:
        seed = Seeder(writer)
        ids = await seed.runs(
            case.scope.namespace_id,
            case.scope.graph_id,
            case.scope.version_id,
            201,
            needs_scheduling=False,
        )
        await writer.execute(
            update(run).where(run.c.id.in_(ids)).values(status="pending")
        )
        await seed.runs(
            case.scope.namespace_id,
            case.scope.graph_id,
            case.scope.version_id,
            100,
            needs_scheduling=False,
        )
    async with database.read_transaction() as reader:
        first = await sweep.next_page(reader)
    async with database.transaction() as writer:
        added = await Seeder(writer).run(
            case.scope.namespace_id,
            case.scope.graph_id,
            case.scope.version_id,
            "pending",
            needs_scheduling=False,
        )

    async with database.read_transaction() as reader:
        second, third, fourth = [await sweep.next_page(reader) for _ in range(3)]
        fifth, sixth = [await sweep.next_page(reader) for _ in range(2)]

    assert [
        len(page.run_ids) for page in (first, second, third, fourth, fifth, sixth)
    ] == [100, 100, 2, 100, 100, 3]
    assert list(first.run_ids) + list(second.run_ids) + list(third.run_ids) == [
        case.run_id,
        *ids,
    ]
    assert added in sixth.run_ids
    assert first.wrapped
    assert fourth.wrapped
    assert not second.wrapped
    assert not third.wrapped
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence", ["missing", "parent_limit", "receipt_limit", "wrong_version"]
)
async def test_promotion_admission_precedes_retained_values(
    database: Database, evidence: str
) -> None:
    case = await promotion_case(database)
    async with database.transaction() as writer:
        if evidence == "missing":
            await writer.execute(
                delete(run_promotion_admission).where(
                    run_promotion_admission.c.run_id == case.run_id
                )
            )
        elif evidence == "wrong_version":
            await writer.execute(
                update(run_promotion_admission)
                .where(run_promotion_admission.c.run_id == case.run_id)
                .values(graph_version_id=case.identifiers.new())
            )
        else:
            column = "run_bytes" if evidence == "parent_limit" else "receipt_bytes"
            await writer.execute(
                update(run_promotion_admission)
                .where(run_promotion_admission.c.run_id == case.run_id)
                .values({column: 2_000_000})
            )
        await writer.execute(
            update(run)
            .where(run.c.id == case.run_id)
            .values(
                trigger={"private": PRIVATE * 10000},
                limits={"private": PRIVATE * 10000},
                params={"private": PRIVATE * 10000},
            )
        )

    async with database.transaction() as writer:
        locks = await case.locks(writer)
        async with observed_statements(writer) as statements:
            outcome = await case.apply(writer, locks)

    assert outcome is PromotionOutcome.REFUSED
    assert any("run_promotion_admission" in statement for statement in statements)
    assert not any(
        "run.trigger" in statement or "run.status" in statement or "CAST(" in statement
        for statement in statements
    )
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_reads_external_timeout(database: Database) -> None:
    timeout = "0" * 4096 + "1s"
    case = await promotion_case(database, timeout=timeout)
    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE run ALTER COLUMN limits SET STORAGE EXTERNAL"
            )
            await writer.execute(
                update(run)
                .where(run.c.id == case.run_id)
                .values(limits={"timeout": timeout})
            )

    outcome = await case.promote(database)

    assert outcome is PromotionOutcome.APPLIED
    if database.dialect is Dialect.POSTGRESQL:
        async with database.read_transaction() as reader:
            assert (
                await reader.scalar(
                    text(
                        "SELECT pg_column_compression(limits) FROM run WHERE id = :id"
                    ),
                    {"id": case.run_id},
                )
                is None
            )
            assert (
                await reader.scalar(
                    text("SELECT pg_column_size(limits) FROM run WHERE id = :id"),
                    {"id": case.run_id},
                )
                > 4096
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["duplicate", "cancel"])
async def test_admission_is_atomic_with_run_and_receipt(
    database: Database, failure: str
) -> None:
    case = await promotion_case(database)
    ready, release = asyncio.Event(), asyncio.Event()
    created: list[str] = []

    async def accept() -> None:
        async with database.transaction() as writer:
            new = await insert_pending(writer, case)
            created.extend([new.run_id, new.receipt_id])
            if failure == "duplicate":
                with await writer.execute(
                    select(run_promotion_admission).where(
                        run_promotion_admission.c.run_id == new.run_id
                    )
                ) as rows:
                    duplicate = dict(rows.mappings().one())
                await writer.execute(
                    insert(run_promotion_admission).values(**duplicate)
                )
            ready.set()
            await release.wait()

    task = asyncio.create_task(accept())
    if failure == "cancel":
        await asyncio.wait_for(ready.wait(), DEADLINE)
        task.cancel()
    with pytest.raises(
        IntegrityError if failure == "duplicate" else asyncio.CancelledError
    ):
        await task
    async with database.read_transaction() as reader:
        for table, key in (
            (run, "id"),
            (run_inputs, "run_id"),
            (run_promotion_admission, "run_id"),
            (inbound_event, "id"),
        ):
            assert (
                await reader.scalar(
                    select(table.c[key]).where(table.c[key].in_(created))
                )
                is None
            )

    assert database.units_in_flight() == 0
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_producer_admission_bounds_native_storage(
    database: Database, tmp_path: Path, encoding: str
) -> None:
    owned = database.dialect is Dialect.SQLITE
    if owned:
        settings = encoded_sqlite_settings(tmp_path / "encoded.db", encoding)
        await upgrade_database(settings)
        selected = Database(settings)
    else:
        selected = database
    retained: JsonObject = {
        "control": "\u0001" * 1024,
        "unicode": "é𝄞" * 128,
        "integers": [-(2**63), 2**63 - 1],
        "floats": [5e-324, 1.7976931348623157e308],
        "nested": [None, True, False, {}],
    }
    try:
        case = await promotion_case(selected, retention=retained)
        async with selected.read_transaction() as reader:
            bound = await reader.scalar(
                select(run_promotion_admission.c.run_bytes).where(
                    run_promotion_admission.c.run_id == case.run_id
                )
            )
            if selected.dialect is Dialect.SQLITE:
                statement = select(sqlite_record_width(list(run.c))).where(
                    run.c.id == case.run_id
                )
            else:
                statement = select(
                    func.pg_column_size(
                        func.jsonb_build_object(
                            "row", func.to_jsonb(run.table_valued())
                        )
                    )
                ).where(run.c.id == case.run_id)
            native = await reader.scalar(statement)

        outcome = await case.promote(selected)
        async with selected.read_transaction() as reader:
            after = await reader.scalar(statement)

        assert type(after) is int
        assert type(native) is int
        assert type(bound) is int
        assert 0 < native < bound
        assert 0 < after < bound
        assert outcome is PromotionOutcome.APPLIED
        assert selected.units_in_flight() == 0
    finally:
        if owned:
            await selected.close()


@pytest.mark.asyncio
async def test_admission_receipt_cannot_authorize_another_occurrence(
    database: Database,
) -> None:
    case = await promotion_case(database)
    other = await append_pending(database, case)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.run_id)
            .values(
                trigger={
                    "kind": "manual",
                    "overlap": "allow",
                    "occurrence_key": other.receipt_id,
                }
            )
        )

    async with database.transaction() as writer:
        locks = await case.locks(writer)
        async with observed_statements(writer) as statements:
            outcome = await case.apply(writer, locks)
    saved, nodes, events = await case.snapshot(database)

    assert outcome is PromotionOutcome.REFUSED
    assert saved["status"] == "pending"
    assert nodes == events == []
    assert not any("inbound_event" in statement for statement in statements)
