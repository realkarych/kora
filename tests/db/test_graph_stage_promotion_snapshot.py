import asyncio
import socket
import sqlite3
from collections.abc import Generator
from contextlib import AsyncExitStack, closing, contextmanager
from dataclasses import replace
from pathlib import Path
from traceback import format_exception, walk_tb

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import SQLITE_READERS, Database, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_stage_promotion_intent_reads import GraphStagePromotionIntentScalars
from kora.db.graph_stage_promotion_snapshot import (
    GraphStagePromotionDocuments,
    GraphStagePromotionSnapshot,
    GraphStagePromotionSnapshotFailure,
    GraphStagePromotionSnapshotRejected,
    read_graph_stage_promotion_snapshot,
)
from kora.db.tables import (
    graph_deployment_stage as stage,
)
from kora.db.tables import (
    graph_deployment_stage_occurrence as occurrence,
)
from kora.db.tables import (
    graph_deployment_stage_promotion_intent as intent,
)
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    REFUSAL_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_database,
    until,
)
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    PreparedScope,
    occurrence_values,
    stage_values,
)
from tests.db.test_graph_preparation_reads import seed_preparation
from tests.db.test_graph_stage_promotion_custody_migration import (
    INTENT_INSERT,
    REQUEST_DIGEST,
    SETTLEMENT_INSERT,
    OriginalStage,
)

FIRST: bytes = b"abc"
SETTLED: bytes = b"defg"
SECOND: bytes = b"hijkl"
DOCUMENT_BYTES: int = len(FIRST) + len(SETTLED) + len(SECOND)
PRIVATE: str = "private-promotion-snapshot"


def expected_intent(
    snapshot: GraphStagePromotionSnapshot, index: int, operation: str
) -> GraphStagePromotionIntentScalars:
    entry = snapshot.inventory.stages[index]
    return GraphStagePromotionIntentScalars(
        entry.store_id,
        entry.payload_digest,
        entry.fence_incarnation,
        operation,
        REQUEST_DIGEST,
        2**63 - 1,
    )


async def seed_stage_for_binding(
    connection: AsyncConnection, binding: GraphPreparationBinding, ordinal: int
) -> OriginalStage:
    identifier = Seeder(connection).new_id()
    scope = PreparedScope(binding.namespace_id, binding.preparation_id)
    fields = stage_values(scope, identifier)
    store = f"store-{ordinal}"
    fields["store_id"] = store
    await connection.execute(stage.insert().values(**fields))
    await connection.execute(
        occurrence.insert().values(**occurrence_values(scope, identifier, ordinal))
    )
    return OriginalStage(
        identifier,
        store,
        str(fields["payload_digest"]),
        int(fields["fence_incarnation"]),
    )


async def seed_documents(
    database: Database,
) -> tuple[GraphPreparationBinding, tuple[str, ...]]:
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=True)
        stages = [
            await seed_stage_for_binding(connection, binding, index)
            for index in range(3)
        ]
        await connection.execute(
            INTENT_INSERT,
            [stages[0].request("first", FIRST), stages[1].request("second", SECOND)],
        )
        await connection.execute(
            SETTLEMENT_INSERT,
            {"stage_id": stages[0].stage_id, "observation_bytes": SETTLED},
        )
    return binding, tuple(entry.stage_id for entry in stages)


async def read(
    database: Database,
    binding: GraphPreparationBinding,
    *,
    max_stages: int = 3,
    max_document_bytes: int = DOCUMENT_BYTES,
) -> GraphStagePromotionSnapshot:
    return await read_graph_stage_promotion_snapshot(
        database,
        binding,
        max_stages=max_stages,
        max_document_bytes=max_document_bytes,
        deadline=asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS,
    )


@contextmanager
def observed_projections(database: Database) -> Generator[list[str]]:
    seen: list[str] = []

    def capture(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if " AS intent_present" in statement:
            seen.append(statement)

    engine = database.reader_engine.sync_engine
    event.listen(engine, "after_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(engine, "after_cursor_execute", capture)


@pytest.mark.asyncio
async def test_promotion_snapshot_returns_exact_complete_detached_batch(
    database: Database,
) -> None:
    binding, stage_ids = await seed_documents(database)
    ledger = ConnectionLedger(database)

    with observed_projections(database) as projections:
        snapshot = await read(database, binding)

    assert snapshot.preparation.state == "open"
    assert tuple(entry.stage_id for entry in snapshot.inventory.stages) == stage_ids
    assert (
        tuple(entry.stage_id for entry in snapshot.inventory.occurrences) == stage_ids
    )
    assert snapshot.documents == (
        GraphStagePromotionDocuments(
            stage_ids[0], FIRST, SETTLED, expected_intent(snapshot, 0, "first")
        ),
        GraphStagePromotionDocuments(
            stage_ids[1], SECOND, None, expected_intent(snapshot, 1, "second")
        ),
        GraphStagePromotionDocuments(stage_ids[2], None, None, None),
    )
    assert len(projections) == len(stage_ids) == 3
    assert ledger.checkouts == 1
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_snapshot_distinguishes_empty_from_unknown_binding(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        binding, _ = await seed_preparation(connection, "open", bound=False)
        other_namespace = await Seeder(connection).namespace("other")
    wrong = replace(binding, namespace_id=other_namespace)
    ledger = ConnectionLedger(database)

    with observed_projections(database) as projections:
        empty = await read(database, binding, max_stages=0, max_document_bytes=0)
        with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
            await read(database, wrong)

    assert empty.preparation.state == "open"
    assert (
        empty.inventory.stages == empty.inventory.occurrences == empty.documents == ()
    )
    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert projections == []
    assert ledger.checkouts == 2
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_snapshot_refuses_incomplete_batch_privately(
    database: Database,
) -> None:
    binding, _ = await seed_documents(database)
    ledger = ConnectionLedger(database)

    with observed_projections(database) as stage_queries:
        with pytest.raises(GraphStagePromotionSnapshotRejected) as count_refused:
            await read(database, binding, max_stages=2)
    with observed_projections(database) as byte_queries:
        try:
            raise RuntimeError(PRIVATE)
        except RuntimeError:
            with pytest.raises(GraphStagePromotionSnapshotRejected) as bytes_refused:
                await read(database, binding, max_document_bytes=DOCUMENT_BYTES - 1)

    assert count_refused.value.reason is GraphStagePromotionSnapshotFailure.CAPACITY
    assert bytes_refused.value.reason is GraphStagePromotionSnapshotFailure.CAPACITY
    assert stage_queries == []
    assert len(byte_queries) == 2
    assert bytes_refused.value.__cause__ is bytes_refused.value.__context__ is None
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(bytes_refused.value.__traceback__)
    }
    assert "_collect" not in frames
    assert "_read_current" not in frames
    assert frames["read_graph_stage_promotion_snapshot"] == {
        "reason": GraphStagePromotionSnapshotFailure.CAPACITY
    }
    assert PRIVATE not in "".join(format_exception(bytes_refused.value))
    assert ledger.checkouts == 2
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["layout", "document", "orphan"])
async def test_promotion_snapshot_refuses_actual_corrupt_storage(
    database: Database, damage: str
) -> None:
    binding, stage_ids = await seed_documents(database)
    if damage == "orphan":
        await insert_orphan_settlement(database, stage_ids[1])
    else:
        await corrupt_snapshot_source(database, stage_ids[1], damage)
    ledger = ConnectionLedger(database)

    with observed_projections(database) as projections:
        with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
            await read(database, binding)

    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert len(projections) == (0 if damage == "layout" else 2)
    assert ledger.checkouts == 1
    assert ledger.outstanding == database.units_in_flight() == 0


async def insert_orphan_settlement(database: Database, stage_id: str) -> None:
    if database.dialect is Dialect.SQLITE:
        with closing(sqlite3.connect(database.settings.sqlite_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM graph_deployment_stage_promotion_intent "
                "WHERE stage_id = ?",
                (stage_id,),
            )
            connection.execute(
                "INSERT INTO graph_deployment_stage_promotion_settlement "
                "(stage_id, observation_bytes) VALUES (?, ?)",
                (stage_id, SETTLED),
            )
            connection.commit()
        return
    async with database.transaction() as connection:
        await connection.exec_driver_sql(
            "ALTER TABLE graph_deployment_stage_promotion_settlement "
            "DROP CONSTRAINT fk_promotion_settlement_intent"
        )
        await connection.execute(intent.delete().where(intent.c.stage_id == stage_id))
        await connection.execute(
            SETTLEMENT_INSERT, {"stage_id": stage_id, "observation_bytes": SETTLED}
        )


async def corrupt_snapshot_source(
    database: Database, stage_id: str, damage: str
) -> None:
    async with database.transaction() as connection:
        if damage == "layout":
            await connection.exec_driver_sql(
                "DROP TABLE graph_deployment_stage_promotion_settlement"
            )
        else:
            sqlite = database.dialect is Dialect.SQLITE
            if sqlite:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            else:
                await connection.exec_driver_sql(
                    "ALTER TABLE graph_deployment_stage_promotion_intent "
                    "DROP CONSTRAINT ck_promotion_intent_document_width"
                )
            try:
                await connection.execute(
                    intent.update()
                    .where(intent.c.stage_id == stage_id)
                    .values(request_bytes=b"")
                )
            finally:
                if sqlite:
                    await connection.exec_driver_sql(
                        "PRAGMA ignore_check_constraints=OFF"
                    )


@pytest.mark.asyncio
async def test_promotion_snapshot_refuses_an_expired_budget_before_checkout(
    database: Database,
) -> None:
    binding, _ = await seed_documents(database)
    ledger = ConnectionLedger(database)
    expired = asyncio.get_running_loop().time() - 1.0

    with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
        await read_graph_stage_promotion_snapshot(
            database,
            binding,
            max_stages=3,
            max_document_bytes=DOCUMENT_BYTES,
            deadline=expired,
        )

    assert refused.value.reason is GraphStagePromotionSnapshotFailure.DEADLINE
    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_snapshot_refuses_a_failed_native_checkout_privately(
    database: Database, tmp_path: Path
) -> None:
    binding, _ = await seed_documents(database)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        settings = database.settings
        if database.dialect is Dialect.SQLITE:
            settings = replace(
                settings, url=settings.url.set(database=str(tmp_path / PRIVATE))
            )
        else:
            settings = replace(
                settings,
                url=settings.url.set(
                    host="127.0.0.1", port=reserved.getsockname()[1], password=PRIVATE
                ),
            )
        unavailable = Database(settings)
        if database.dialect is Dialect.SQLITE:
            settings.sqlite_path.unlink()
            settings.sqlite_path.mkdir()
        ledger = ConnectionLedger(unavailable)

        try:
            with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
                await read(unavailable, binding)
        finally:
            await unavailable.close()

    assert refused.value.reason is GraphStagePromotionSnapshotFailure.UNVERIFIED
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert PRIVATE not in "".join(format_exception(refused.value))
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }
    assert "_unit" not in frames
    assert "_read_snapshot" not in frames
    assert frames["read_graph_stage_promotion_snapshot"] == {
        "reason": GraphStagePromotionSnapshotFailure.UNVERIFIED
    }
    assert ledger.checkouts == ledger.outstanding == unavailable.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_snapshot_deadline_ends_wait_before_a_native_slot_is_free(
    database: Database,
) -> None:
    binding, _ = await seed_documents(database)
    narrow = narrow_database(database, pool_size=1)
    ledger = ConnectionLedger(narrow)
    capacity = SQLITE_READERS if narrow.dialect is Dialect.SQLITE else 1
    held = AsyncExitStack()
    try:
        for _ in range(capacity):
            await held.enter_async_context(narrow.read_transaction())
        deadline = asyncio.get_running_loop().time() + REFUSAL_DEADLINE_SECONDS

        with observed_projections(narrow) as projections:
            task = asyncio.create_task(
                read_graph_stage_promotion_snapshot(
                    narrow,
                    binding,
                    max_stages=3,
                    max_document_bytes=DOCUMENT_BYTES,
                    deadline=deadline,
                )
            )
            try:
                await until(lambda: narrow.units_in_flight() == capacity + 1)
                done, _ = await asyncio.wait(
                    {task}, timeout=REFUSAL_DEADLINE_SECONDS * 2
                )
                units_before_release = narrow.units_in_flight()
                checkouts_before_release = ledger.checkouts
            finally:
                await held.aclose()
            with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
        checkouts_after_release = ledger.checkouts
        after = await read(narrow, binding)
    finally:
        await held.aclose()
        await narrow.close()

    assert refused.value.reason is GraphStagePromotionSnapshotFailure.DEADLINE
    assert done == {task}
    assert units_before_release == capacity
    assert checkouts_before_release == checkouts_after_release == capacity
    assert projections == []
    assert len(after.documents) == 3
    assert ledger.outstanding == narrow.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_snapshot_expiry_releases_a_borrowed_unit(
    database: Database,
) -> None:
    binding, _ = await seed_documents(database)
    ledger = ConnectionLedger(database)
    projected = asyncio.Event()
    hold = asyncio.Event()
    engine = database.reader_engine.sync_engine
    deadline = asyncio.get_running_loop().time() + REFUSAL_DEADLINE_SECONDS

    def after_projection(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if " AS intent_present" in statement:
            projected.set()
            await_(hold.wait())

    event.listen(engine, "after_cursor_execute", after_projection)
    try:
        with pytest.raises(GraphStagePromotionSnapshotRejected) as refused:
            await asyncio.wait_for(
                read_graph_stage_promotion_snapshot(
                    database,
                    binding,
                    max_stages=3,
                    max_document_bytes=DOCUMENT_BYTES,
                    deadline=deadline,
                ),
                CONDITION_DEADLINE_SECONDS,
            )
    finally:
        event.remove(engine, "after_cursor_execute", after_projection)
    checked_in = ledger.outstanding
    in_flight = database.units_in_flight()
    after = await read(database, binding)

    assert projected.is_set()
    assert refused.value.reason is GraphStagePromotionSnapshotFailure.DEADLINE
    assert refused.value.__context__ is refused.value.__cause__ is None
    assert (
        checked_in == in_flight == ledger.outstanding == database.units_in_flight() == 0
    )
    assert ledger.checkouts == 2
    assert len(after.documents) == 3


@pytest.mark.asyncio
async def test_promotion_snapshot_retains_visibility_across_a_real_commit(
    database: Database,
) -> None:
    binding, stage_ids = await seed_documents(database)
    ledger = ConnectionLedger(database)
    first = asyncio.Event()
    resume = asyncio.Event()
    engine = database.reader_engine.sync_engine
    replaced = b"newer"

    def after_projection(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if " AS intent_present" in statement and not first.is_set():
            first.set()
            await_(resume.wait())

    event.listen(engine, "after_cursor_execute", after_projection)
    try:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(read(database, binding))
            try:
                await asyncio.wait_for(first.wait(), CONDITION_DEADLINE_SECONDS)
                async with database.transaction() as writer:
                    await writer.execute(
                        intent.update()
                        .where(intent.c.stage_id == stage_ids[1])
                        .values(request_bytes=replaced)
                    )
            finally:
                resume.set()
        before = task.result()
    finally:
        event.remove(engine, "after_cursor_execute", after_projection)
    after = await read(database, binding)

    assert before.documents[1] == GraphStagePromotionDocuments(
        stage_ids[1], SECOND, None, expected_intent(before, 1, "second")
    )
    assert after.documents[1] == GraphStagePromotionDocuments(
        stage_ids[1], replaced, None, expected_intent(after, 1, "second")
    )
    assert ledger.checkouts == 3
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_promotion_snapshot_cancellation_releases_native_resources(
    database: Database,
) -> None:
    binding, _ = await seed_documents(database)
    ledger = ConnectionLedger(database)
    projected = asyncio.Event()
    resume = asyncio.Event()
    engine = database.reader_engine.sync_engine
    projected_count = 0

    def after_projection(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal projected_count
        if " AS intent_present" in statement:
            projected_count += 1
            if projected_count == 2:
                projected.set()
                await_(resume.wait())

    event.listen(engine, "after_cursor_execute", after_projection)
    try:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(read(database, binding))
            try:
                await asyncio.wait_for(projected.wait(), CONDITION_DEADLINE_SECONDS)
                assert task.cancel()
            finally:
                resume.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await asyncio.wait_for(task, CONDITION_DEADLINE_SECONDS)
    finally:
        event.remove(engine, "after_cursor_execute", after_projection)
    checked_in = ledger.outstanding
    in_flight = database.units_in_flight()
    after = await read(database, binding)

    assert (
        checked_in == in_flight == ledger.outstanding == database.units_in_flight() == 0
    )
    assert ledger.checkouts == 2
    assert len(after.documents) == 3
    assert projected_count == 2
    assert cancelled.value.__context__ is cancelled.value.__cause__ is None
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(cancelled.value.__traceback__)
    }
    assert "_collect" not in frames
    assert "_project_stage" not in frames
    assert "_read_snapshot" not in frames
    assert "read_graph_stage_promotion_snapshot" not in frames
