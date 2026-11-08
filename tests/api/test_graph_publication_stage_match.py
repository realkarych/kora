import asyncio
import pickle
from dataclasses import dataclass, replace
from typing import Literal

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.api.graph_execution_preparation import PreparedGraphDeploymentInputs
from kora.api.graph_publication_stage_match import (
    MatchedGraphStage,
    match_graph_publication_stages,
)
from kora.db.connection import Database
from kora.db.graph_deployment_stage_reads import GraphStageInventoryRejected
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_value_json import ObjectReader, TextBudget, render_object
from kora.db.tables import graph_deployment_preparation, storage_address_fence
from kora.db.tables import graph_deployment_stage as stage_table
from kora.db.tables import graph_deployment_stage_occurrence as occurrence_table
from kora.engine.graph_command_manifests import PreparedCommandStageGroup
from kora.engine.locks import LockError, TransitionLocks
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from tests.api.test_graph_deployment_attachment import attachment_inputs, prepare_inputs
from tests.api.test_graph_execution_preparation import (
    MARKER,
    PRIVATE_STORE,
    READ_LIMITS,
    deployment_source,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    PreparedScope,
    occurrence_values,
    stage_values,
)
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.lock_support import DEADLINE, lock_inventory
from tests.engine.test_graph_publication_target import publication_scopes
from tests.engine.test_graph_resource_references import SCOPE

Address = tuple[str, str]
CheckedPair = tuple[PreparedGraphDeploymentInputs, PreparedGraphDeploymentInputs]
Mismatch = Literal["missing", "extra", "size", "store", "digest", "ordinals"]
EXTRA_ADDRESS: Address = ("Unused-Publication-Store", "sha256:" + "e" * 64)


@dataclass(frozen=True)
class StagedDeployment:
    binding: GraphPreparationBinding
    other: GraphPreparationBinding
    graph_id: str
    addresses: tuple[Address, ...]
    stages: tuple[MatchedGraphStage, ...]


@pytest.fixture(scope="module")
def checked_inputs() -> CheckedPair:
    ir, files, _ = attachment_inputs(SCOPE)
    other_ir = ObjectReader(ir).object()
    other_ir["x-signed-zero"] = 0.0
    return (
        prepare_inputs(ir, files),
        prepare_inputs(
            render_object(other_ir, TextBudget(READ_LIMITS.identity.max_encoded_bytes)),
            files,
        ),
    )


@pytest.fixture(scope="module")
def empty_checked() -> PreparedGraphDeploymentInputs:
    return prepare_inputs(deployment_source(()), ())


async def seed_deployment(
    database: Database,
    checked: PreparedGraphDeploymentInputs,
    mismatch: Mismatch | None = None,
) -> StagedDeployment:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        scope = PreparedScope(namespace_id, seeder.new_id())
        binding = GraphPreparationBinding(
            scope.preparation_id,
            namespace_id,
            "pipeline",
            None,
            checked.logical.digests.deployment_digest,
        )
        other = replace(binding, preparation_id=seeder.new_id())
        await connection.execute(
            graph_deployment_preparation.insert(),
            [
                {
                    "id": original.preparation_id,
                    "namespace_id": namespace_id,
                    "graph_name": original.graph_name,
                    "initial_graph_id": None,
                    "deployment_digest": original.deployment_digest,
                    "state": "open",
                }
                for original in (binding, other)
            ],
        )
        addresses = tuple(
            (group.store_id, group.payload_digest) for group in checked.groups
        )
        if addresses:
            addresses = (*addresses, EXTRA_ADDRESS)
        fences: dict[Address, int] = {}
        for store, digest in addresses:
            incarnation: object = await connection.scalar(
                storage_address_fence.insert()
                .values(
                    store_id=store, payload_digest=digest, last_deletion_generation=0
                )
                .returning(storage_address_fence.c.fence_incarnation)
            )
            assert type(incarnation) is int
            fences[(store, digest)] = incarnation
        stages = tuple(
            MatchedGraphStage(
                seeder.new_id(),
                group.store_id,
                group.payload_digest,
                fences[(group.store_id, group.payload_digest)],
                group.total_size,
                group.ordinals,
            )
            for group in checked.groups
        )
        for index, stage in enumerate(stages):
            if index == 0 and mismatch == "missing":
                continue
            row = stage_values(scope, stage.stage_id, stage.fence_incarnation)
            row.update(
                store_id=EXTRA_ADDRESS[0]
                if index == 0 and mismatch == "store"
                else stage.store_id,
                payload_digest=EXTRA_ADDRESS[1]
                if index == 0 and mismatch == "digest"
                else stage.payload_digest,
                total_size=stage.total_size + (index == 0 and mismatch == "size"),
            )
            await connection.execute(stage_table.insert().values(**row))
            ordinals = stage.ordinals
            if mismatch == "ordinals":
                ordinals = tuple(sorted((stages[1 - index].ordinals[0], *ordinals[1:])))
            await connection.execute(
                occurrence_table.insert(),
                [
                    occurrence_values(scope, stage.stage_id, ordinal)
                    for ordinal in ordinals
                ],
            )
        if mismatch == "extra":
            extra = seeder.new_id()
            await connection.execute(
                stage_table.insert().values(
                    **{
                        **stage_values(scope, extra, fences[EXTRA_ADDRESS]),
                        "store_id": EXTRA_ADDRESS[0],
                        "payload_digest": EXTRA_ADDRESS[1],
                    }
                )
            )
            await connection.execute(
                occurrence_table.insert().values(**occurrence_values(scope, extra, 3))
            )
    return StagedDeployment(binding, other, graph_id, addresses, stages)


async def writer_locks(
    connection: AsyncConnection,
    staged: StagedDeployment,
    addresses: tuple[Address, ...],
) -> TransitionLocks:
    locks = TransitionLocks(connection)
    await locks.acquire_scopes(publication_scopes(staged.binding, staged.graph_id))
    await locks.acquire_existing_storage_addresses(
        addresses, max_addresses=len(addresses)
    )
    await locks.acquire_publication_graph_preparation(
        connection, staged.binding, staged.graph_id
    )
    return locks


def stage_facts(
    stage: MatchedGraphStage,
) -> tuple[str, str, str, int, int, tuple[int, ...]]:
    return (
        stage.stage_id,
        stage.store_id,
        stage.payload_digest,
        stage.fence_incarnation,
        stage.total_size,
        stage.ordinals,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True], ids=["repeated-and-zero", "empty"])
async def test_publication_matches_exact_original_stages(
    database: Database,
    checked_inputs: CheckedPair,
    empty_checked: PreparedGraphDeploymentInputs,
    empty: bool,
) -> None:
    checked = empty_checked if empty else checked_inputs[0]
    staged = await seed_deployment(database, checked)
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        locks = await writer_locks(connection, staged, staged.addresses)
        transaction = connection.get_transaction()
        with executed_queries(connection) as queries:
            matched = await match_graph_publication_stages(
                connection, locks, staged.binding, staged.graph_id, checked
            )
        still_current = connection.get_transaction() is transaction

    assert tuple(map(stage_facts, matched)) == tuple(map(stage_facts, staged.stages))
    assert still_current
    assert len(queries) == 2 + len(staged.stages)
    assert all("FROM graph_deployment_stage" in statement for statement, _ in queries)
    if not empty:
        assert {(stage.total_size, stage.ordinals) for stage in matched} == {
            (0, (1,)),
            (len(MARKER), (0, 2)),
        }
        assert PRIVATE_STORE not in repr(matched)
        with pytest.raises(
            TypeError, match="matched graph stage requires explicit access"
        ):
            pickle.dumps(matched)
        with pytest.raises(
            ValueError, match="MatchedGraphStage.*not iterable"
        ) as generic_refusal:
            jsonable_encoder(matched)
        assert PRIVATE_STORE not in str(generic_refusal.value)
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["missing", "extra", "size", "store", "digest", "ordinals"]
)
async def test_publication_refuses_reader_valid_inventory_mismatch(
    database: Database,
    checked_inputs: CheckedPair,
    mismatch: Mismatch,
) -> None:
    checked = checked_inputs[0]
    staged = await seed_deployment(database, checked, mismatch)
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        locks = await writer_locks(connection, staged, staged.addresses)
        with executed_queries(connection) as queries:
            with pytest.raises(GraphStageInventoryRejected) as refusal:
                await match_graph_publication_stages(
                    connection, locks, staged.binding, staged.graph_id, checked
                )

    assert str(refusal.value) == "graph deployment stage inventory rejected"
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert len(queries) == 2 + 2 + (mismatch == "extra") - (mismatch == "missing")
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem", ["digest", "address", "preparation", "connection", "request-bytes"]
)
async def test_publication_requires_exact_precheck_before_stage_sql(
    database: Database,
    checked_inputs: CheckedPair,
    problem: str,
) -> None:
    checked, other_checked = checked_inputs
    staged = await seed_deployment(database, checked)
    assert (
        checked.logical.digests.deployment_digest
        != other_checked.logical.digests.deployment_digest
    )
    assert tuple(
        (group.payload_digest, group.ordinals) for group in checked.groups
    ) == tuple((group.payload_digest, group.ordinals) for group in other_checked.groups)
    incomplete = staged.addresses[:-2] if problem == "address" else staged.addresses
    binding = staged.other if problem == "preparation" else staged.binding
    candidate = other_checked if problem == "digest" else checked
    if problem == "request-bytes":
        group = checked.groups[0]
        oversized = PreparedCommandStageGroup(
            group.store_id,
            group.payload_digest,
            b"x" * COMMAND_RESOURCE_MAX_BYTES,
            (0, 1, 2, 3, 4),
        )
        candidate = PreparedGraphDeploymentInputs(checked.logical, (oversized,))
    error = (
        GraphStageInventoryRejected
        if problem in ("digest", "request-bytes")
        else LockError
    )
    ledger = ConnectionLedger(database)

    async with database.read_transaction() as reader, database.transaction() as writer:
        locks = await writer_locks(writer, staged, incomplete)
        connection = reader if problem == "connection" else writer
        with executed_queries(connection) as queries:
            with pytest.raises(error):
                await match_graph_publication_stages(
                    connection, locks, binding, staged.graph_id, candidate
                )

    assert queries == []
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_cancelled_stage_match_releases_caller_writer(
    database: Database,
    checked_inputs: CheckedPair,
) -> None:
    checked = checked_inputs[0]
    staged = await seed_deployment(database, checked)
    reading = asyncio.Event()
    unreleased = asyncio.Event()
    ledger = ConnectionLedger(database)

    def pause(
        connection: object, cursor: object, statement: str, *args: object
    ) -> None:
        if "FROM graph_deployment_stage_occurrence" in statement:
            reading.set()
            await_(asyncio.wait_for(unreleased.wait(), DEADLINE))

    async def match_until_cancelled() -> None:
        async with database.transaction() as connection:
            locks = await writer_locks(connection, staged, staged.addresses)
            event.listen(connection.sync_connection, "before_cursor_execute", pause)
            try:
                await match_graph_publication_stages(
                    connection, locks, staged.binding, staged.graph_id, checked
                )
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", pause)

    task = asyncio.create_task(match_until_cancelled())
    try:
        await asyncio.wait_for(reading.wait(), DEADLINE)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)
    async with database.transaction() as connection:
        await writer_locks(connection, staged, staged.addresses)

    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
