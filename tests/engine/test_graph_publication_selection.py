import asyncio
from dataclasses import replace
from traceback import walk_tb
from typing import Literal

import pytest
from sqlalchemy import event, update
from sqlalchemy.util import await_

from kora.db.connection import Database
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.tables import graph, graph_version
from kora.db.transactions import WriterTransactionError
from kora.engine.locks import (
    FirstGraphPublication,
    GraphPublicationCorrectionCandidate,
    GraphPublicationDigestCandidate,
    GraphPublicationDigestUnavailable,
    GraphPublicationObservation,
    GraphPublicationOrdinalObservation,
    GraphPublicationOrdinalUnavailable,
    GraphPublicationTargetUnavailable,
    LockError,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, lock_inventory
from tests.engine.test_graph_publication_target import publication_locks, seed_target

OTHER_DIGEST = "e" * 64
INVALID_ID = "8" + "Z" * 25
Queries = list[tuple[str, tuple[object, ...]]]


async def observe(
    database: Database,
    binding: GraphPreparationBinding,
    selected: str,
    captured: Queries,
) -> GraphPublicationObservation:
    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        with executed_queries(writer) as queries:
            try:
                return await locks.observe_graph_publication(writer, binding, selected)
            finally:
                captured.extend(queries)


def statements(queries: Queries) -> list[str]:
    kinds: list[str] = []
    for sql, _ in queries:
        assert sql.lstrip().startswith("SELECT")
        if "graph.namespace_id" in sql:
            kinds.append("target")
        elif "deployment_digest" in sql:
            kinds.append("digest")
        else:
            assert "graph_version" in sql
            assert "number" in sql
            kinds.append("ordinal")
    return kinds


async def assert_release(database: Database, ledger: ConnectionLedger) -> None:
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()
    assert await lock_inventory(database) == []


def assert_private_refusal(error: BaseException, *values: str) -> None:
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
    }
    assert frames["observe_graph_publication"] == {}
    assert error.__cause__ is error.__context__ is None
    assert all(value not in str(error) for value in values)


@pytest.mark.asyncio
async def test_publication_observation_selects_one_guarded_branch(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        prospective = seeder.new_id()
        first_version = await seeder.graph_version(selected)
        later = await seeder.graph_version(selected, 9)
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == later)
            .values(deployment_digest="d" * 64)
        )
        empty = await seeder.graph(binding.namespace_id, "empty")
    ledger = ConnectionLedger(database)
    bound = replace(binding, initial_graph_id=selected)
    missing = replace(binding, graph_name="absent")
    different = replace(bound, deployment_digest=OTHER_DIGEST)
    cases = (
        (missing, prospective, FirstGraphPublication(prospective), ["target"]),
        (
            binding,
            prospective,
            GraphPublicationCorrectionCandidate(selected),
            ["target"],
        ),
        (
            binding,
            selected,
            GraphPublicationDigestCandidate(first_version),
            ["target", "digest"],
        ),
        (
            different,
            selected,
            GraphPublicationOrdinalObservation(selected, 9),
            ["target", "digest", "ordinal"],
        ),
        (
            replace(binding, graph_name="empty"),
            empty,
            GraphPublicationOrdinalObservation(empty, None),
            ["target", "digest", "ordinal"],
        ),
    )

    for request, graph_id, expected, sequence in cases:
        observed_queries: Queries = []
        before = ledger.checkouts

        observed = await observe(database, request, graph_id, observed_queries)

        assert observed == expected
        assert statements(observed_queries) == sequence
        assert all(
            identity not in repr(observed)
            for identity in (graph_id, selected, first_version)
        )
        assert ledger.checkouts - before == 1
        await assert_release(database, ledger)

    inconsistent = (
        (replace(missing, initial_graph_id=selected), selected),
        (replace(binding, initial_graph_id=prospective), prospective),
    )
    for request, graph_id in inconsistent:
        refusal_queries: Queries = []
        before = ledger.checkouts

        with pytest.raises(GraphPublicationTargetUnavailable) as refused:
            await observe(database, request, graph_id, refusal_queries)

        assert statements(refusal_queries) == ["target"]
        assert_private_refusal(refused.value, graph_id, request.graph_name, DIGEST)
        assert ledger.checkouts - before == 1
        await assert_release(database, ledger)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["target", "digest", "ambiguous", "ordinal"])
async def test_publication_observation_preserves_native_refusals(
    database: Database,
    corruption: Literal["target", "digest", "ambiguous", "ordinal"],
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        first_version: str | None = None
        if corruption == "target":
            await writer.execute(
                update(graph).where(graph.c.id == selected).values(id=INVALID_ID)
            )
            request = binding
            expected = GraphPublicationTargetUnavailable
            sequence = ["target"]
        else:
            first_version = await seeder.graph_version(selected, 3)
            if corruption == "digest":
                await writer.execute(
                    update(graph_version)
                    .where(graph_version.c.id == first_version)
                    .values(id=INVALID_ID)
                )
            elif corruption == "ambiguous":
                await seeder.graph_version(selected, 4)
            else:
                invalid_ordinal = await seeder.graph_version(selected, -1)
                await writer.execute(
                    update(graph_version)
                    .where(graph_version.c.id == invalid_ordinal)
                    .values(deployment_digest="d" * 64)
                )
            request = replace(
                binding,
                deployment_digest=OTHER_DIGEST if corruption == "ordinal" else DIGEST,
            )
            expected = (
                GraphPublicationOrdinalUnavailable
                if corruption == "ordinal"
                else GraphPublicationDigestUnavailable
            )
            sequence = ["target", "digest"]
            if corruption == "ordinal":
                sequence.append("ordinal")
    ledger = ConnectionLedger(database)
    observed_queries: Queries = []

    with pytest.raises(expected) as refused:
        await observe(database, request, selected, observed_queries)

    assert statements(observed_queries) == sequence
    assert_private_refusal(refused.value, INVALID_ID, selected, DIGEST)
    assert ledger.checkouts == 1
    await assert_release(database, ledger)

    if corruption == "ordinal":
        assert first_version is not None
        hit_queries: Queries = []
        before = ledger.checkouts

        hit = await observe(database, binding, selected, hit_queries)

        assert hit == GraphPublicationDigestCandidate(first_version)
        assert statements(hit_queries) == ["target", "digest"]
        assert ledger.checkouts - before == 1
        await assert_release(database, ledger)


@pytest.mark.asyncio
async def test_publication_observation_rejects_wrong_writer_scope_and_phase(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        locks = await publication_locks(writer, binding, selected)
        other_scope = replace(binding, namespace_id=Seeder(writer).new_id())
        with executed_queries(writer) as denied:
            with pytest.raises(LockError, match="required transaction locks"):
                await locks.observe_graph_publication(writer, other_scope, selected)
            with pytest.raises(WriterTransactionError, match="not current"):
                await asyncio.create_task(
                    locks.observe_graph_publication(writer, binding, selected)
                )
        async with database.read_transaction() as reader:
            with executed_queries(reader) as foreign:
                with pytest.raises(LockError, match="does not own"):
                    await locks.observe_graph_publication(reader, binding, selected)
        await locks.acquire_existing_storage_addresses((), max_addresses=0)
        with executed_queries(writer) as late:
            with pytest.raises(LockError, match="precedes storage addresses"):
                await locks.observe_graph_publication(writer, binding, selected)

    assert denied == foreign == late == []
    await assert_release(database, ledger)


@pytest.mark.asyncio
async def test_publication_observation_uses_one_original_binding_across_target_wait(
    database: Database,
) -> None:
    binding, selected = await seed_target(database)
    async with database.transaction() as writer:
        version_id = await Seeder(writer).graph_version(selected)
    ledger = ConnectionLedger(database)
    target_dispatched = asyncio.Event()
    resume_target = asyncio.Event()
    queries: Queries = []

    def pause_target(
        connection: object, cursor: object, sql: str, *arguments: object
    ) -> None:
        if "graph.namespace_id" in sql:
            target_dispatched.set()
            await_(asyncio.wait_for(resume_target.wait(), DEADLINE))

    async def read_paused_target() -> GraphPublicationObservation:
        async with database.transaction() as writer:
            locks = await publication_locks(writer, binding, selected)
            with executed_queries(writer) as observed:
                event.listen(
                    writer.sync_connection, "before_cursor_execute", pause_target
                )
                try:
                    return await locks.observe_graph_publication(
                        writer, binding, selected
                    )
                finally:
                    event.remove(
                        writer.sync_connection, "before_cursor_execute", pause_target
                    )
                    queries.extend(observed)

    owner = asyncio.create_task(read_paused_target())
    try:
        await asyncio.wait_for(target_dispatched.wait(), DEADLINE)
        binding.__init__(
            binding.preparation_id,
            binding.namespace_id,
            "changed",
            binding.initial_graph_id,
            OTHER_DIGEST,
        )
    finally:
        resume_target.set()
        result = await asyncio.wait_for(owner, DEADLINE)

    assert result == GraphPublicationDigestCandidate(version_id)
    assert statements(queries) == ["target", "digest"]
    assert "pipeline" in queries[0][1]
    assert DIGEST in queries[1][1]
    assert OTHER_DIGEST not in queries[1][1]
    assert ledger.checkouts == 1
    await assert_release(database, ledger)
