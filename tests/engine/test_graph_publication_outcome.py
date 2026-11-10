import asyncio
from dataclasses import replace
from traceback import format_exception, walk_tb
from typing import cast

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationSettlement,
    read_graph_preparation_settlement,
)
from kora.db.tables import (
    graph,
    graph_deployment_preparation,
    graph_deployment_preparation_capacity,
    graph_version,
)
from kora.engine.graph_preparation_admission import GraphPreparationAdmissionOutcome
from kora.engine.graph_publication_outcome import (
    GraphPublicationOutcome,
    GraphPublicationOutcomeRejected,
    record_graph_publication_outcome,
)
from kora.engine.locks import TransitionLocks
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE
from tests.engine.test_graph_preparation_admission import reserve, seed_binding
from tests.engine.test_graph_publication_target import publication_scopes
from tests.engine.test_preparation_capacity_lock import insert_ledger

__all__ = ["kora_capture"]

OPEN = GraphPreparationSettlement("open", None, None)


async def admitted(
    database: Database, *, bound: bool
) -> tuple[GraphPreparationBinding, str, GraphPreparationBinding]:
    await insert_ledger(database, 0)
    binding, selected = await seed_binding(database, "private_outcome", bound=bound)
    sibling, sibling_id = await seed_binding(database, "private_sibling")
    assert (
        await reserve(database, binding, selected, 2)
        is GraphPreparationAdmissionOutcome.PREPARED
    )
    assert (
        await reserve(database, sibling, sibling_id, 2)
        is GraphPreparationAdmissionOutcome.PREPARED
    )
    return binding, selected, sibling


async def publication_guard(
    writer: AsyncConnection, binding: GraphPreparationBinding, selected: str
) -> TransitionLocks:
    locks = TransitionLocks(writer)
    await locks.acquire_scopes(publication_scopes(binding, selected))
    await locks.acquire_existing_storage_addresses((), max_addresses=0)
    await locks.acquire_publication_graph_preparation(writer, binding, selected)
    return locks


async def prepared_version(
    writer: AsyncConnection, binding: GraphPreparationBinding, selected: str
) -> str:
    if binding.initial_graph_id is not None:
        version: object = await writer.scalar(
            select(graph_version.c.id).where(graph_version.c.graph_id == selected)
        )
        assert type(version) is str
        return version

    seeder = Seeder(writer)
    await writer.execute(
        graph.insert().values(
            id=selected,
            namespace_id=binding.namespace_id,
            name=binding.graph_name,
            **seeder.stamps(),
        )
    )
    return await seeder.graph_version(selected)


async def state(
    reader: AsyncConnection,
    binding: GraphPreparationBinding,
    sibling: GraphPreparationBinding,
    selected: str,
    version: str,
) -> tuple[
    GraphPreparationSettlement | None,
    GraphPreparationSettlement | None,
    object,
    object,
    object,
]:
    original = await read_graph_preparation_settlement(reader, binding)
    other = await read_graph_preparation_settlement(reader, sibling)
    occupied: object = await reader.scalar(
        select(graph_deployment_preparation_capacity.c.occupied)
    )
    graph_id: object = await reader.scalar(
        select(graph.c.id).where(graph.c.id == selected)
    )
    version_id: object = await reader.scalar(
        select(graph_version.c.id).where(graph_version.c.id == version)
    )
    return original, other, occupied, graph_id, version_id


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True], ids=("published", "reused"))
@pytest.mark.parametrize("commit", [True, False], ids=("commit", "rollback"))
async def test_exact_publication_outcome_shares_the_writer_and_keeps_its_charge(
    database: Database, bound: bool, commit: bool
) -> None:
    binding, selected, sibling = await admitted(database, bound=bound)
    ledger = ConnectionLedger(database)
    outcome = (
        GraphPublicationOutcome.REUSED if bound else GraphPublicationOutcome.PUBLISHED
    )

    async with database.transaction() as writer:
        locks = await publication_guard(writer, binding, selected)
        version = await prepared_version(writer, binding, selected)
        with executed_queries(writer) as queries:
            await record_graph_publication_outcome(
                writer,
                locks,
                binding,
                graph_id=selected,
                version_id=version,
                outcome=outcome,
            )
        [(statement, arguments)] = queries
        explain = (
            "EXPLAIN QUERY PLAN " if database.dialect is Dialect.SQLITE else "EXPLAIN "
        )
        rows = await writer.exec_driver_sql(explain + statement, arguments)
        plan: list[tuple[object, ...]] = [tuple(row) for row in rows]
        owned = await state(writer, binding, sibling, selected, version)
        async with database.read_transaction() as reader:
            before = await state(reader, binding, sibling, selected, version)
        if not commit:
            with pytest.raises(GraphPublicationOutcomeRejected):
                await record_graph_publication_outcome(
                    writer,
                    locks,
                    binding,
                    graph_id=selected,
                    version_id=version,
                    outcome=outcome,
                )
            assert await state(writer, binding, sibling, selected, version) == owned
            await writer.rollback()
    async with database.read_transaction() as reader:
        after = await state(reader, binding, sibling, selected, version)

    completed = GraphPreparationSettlement(outcome.value, selected, version)
    original = (OPEN, OPEN, 2, selected if bound else None, version if bound else None)
    assert owned == (completed, OPEN, 2, selected, version)
    assert before == original
    assert after == (owned if commit else original)
    assert statement.lstrip().startswith("UPDATE ")
    if database.dialect is Dialect.SQLITE:
        assert "USING INDEX" in str(plan[0][3])
        assert "(id=?)" in str(plan[0][3])
    else:
        assert any(
            "Index Scan using pk_graph_deployment_preparation" in str(row[0])
            for row in plan
        )
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


def assert_private_failure(error: GraphPublicationOutcomeRejected) -> str:
    rendered = "".join(format_exception(error))
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
    }

    assert error.args == ("graph publication outcome rejected",)
    assert error.__cause__ is error.__context__ is None
    assert not frames["record_graph_publication_outcome"]
    assert "_record_publication_outcome" not in frames
    return rendered


@pytest.mark.asyncio
async def test_publication_outcome_refuses_wrong_input_before_dispatch(
    database: Database,
) -> None:
    binding, selected, sibling = await admitted(database, bound=True)
    ledger = ConnectionLedger(database)
    valid = GraphPublicationOutcome.REUSED

    async with database.transaction() as writer:
        version = await prepared_version(writer, binding, selected)
        unguarded = TransitionLocks(writer)
        with executed_queries(writer) as unguarded_queries:
            with pytest.raises(GraphPublicationOutcomeRejected) as without_guard:
                await record_graph_publication_outcome(
                    writer,
                    unguarded,
                    binding,
                    graph_id=selected,
                    version_id=version,
                    outcome=valid,
                )
        await unguarded.acquire_scopes(publication_scopes(binding, selected))
        await unguarded.acquire_existing_storage_addresses((), max_addresses=0)
        await unguarded.acquire_publication_graph_preparation(writer, binding, selected)
        attempts = (
            (sibling, selected, version, valid),
            (replace(binding, deployment_digest="f" * 64), selected, version, valid),
            (binding, sibling.namespace_id, version, valid),
            (binding, selected, "private-version", valid),
            (binding, selected, version, cast(GraphPublicationOutcome, "private-kind")),
        )
        errors: list[GraphPublicationOutcomeRejected] = [without_guard.value]
        with executed_queries(writer) as queries:
            for original, graph_id, version_id, kind in attempts:
                with pytest.raises(GraphPublicationOutcomeRejected) as refused:
                    await record_graph_publication_outcome(
                        writer,
                        unguarded,
                        original,
                        graph_id=graph_id,
                        version_id=version_id,
                        outcome=kind,
                    )
                errors.append(refused.value)

        async def write() -> None:
            await record_graph_publication_outcome(
                writer,
                unguarded,
                binding,
                graph_id=selected,
                version_id=version,
                outcome=valid,
            )

        with executed_queries(writer) as foreign_queries:
            with pytest.raises(GraphPublicationOutcomeRejected) as foreign:
                await asyncio.create_task(write())
        errors.append(foreign.value)
        substituted = replace(binding, deployment_digest="f" * 64)
        await writer.execute(
            graph_deployment_preparation.update()
            .where(graph_deployment_preparation.c.id == binding.preparation_id)
            .values(deployment_digest=substituted.deployment_digest)
        )
        with executed_queries(writer) as stale_queries:
            with pytest.raises(GraphPublicationOutcomeRejected) as stale:
                await write()
        errors.append(stale.value)
        still_open = await read_graph_preparation_settlement(writer, substituted)
        await writer.rollback()
        with executed_queries(writer) as expired_queries:
            with pytest.raises(GraphPublicationOutcomeRejected) as expired:
                await write()
        errors.append(expired.value)
    async with database.read_transaction() as reader:
        durable = await state(reader, binding, sibling, selected, version)

    assert not unguarded_queries
    assert not queries
    assert not foreign_queries
    assert not expired_queries
    assert len(stale_queries) == 1
    assert still_open == OPEN
    for error in errors:
        rendered = assert_private_failure(error)
        assert binding.preparation_id not in rendered
        assert "private-version" not in rendered
        assert "private-kind" not in rendered
    assert durable == (OPEN, OPEN, 2, selected, version)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_publication_outcome_native_failure_is_private(
    database: Database, kora_capture: Capture
) -> None:
    binding, selected, sibling = await admitted(database, bound=True)
    ledger = ConnectionLedger(database)
    async with database.read_transaction() as reader:
        version = await prepared_version(reader, binding, selected)

    async def unavailable_outcome() -> None:
        async with database.transaction() as writer:
            locks = await publication_guard(writer, binding, selected)
            await writer.execute(
                text(
                    "ALTER TABLE graph_deployment_preparation "
                    "RENAME COLUMN deployment_digest TO unavailable_outcome_digest"
                )
            )
            await record_graph_publication_outcome(
                writer,
                locks,
                binding,
                graph_id=selected,
                version_id=version,
                outcome=GraphPublicationOutcome.REUSED,
            )

    with pytest.raises(GraphPublicationOutcomeRejected) as refused:
        await unavailable_outcome()
    async with database.read_transaction() as reader:
        restored = await state(reader, binding, sibling, selected, version)
    rendered = assert_private_failure(refused.value)
    records = kora_capture.finish()

    assert sum(record["event"] == "db.transaction.failed" for record in records) == 1

    for private in (
        binding.preparation_id,
        binding.namespace_id,
        binding.graph_name,
        selected,
        version,
        binding.deployment_digest,
        "unavailable_outcome_digest",
    ):
        assert private not in rendered
        assert private not in repr(records)
    assert restored == (OPEN, OPEN, 2, selected, version)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_cancelled_publication_outcome_rolls_back_new_version_and_releases_writer(
    database: Database,
) -> None:
    binding, selected, sibling = await admitted(database, bound=False)
    ledger = ConnectionLedger(database)
    updated = asyncio.Event()
    unreleased = asyncio.Event()
    versions: list[str] = []

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if statement.lstrip().startswith(
            (
                "UPDATE OR ABORT main.graph_deployment_preparation",
                "UPDATE ONLY graph_deployment_preparation",
            )
        ):
            updated.set()
            await_(asyncio.wait_for(unreleased.wait(), DEADLINE))

    async def publish() -> None:
        async with database.transaction() as writer:
            locks = await publication_guard(writer, binding, selected)
            version = await prepared_version(writer, binding, selected)
            versions.append(version)
            event.listen(writer.sync_connection, "after_cursor_execute", pause)
            try:
                await record_graph_publication_outcome(
                    writer,
                    locks,
                    binding,
                    graph_id=selected,
                    version_id=version,
                    outcome=GraphPublicationOutcome.PUBLISHED,
                )
            finally:
                event.remove(writer.sync_connection, "after_cursor_execute", pause)

    task = asyncio.create_task(publish())
    try:
        await asyncio.wait_for(updated.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, DEADLINE)
    async with database.read_transaction() as reader:
        durable = await state(reader, binding, sibling, selected, versions[0])
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(cancelled.value.__traceback__)
    }

    assert cancelled.value.__cause__ is cancelled.value.__context__ is None
    assert not frames.get("record_graph_publication_outcome")
    assert "_record_publication_outcome" not in frames
    assert len(versions) == 1
    assert durable == (OPEN, OPEN, 2, None, None)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
