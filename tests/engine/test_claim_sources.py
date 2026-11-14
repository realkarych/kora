import asyncio
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import Connection, event, func, insert, select, update
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.util.concurrency import await_

from kora.db.claim_source_values import (
    ClaimSourcesRejected,
    SourceAllowance,
    prepare_claim_sources,
)
from kora.db.claim_sources import (
    AcquiredClaimSources,
    ClaimSourceBinding,
    ClaimSourceReadLimits,
    acquire_claim_sources,
)
from kora.db.claim_sources_v1 import MAX_SOURCE_COUNT
from kora.db.connection import Database, Dialect
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import attempt, attempt_claim_read_admission
from kora.engine.claim_sources import (
    decode_claim_sources,
    insert_claim_attempt,
    recheck_claim_sources,
    update_claim_sources,
)
from kora.engine.locks import TransitionLocks
from kora.ir.json import JsonObject, JsonValue
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import START, Seeder
from tests.db.test_graph_read_admission import executed_queries, sqlite_instructions
from tests.db.test_run_input_parent import open_sqlite
from tests.engine.test_run_input_reads import seed as seed_run

ALLOWANCE = SourceAllowance(1024 * 1024, 1024 * 1024)
LIMITS = ClaimSourceReadLimits(4096, 4096, 2 * 1024 * 1024, (ALLOWANCE,) * 5)
PRIVATE: str = "private-claim-source-fixture"
SCALARS: list[JsonValue] = [True, 10**5000, -5e-324, {"unicode": "😀é"}, [[[]]]]


def source_values() -> JsonObject:
    return {
        "input_bindings": {"version": 1, "inputs": []},
        "execution_policy": None,
        "execution_request": None,
        "handle": None,
        "deadline_binding": None,
    }


async def candidate(database: Database) -> tuple[ClaimSourceBinding, dict[str, object]]:
    parent = await seed_run(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        node_id = await seeder.node(parent.run_id, "task")
        attempt_id, price_id = seeder.new_id(), seeder.new_id()
    binding = ClaimSourceBinding(
        RunInputBinding(
            parent.namespace_id, parent.graph_id, parent.version_id, parent.run_id
        ),
        node_id,
        attempt_id,
        price_id,
        "task",
    )
    values: dict[str, object] = {
        "id": attempt_id,
        "node_instance_id": node_id,
        "number": 1,
        "runtime": "local",
        "execution_state": "unknown",
        "recovery_version": 0,
        "recovery_required": False,
        "status": "ready",
        "desired_state": "run",
        "record_version": 1,
        "infrastructure_retries": 0,
        "lease_epoch": 0,
        "counters": {},
        "scratch": {},
        "price_table_id": price_id,
        "created_at": START,
        "updated_at": START,
    }
    return binding, values


async def seed(
    database: Database, sources: JsonObject | None = None
) -> ClaimSourceBinding:
    binding, values = await candidate(database)
    selected = source_values() if sources is None else sources
    prepared = prepare_claim_sources(selected, {name: ALLOWANCE for name in selected})
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(binding.run.run_id)
        await insert_claim_attempt(
            writer, locks, binding, values, prepared, LIMITS, START
        )
    return binding


async def read(
    database: Database,
    binding: ClaimSourceBinding,
    limits: ClaimSourceReadLimits = LIMITS,
) -> AcquiredClaimSources | None:
    async with database.read_snapshot() as reader:
        return await acquire_claim_sources(reader, binding, limits)


@pytest.mark.asyncio
async def test_claim_sources_new_attempt_round_trips_and_initializes_offset(
    database: Database,
) -> None:
    binding = await seed(database)
    ledger = ConnectionLedger(database)

    acquired = await read(database, binding)
    assert acquired is not None
    decoded = decode_claim_sources(acquired)
    async with database.read_transaction() as reader:
        offset = await reader.scalar(
            select(attempt.c.acknowledged_segment_offset).where(
                attempt.c.id == binding.attempt_id
            )
        )

    assert decoded.values == source_values()
    assert decoded.binding == binding
    assert decoded.revision == 1
    assert offset == 0
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_sources_prepare_detaches_before_writer(database: Database) -> None:
    binding, values = await candidate(database)
    original = source_values()
    prepared = prepare_claim_sources(original, {name: ALLOWANCE for name in original})
    original["input_bindings"] = PRIVATE

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(binding.run.run_id)
        await insert_claim_attempt(
            writer, locks, binding, values, prepared, LIMITS, START
        )
    acquired = await read(database, binding)

    assert acquired is not None
    assert decode_claim_sources(acquired).values == source_values()


@pytest.mark.asyncio
async def test_claim_sources_recheck_rejects_changed_source_and_preserves_offset(
    database: Database,
) -> None:
    binding = await seed(database)
    acquired = await read(database, binding)
    assert acquired is not None
    prepared = prepare_claim_sources(
        {"execution_request": {"payload_state": "expired"}},
        {"execution_request": ALLOWANCE},
    )

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(binding.run.run_id)
        unchanged = await recheck_claim_sources(writer, locks, acquired, LIMITS)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == binding.attempt_id)
            .values(acknowledged_segment_offset=17)
        )
        next_revision = await update_claim_sources(
            writer, locks, binding, prepared, LIMITS, 1, START
        )
        changed = await recheck_claim_sources(writer, locks, acquired, LIMITS)
    latest = await read(database, binding)
    async with database.read_transaction() as reader:
        offset = await reader.scalar(
            select(attempt.c.acknowledged_segment_offset).where(
                attempt.c.id == binding.attempt_id
            )
        )

    assert unchanged is True
    assert changed is False
    assert next_revision == 2
    assert latest is not None
    assert decode_claim_sources(latest).values["execution_request"] == {
        "payload_state": "expired"
    }
    assert offset == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [RuntimeError, asyncio.CancelledError])
async def test_claim_sources_insert_and_update_roll_back_together(
    database: Database, rollback: type[BaseException]
) -> None:
    binding, values = await candidate(database)
    original = source_values()
    prepared = prepare_claim_sources(original, {name: ALLOWANCE for name in original})

    async def interrupted_insert() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            await insert_claim_attempt(
                writer, locks, binding, values, prepared, LIMITS, START
            )
            raise rollback()

    with pytest.raises(rollback):
        await interrupted_insert()
    async with database.read_transaction() as reader:
        saved_attempt = await reader.scalar(
            select(attempt.c.id).where(attempt.c.id == binding.attempt_id)
        )
        saved_admission = await reader.scalar(
            select(attempt_claim_read_admission.c.attempt_id).where(
                attempt_claim_read_admission.c.attempt_id == binding.attempt_id
            )
        )

    assert saved_attempt is saved_admission is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_sources_unknown_legacy_admission_is_not_reconstructed(
    database: Database,
) -> None:
    binding, values = await candidate(database)
    async with database.transaction() as writer:
        await writer.execute(insert(attempt).values(**values, **source_values()))

    acquired = await read(database, binding)
    async with database.read_transaction() as reader:
        offset = await reader.scalar(
            select(attempt.c.acknowledged_segment_offset).where(
                attempt.c.id == binding.attempt_id
            )
        )
        admission = await reader.scalar(
            select(attempt_claim_read_admission.c.attempt_id).where(
                attempt_claim_read_admission.c.attempt_id == binding.attempt_id
            )
        )

    assert acquired is offset is admission is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["namespace", "graph", "version", "run", "node", "attempt", "price", "path"],
)
async def test_claim_sources_reject_exact_binding_substitution(
    database: Database, field: str
) -> None:
    binding = await seed(database)
    run_fields = {
        "namespace": "namespace_id",
        "graph": "graph_id",
        "version": "version_id",
        "run": "run_id",
    }
    own_fields = {
        "node": "node_id",
        "attempt": "attempt_id",
        "price": "price_table_id",
        "path": "node_path",
    }
    if field in run_fields:
        altered = replace(
            binding, run=replace(binding.run, **{run_fields[field]: binding.attempt_id})
        )
    else:
        altered = replace(binding, **{own_fields[field]: binding.run.version_id})

    result = await read(database, altered)

    assert result is None


@pytest.mark.asyncio
async def test_claim_sources_refuse_stale_or_overflowed_revision_without_writes(
    database: Database,
) -> None:
    binding = await seed(database)
    prepared = prepare_claim_sources(
        {"handle": {"private": PRIVATE}}, {"handle": ALLOWANCE}
    )

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(binding.run.run_id)
        for revision in (0, 2, MAX_SOURCE_COUNT):
            with pytest.raises(ClaimSourcesRejected):
                await update_claim_sources(
                    writer, locks, binding, prepared, LIMITS, revision, START
                )
    acquired = await read(database, binding)

    assert acquired is not None
    assert acquired.revision == 1
    assert decode_claim_sources(acquired).values["handle"] is None


@pytest.mark.asyncio
async def test_claim_sources_bounds_apply_before_payload_acquisition(
    database: Database,
) -> None:
    binding = await seed(database)
    async with database.read_transaction() as reader:
        bound = await reader.scalar(
            select(attempt_claim_read_admission.c.input_bindings_native_bytes).where(
                attempt_claim_read_admission.c.attempt_id == binding.attempt_id
            )
        )
    assert type(bound) is int
    exact = replace(LIMITS, fields=(SourceAllowance(bound, bound), *LIMITS.fields[1:]))
    below = replace(
        LIMITS, fields=(SourceAllowance(bound - 1, bound), *LIMITS.fields[1:])
    )

    accepted = await read(database, binding, exact)
    rejected = await read(database, binding, below)

    assert accepted is not None
    assert rejected is None


@pytest.mark.asyncio
async def test_claim_sources_guard_unrelated_sqlite_wide_record(
    database: Database,
) -> None:
    binding = await seed(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == binding.attempt_id)
            .values(scratch={"private": PRIVATE * 10000})
        )

    acquired = await read(database, binding, replace(LIMITS, attempt_row=4096))

    assert (acquired is None) == (database.dialect is Dialect.SQLITE)


@pytest.mark.parametrize(
    "value", SCALARS, ids=["boolean", "wide-integer", "subnormal", "unicode", "nested"]
)
def test_claim_source_arithmetic_supports_existing_scalar_domain(
    value: JsonValue,
) -> None:
    values: JsonObject = {"handle": {"value": value}}

    prepared = prepare_claim_sources(values, {"handle": ALLOWANCE})

    assert prepared.fields[0].native_bytes == prepared.fields[0].transport_bytes
    assert prepared.fields[0].native_bytes > 0


@pytest.mark.asyncio
async def test_claim_sources_coherent_snapshot_observes_one_revision(
    database: Database,
) -> None:
    binding = await seed(database)
    prepared = prepare_claim_sources(
        {"handle": {"private": PRIVATE}}, {"handle": ALLOWANCE}
    )

    async with database.read_snapshot() as reader:
        before = await acquire_claim_sources(reader, binding, LIMITS)
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            await update_claim_sources(
                writer, locks, binding, prepared, LIMITS, 1, START
            )
        same_snapshot = await acquire_claim_sources(reader, binding, LIMITS)
    current = await read(database, binding)

    assert before is not None
    assert same_snapshot == before
    assert current is not None
    assert current.revision == 2
    assert decode_claim_sources(current).values["handle"] == {"private": PRIVATE}


@pytest.mark.asyncio
async def test_claim_sources_concurrent_updates_accept_one_expected_revision(
    database: Database,
) -> None:
    binding = await seed(database)
    prepared = prepare_claim_sources(
        {"handle": {"private": PRIVATE}}, {"handle": ALLOWANCE}
    )

    async def update_once() -> int:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            return await update_claim_sources(
                writer, locks, binding, prepared, LIMITS, 1, START
            )

    results = await asyncio.gather(update_once(), update_once(), return_exceptions=True)
    current = await read(database, binding)

    assert results.count(2) == 1
    assert sum(isinstance(result, ClaimSourcesRejected) for result in results) == 1
    assert current is not None
    assert current.revision == 2


@pytest.mark.asyncio
async def test_claim_sources_cancelled_acquisition_releases_snapshot(
    database: Database,
) -> None:
    binding = await seed(database)
    entered, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    def pause(
        connection: Connection, cursor: DBAPICursor, statement: str, *rest: object
    ) -> None:
        if (
            "FROM ONLY attempt_claim_read_admission" in statement
            or "FROM main.attempt_claim_read_admission INDEXED BY" in statement
        ):
            entered.set()
            await_(release.wait())

    engine = database.reader_engine.sync_engine
    event.listen(engine, "after_cursor_execute", pause)
    task = asyncio.create_task(read(database, binding))
    try:
        await asyncio.wait_for(entered.wait(), CONDITION_DEADLINE_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        event.remove(engine, "after_cursor_execute", pause)
    recovered = await read(database, binding)

    assert recovered is not None
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["EXTENDED", "EXTERNAL"])
async def test_claim_sources_admit_large_values_before_native_projection(
    database: Database, storage: str
) -> None:
    binding, values = await candidate(database)
    content = (
        PRIVATE * 2000
        if storage == "EXTENDED"
        else "".join(sha256(str(index).encode()).hexdigest() for index in range(700))
    )
    original: JsonObject = {**source_values(), "handle": {"private": content}}
    prepared = prepare_claim_sources(original, {name: ALLOWANCE for name in original})
    bounds = prepared.fields[3]
    below = replace(
        LIMITS,
        fields=(
            *LIMITS.fields[:3],
            SourceAllowance(bounds.native_bytes - 1, bounds.transport_bytes),
            LIMITS.fields[4],
        ),
    )
    async with database.transaction() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            await writer.exec_driver_sql(
                f"ALTER TABLE attempt ALTER COLUMN handle SET STORAGE {storage}"
            )
        locks = TransitionLocks(writer)
        await locks.acquire_run(binding.run.run_id)
        await insert_claim_attempt(
            writer, locks, binding, values, prepared, LIMITS, START
        )

    async with database.read_snapshot() as reader:
        with executed_queries(reader) as refused_queries:
            refused = await acquire_claim_sources(reader, binding, below)
        accepted = await acquire_claim_sources(reader, binding, LIMITS)
        if database.dialect is Dialect.POSTGRESQL:
            compression = await reader.scalar(
                select(func.pg_catalog.pg_column_compression(attempt.c.handle)).where(
                    attempt.c.id == binding.attempt_id
                )
            )
        else:
            compression = None
        toast_size = 0
        if database.dialect is Dialect.POSTGRESQL:
            toast_size = (
                await reader.exec_driver_sql(
                    "SELECT pg_relation_size(reltoastrelid) FROM pg_class "
                    "WHERE oid = 'attempt'::regclass"
                )
            ).scalar_one()

    assert refused is None
    assert accepted is not None
    assert decode_claim_sources(accepted).values == original
    assert all(
        "convert_to" not in sql and " AS BLOB" not in sql for sql, _ in refused_queries
    )
    if database.dialect is Dialect.POSTGRESQL:
        assert (compression is not None) == (storage == "EXTENDED")
        if storage == "EXTERNAL":
            assert type(toast_size) is int
            assert toast_size > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_claim_sources_sqlite_preserves_declared_encoding(
    tmp_path: Path, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    original: JsonObject = {**source_values(), "handle": {"unicode": "é😀"}}
    try:
        binding = await seed(database, original)

        acquired = await read(database, binding)
    finally:
        await database.close()

    assert acquired is not None
    assert acquired.encoding == encoding
    assert decode_claim_sources(acquired).values == original


def test_claim_source_preparation_counts_alias_occurrences_and_refuses_cycles() -> None:
    member: JsonObject = {"value": PRIVATE}
    shared: JsonObject = {"a": member, "b": member}
    independent: JsonObject = {"a": dict(member), "b": dict(member)}
    member["cycle"] = member

    with pytest.raises(ValueError, match="claim sources are unavailable"):
        prepare_claim_sources({"handle": shared}, {"handle": ALLOWANCE})
    del member["cycle"]
    repeated = prepare_claim_sources({"handle": shared}, {"handle": ALLOWANCE})
    copied = prepare_claim_sources({"handle": independent}, {"handle": ALLOWANCE})

    assert repeated == copied


def test_claim_source_preparation_keeps_admitted_deep_domain() -> None:
    root: JsonValue = "leaf"
    for _ in range(1500):
        root = [root]

    prepared = prepare_claim_sources({"handle": root}, {"handle": ALLOWANCE})

    assert prepared.fields[0].native_bytes == 1500 * 96 + 112


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", SCALARS, ids=["boolean", "wide-integer", "subnormal", "unicode", "nested"]
)
async def test_claim_source_scalar_domain_round_trips_in_database(
    database: Database, value: JsonValue
) -> None:
    sources: JsonObject = {**source_values(), "handle": {"value": value}}
    binding = await seed(database, sources)

    acquired = await read(database, binding)

    assert acquired is not None
    assert decode_claim_sources(acquired).values == sources


@pytest.mark.asyncio
async def test_claim_source_late_native_update_failure_preserves_previous_revision(
    database: Database,
) -> None:
    binding = await seed(database)
    prepared = prepare_claim_sources(
        {"handle": {"value": PRIVATE}}, {"handle": ALLOWANCE}
    )
    async with database.transaction() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            await writer.exec_driver_sql(
                "ALTER TABLE attempt_claim_read_admission "
                "ADD CONSTRAINT refuse_revision_fixture CHECK (source_revision = 1)"
            )
        else:
            await writer.exec_driver_sql(
                "CREATE TRIGGER refuse_revision_fixture BEFORE UPDATE "
                "ON attempt_claim_read_admission BEGIN SELECT "
                "RAISE(ABORT, 'revision fixture refusal'); END"
            )

    async def rejected_update() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            await update_claim_sources(
                writer, locks, binding, prepared, LIMITS, 1, START
            )

    with pytest.raises(ClaimSourcesRejected) as failure:
        await rejected_update()
    acquired = await read(database, binding)

    assert acquired is not None
    assert acquired.revision == 1
    assert decode_claim_sources(acquired).values == source_values()
    assert str(failure.value) == "claim sources are unavailable"
    assert failure.value.__cause__ is failure.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [RuntimeError, asyncio.CancelledError])
async def test_claim_source_completed_update_rolls_back_on_failure(
    database: Database, rollback: type[BaseException]
) -> None:
    binding = await seed(database)
    prepared = prepare_claim_sources(
        {"handle": {"value": PRIVATE}}, {"handle": ALLOWANCE}
    )

    async def interrupted_update() -> None:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(binding.run.run_id)
            await update_claim_sources(
                writer, locks, binding, prepared, LIMITS, 1, START
            )
            raise rollback()

    with pytest.raises(rollback):
        await interrupted_update()
    acquired = await read(database, binding)

    assert acquired is not None
    assert acquired.revision == 1
    assert decode_claim_sources(acquired).values == source_values()


@pytest.mark.asyncio
async def test_claim_sources_sqlite_native_guards_precede_payload_reads(
    tmp_path: Path,
) -> None:
    database = await open_sqlite(tmp_path)
    try:
        binding = await seed(database)

        async with database.read_snapshot() as reader:
            with executed_queries(reader) as statements:
                acquired = await acquire_claim_sources(reader, binding, LIMITS)
            selected = [
                (sql, args)
                for sql, args in statements
                if "main.attempt INDEXED BY ix_claim_attempt_locator" in sql
            ]
            native = [
                await sqlite_instructions(reader, sql, args) for sql, args in selected
            ]
            plans = [
                list(await reader.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args))
                for sql, args in selected
            ]
    finally:
        await database.close()

    assert acquired is not None
    assert len(native) == 6
    metadata = native[0]
    for name in (
        "input_bindings",
        "execution_policy",
        "handle",
        "scratch",
        "counters",
        "recovery",
    ):
        position = attempt.c.keys().index(name)
        accesses = [
            row for row in metadata if row[1] == "Column" and row[3] == position
        ]
        assert [row[6] for row in accesses] == [0x80, 0x80, 0xC0]
    for instructions in native[1:]:
        for row in instructions:
            if row[1] == "Column" and row[6] == 0:
                address = row[0]
                assert type(address) is int
                assert any(
                    earlier[1] in ("Gt", "Ne", "IfNot")
                    and type(earlier[3]) is int
                    and earlier[3] > address
                    for earlier in instructions[:address]
                )
    assert all(
        any("USING INDEX ix_claim_attempt_locator" in str(row[-1]) for row in plan)
        for plan in plans
    )
