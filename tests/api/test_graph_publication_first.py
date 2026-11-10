import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from traceback import walk_tb

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    attach_graph_deployment_references,
)
from kora.api.graph_execution_preparation import (
    PreparedGraphDeploymentInputs,
    PreparedLogicalExecutionContract,
)
from kora.api.graph_publication_first import (
    first_graph_insert_statement,
    prepare_first_graph_values,
)
from kora.api.graph_publication_metadata import prepare_graph_publication_metadata
from kora.api.graph_publication_saved import (
    SAVED_LOCATOR,
    graph_publication_insert_statement,
    graph_publication_saved_statement,
)
from kora.api.graph_publication_values import prepare_graph_publication_binds
from kora.db.connection import Database, Dialect
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.identifiers import encode
from kora.db.tables import graph, graph_version
from kora.engine.graph_resource_references import GraphResourceScope
from tests.api.test_graph_deployment_attachment import ATTACH_LIMITS
from tests.api.test_graph_publication_metadata import personal_principal
from tests.api.test_graph_publication_saved import STAMP
from tests.api.test_graph_publication_values import (
    checked_attachment,
    publication_limits,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.test_graph_resource_references import assert_refused

OFFSET_STAMP = STAMP.astimezone(timezone(timedelta(hours=5, minutes=45)))


class AlternateDigest(str):
    pass


def name_bound_preparation(
    candidate: AttachedGraphDeployment, preparation_id: str
) -> GraphPreparationBinding:
    authored_graph = ObjectReader(candidate.canonical_ir).object()["graph"]
    assert type(authored_graph) is dict
    name = authored_graph["id"]
    assert type(name) is str
    return GraphPreparationBinding(
        preparation_id,
        candidate.scope_ids[0],
        name,
        None,
        candidate.digests.deployment_digest,
    )


async def observe_pair(
    connection: AsyncConnection, scope: GraphResourceScope
) -> tuple[tuple[object, ...] | None, object]:
    graph_row = (
        await connection.execute(
            select(
                graph.c.id,
                graph.c.namespace_id,
                graph.c.name,
                graph.c.created_at,
                graph.c.updated_at,
            ).where(graph.c.id == scope.graph_id)
        )
    ).one_or_none()
    version_id: object = await connection.scalar(
        select(graph_version.c.id).where(
            graph_version.c.graph_id == scope.graph_id,
            graph_version.c.number == 1,
        )
    )
    return tuple(graph_row) if graph_row is not None else None, version_id


def assert_saved_match(dialect: Dialect, marker: object) -> None:
    expected = 1 if dialect is Dialect.SQLITE else True
    assert type(marker) is type(expected)
    assert marker == expected


async def assert_conflicts_leave_incumbent(
    database: Database,
    scope: GraphResourceScope,
    expected: tuple[tuple[object, ...], str],
    values: dict[str, str | datetime],
    version: dict[str, object],
    other_id: str,
    scratch_id: str,
) -> None:
    insert = first_graph_insert_statement(database.dialect)
    conflicts = (
        values | {"id": other_id},
        values | {"name": "different_name"},
    )
    scratch = values | {"id": scratch_id, "name": "scratch_graph"}
    for conflict in conflicts:
        if database.dialect is Dialect.SQLITE:
            async with database.transaction() as writer:
                marker: object = await writer.scalar(insert, scratch)

                with pytest.raises(IntegrityError):
                    await writer.scalar(insert, conflict)

                assert type(marker) is int
                assert marker == 1
                assert (
                    await writer.scalar(
                        select(graph.c.id).where(graph.c.id == scratch_id)
                    )
                    == scratch_id
                )
                await writer.rollback()
        else:
            with pytest.raises(IntegrityError):
                async with database.transaction() as writer:
                    await writer.scalar(insert, conflict)

        async with database.read_transaction() as reader:
            incumbent = await observe_pair(reader, scope)
            saved: object = await reader.scalar(
                graph_publication_saved_statement(database.dialect), version
            )
            unused = (
                await reader.execute(
                    select(graph.c.id).where(graph.c.id.in_((other_id, scratch_id)))
                )
            ).all()

        assert incumbent == expected
        assert_saved_match(database.dialect, saved)
        assert not unused


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [True, False], ids=("commit", "rollback"))
async def test_first_graph_and_version_share_original_writer(
    database: Database, commit: bool
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        scope = GraphResourceScope(
            await seeder.namespace(), seeder.new_id(), seeder.new_id()
        )
        preparation_id, other_id, scratch_id = (seeder.new_id() for _ in range(3))
    candidate = await asyncio.to_thread(
        checked_attachment, with_files=False, scope=scope
    )
    binding = name_bound_preparation(candidate, preparation_id)
    graph_values = prepare_first_graph_values(candidate, binding, at=OFFSET_STAMP)
    payloads = prepare_graph_publication_binds(
        candidate, limits=publication_limits(candidate)
    )
    fixed = payloads.fixed_values(
        database.dialect, number=1, source_digest="f" * 64, at=OFFSET_STAMP
    )
    metadata = prepare_graph_publication_metadata(
        database.dialect, personal_principal("graph-owner"), (), max_metadata_bytes=128
    )
    version_values = fixed | payloads.parameters() | metadata.parameters()
    expected = (
        (scope.graph_id, scope.namespace_id, binding.graph_name, STAMP, STAMP),
        scope.version_id,
    )

    async with database.transaction() as writer:
        marker: object = await writer.scalar(
            first_graph_insert_statement(database.dialect), graph_values
        )
        locator: object = await writer.scalar(
            graph_publication_insert_statement(database.dialect), version_values
        )
        compared: object = await writer.scalar(
            graph_publication_saved_statement(database.dialect),
            version_values | {SAVED_LOCATOR: locator},
        )
        owned = await observe_pair(writer, scope)
        async with database.read_transaction() as reader:
            before_settlement = await observe_pair(reader, scope)
        if not commit:
            await writer.rollback()
    async with database.read_transaction() as reader:
        after_settlement = await observe_pair(reader, scope)
    if commit:
        await assert_conflicts_leave_incumbent(
            database,
            scope,
            expected,
            graph_values,
            version_values | {SAVED_LOCATOR: locator},
            other_id,
            scratch_id,
        )

    assert type(marker) is int
    assert marker == 1
    assert_saved_match(database.dialect, compared)
    assert owned == expected
    assert before_settlement == (None, None)
    assert after_settlement == (expected if commit else (None, None))
    assert database.units_in_flight() == ledger.outstanding == 0


def test_first_graph_values_use_sealed_scope_and_private_refusal() -> None:
    namespace_id, graph_id, version_id = encode(1), encode(2), encode(3)
    scope = GraphResourceScope(namespace_id, graph_id, version_id)
    candidate = checked_attachment(with_files=False, scope=scope)
    binding = name_bound_preparation(candidate, encode(4))
    wrong_bindings = (
        replace(binding, namespace_id=encode(5)),
        replace(binding, deployment_digest="a" * 64),
        replace(binding, initial_graph_id=graph_id),
        replace(binding, graph_name="private name"),
    )
    below_utc_floor = datetime(1000, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    alternate = PreparedLogicalExecutionContract(
        candidate.canonical_ir,
        candidate.canonical_contract,
        replace(
            candidate.digests,
            deployment_digest=AlternateDigest(binding.deployment_digest),
        ),
    )
    wrong_digest_type = attach_graph_deployment_references(
        PreparedGraphDeploymentInputs(alternate, ()), scope, (), limits=ATTACH_LIMITS
    )
    wrong_scope = checked_attachment(
        with_files=False,
        scope=GraphResourceScope(namespace_id, "private graph", version_id),
    )
    attempts = (
        *((candidate, item, OFFSET_STAMP) for item in wrong_bindings),
        (candidate, binding, datetime(2026, 11, 10)),
        (candidate, binding, below_utc_floor),
        (object(), binding, OFFSET_STAMP),
        (wrong_scope, binding, OFFSET_STAMP),
        (wrong_digest_type, binding, OFFSET_STAMP),
    )

    candidate.scope.__init__(encode(6), encode(7), encode(8))
    actual = prepare_first_graph_values(candidate, binding, at=OFFSET_STAMP)
    refusals: list[GraphValueRejected] = []
    for attachment, supplied_binding, timestamp in attempts:
        with pytest.raises(GraphValueRejected) as raised:
            prepare_first_graph_values(attachment, supplied_binding, at=timestamp)
        refusals.append(raised.value)

    assert actual == {
        "id": graph_id,
        "namespace_id": namespace_id,
        "name": binding.graph_name,
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    for refused in refusals:
        assert_refused(refused, GraphValueFailure.INVALID)
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(refused.__traceback__)
        }
        assert not frames["prepare_first_graph_values"]
        assert "_checked_first_graph_values" not in frames
