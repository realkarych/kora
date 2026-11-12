import asyncio
from hashlib import sha256
from typing import cast

import pytest
from sqlalchemy import BigInteger, LargeBinary, Text, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement

from kora.api.graph_deployment import (
    DeploymentFindings,
    DeploymentRefusal,
    prepare_graph_deployment,
)
from kora.api.graph_deployment_limits import IR_BYTES, READ
from kora.api.graph_deployment_writer import (
    GraphDeploymentTarget,
    PublishedGraphVersion,
    publish_graph_deployment,
)
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database, Dialect
from kora.db.graph_read_admission import read_graph_version_admission
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueRejected
from kora.db.graph_version_reads import MAX_SQL_LENGTH, acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import graph, graph_version, graph_version_resource_owner
from kora.engine.graph_version_identity import (
    GraphVersionPin,
    read_graph_version_identity,
)
from kora.ir.json import JsonObject
from tests.api.deployment_cases import (
    AT,
    GRAPH,
    OWNER,
    SELECTION,
    SELECTORS,
    evaluator,
    prepared,
    principal,
    publish,
    seeded_namespace,
    skeleton_ir,
    unresolved_approval,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, Seeder, fixed_randomness

__all__ = ["evaluator"]

DOCUMENT_COLUMNS = frozenset(
    {"ir", "execution_contract", "resolved_digests", "resources"}
)
EMPTY_RESOURCES: JsonObject = {
    "interpretation": "kora.graph-resource-refs.v1",
    "commands": [],
}


def nodes_of(ir: JsonObject) -> dict[str, dict[str, dict[str, object]]]:
    return cast(
        "dict[str, dict[str, dict[str, object]]]",
        cast("JsonObject", ir["graph"])["nodes"],
    )


def with_prompt(prompt: str) -> JsonObject:
    ir = skeleton_ir()
    nodes_of(ir)["agent"]["config"]["prompt"] = prompt
    return ir


async def row_counts(database: Database) -> tuple[int, int]:
    async with database.read_transaction() as reader:
        graphs = await reader.scalar(select(func.count()).select_from(graph))
        versions = await reader.scalar(select(func.count()).select_from(graph_version))
    return int(graphs or 0), int(versions or 0)


def stored_widths(dialect: Dialect) -> tuple[ColumnElement[int], ColumnElement[int]]:
    if dialect is Dialect.POSTGRESQL:
        total = sum(
            (
                func.pg_column_size(
                    sql_cast(sql_cast(column, Text()), JSONB())
                    if column.name in DOCUMENT_COLUMNS
                    else column,
                    type_=BigInteger(),
                )
                for column in graph_version.columns
            ),
            start=cast("ColumnElement[int]", sql_cast(0, BigInteger())),
        )
        transport = func.octet_length(
            graph_resource_binary_projection(dialect, graph_version.c.resources),
            type_=BigInteger(),
        )
        return total, transport
    total = sum(
        (
            func.length(sql_cast(column, LargeBinary()), type_=BigInteger())
            for column in graph_version.columns
        ),
        start=cast("ColumnElement[int]", sql_cast(0, BigInteger())),
    )
    transport = func.length(
        graph_resource_binary_projection(dialect, graph_version.c.resources),
        type_=BigInteger(),
    )
    return total, transport


@pytest.mark.asyncio
async def test_deployment_preparation_returns_preimage_digests(
    evaluator: Evaluator,
) -> None:
    ir = skeleton_ir()

    deployment = await prepared(evaluator, ir)

    logical = deployment.inputs.logical
    pair = (
        b'{"ir":'
        + logical.canonical_ir.encode("ascii")
        + b',"execution_contract":'
        + logical.canonical_contract.encode("ascii")
        + b"}"
    )
    assert deployment.graph_name == GRAPH
    assert (
        logical.digests.ir_digest == sha256(logical.canonical_ir.encode()).hexdigest()
    )
    assert (
        logical.digests.execution_contract_digest
        == sha256(logical.canonical_contract.encode()).hexdigest()
    )
    assert logical.digests.deployment_digest == sha256(pair).hexdigest()
    assert deployment.source_digest == logical.digests.ir_digest
    assert deployment.warnings.errors == ()


@pytest.mark.asyncio
async def test_deployment_preparation_keeps_the_authored_source_digest(
    evaluator: Evaluator,
) -> None:
    authored = sha256(b"graph sources").hexdigest()
    ir = skeleton_ir()
    ir["meta"] = {"source_digest": "sha256:" + authored}

    deployment = await prepared(evaluator, ir)

    assert deployment.source_digest == authored


@pytest.mark.asyncio
async def test_deployment_preparation_refuses_before_preparing(
    evaluator: Evaluator,
) -> None:
    foreign = skeleton_ir(namespace="other")
    renamed = skeleton_ir(graph="other")
    unselected = skeleton_ir(harness="claude")
    malformed = skeleton_ir()
    malformed["graph"] = "not a graph"
    oversized = with_prompt("x" * IR_BYTES)
    unresolved = unresolved_approval()

    outcomes = [
        await prepare_graph_deployment(ir, SELECTORS, SELECTION, evaluator)
        for ir in (foreign, renamed, unselected, malformed, oversized, unresolved)
    ]

    assert outcomes[:2] == [DeploymentRefusal.SELECTORS, DeploymentRefusal.SELECTORS]
    assert isinstance(outcomes[2], DeploymentFindings)
    assert [error.code for error in outcomes[2].diagnostics.errors] == ["IR018"]
    assert outcomes[3:] == [
        DeploymentRefusal.DOCUMENT,
        DeploymentRefusal.TOO_LARGE,
        DeploymentRefusal.INCOMPLETE,
    ]


@pytest.mark.asyncio
async def test_deployment_preparation_admits_documents_within_its_budgets(
    evaluator: Evaluator,
) -> None:
    admitted = with_prompt("x" * 700_000)
    beyond = with_prompt("x" * 1_200_000)

    within = await prepare_graph_deployment(admitted, SELECTORS, SELECTION, evaluator)
    exhausted = await prepare_graph_deployment(beyond, SELECTORS, SELECTION, evaluator)

    assert not isinstance(within, DeploymentRefusal | DeploymentFindings)
    assert exhausted is DeploymentRefusal.INCOMPLETE


@pytest.mark.asyncio
async def test_deploy_writer_publishes_first_graph_without_files(
    database: Database, evaluator: Evaluator
) -> None:
    namespace_id, token_id = await seeded_namespace(database)
    deployment = await prepared(evaluator)

    published = await publish(database, deployment, namespace_id, token_id)

    total, transport = stored_widths(database.dialect)
    async with database.read_transaction() as reader:
        graph_row = (await reader.execute(select(graph))).one()
        version = (await reader.execute(select(graph_version))).one()
        widths = (await reader.execute(select(total, transport))).one()
        owners = await reader.scalar(
            select(func.count()).select_from(graph_version_resource_owner)
        )
        admission = await read_graph_version_admission(
            reader,
            namespace_id,
            graph_row.id,
            published.graph_version_id,
            max_db_input_bytes=MAX_SQL_LENGTH,
            max_resource_transport_bytes=MAX_SQL_LENGTH,
        )
        acquired = await acquire_graph_version(
            reader,
            namespace_id,
            graph_row.id,
            published.graph_version_id,
            max_encoded_bytes=READ.identity.max_encoded_bytes,
        )
    digests = deployment.inputs.logical.digests
    assert published == PublishedGraphVersion(version.id, 1, digests, False)
    assert (graph_row.namespace_id, graph_row.name) == (namespace_id, GRAPH)
    assert version.graph_id == graph_row.id
    assert (version.number, version.status, version.deployed_by) == (1, "active", OWNER)
    assert version.resources == EMPTY_RESOURCES
    assert version.source_digest == digests.ir_digest
    assert owners == 0
    assert admission is not None
    assert admission.db_input_bytes >= int(widths[0])
    assert admission.resource_transport_bytes == int(widths[1])
    assert acquired is not None
    identity = read_graph_version_identity(
        acquired,
        GraphVersionPin(graph_row.id, version.id, digests.execution_contract_digest),
        limits=READ.identity,
    )
    assert identity.canonical_ir == deployment.inputs.logical.canonical_ir
    assert identity.canonical_contract == deployment.inputs.logical.canonical_contract


@pytest.mark.asyncio
async def test_deploy_writer_reuses_equal_version(
    database: Database, evaluator: Evaluator
) -> None:
    namespace_id, token_id = await seeded_namespace(database)
    first = await publish(database, await prepared(evaluator), namespace_id, token_id)

    again = await publish(database, await prepared(evaluator), namespace_id, token_id)
    changed = await publish(
        database,
        await prepared(evaluator, with_prompt("Summarize the changes.")),
        namespace_id,
        token_id,
    )

    assert again == PublishedGraphVersion(
        first.graph_version_id, 1, first.digests, True
    )
    assert changed.number == 2
    assert not changed.reused
    assert changed.digests.deployment_digest != first.digests.deployment_digest
    assert await row_counts(database) == (1, 2)


@pytest.mark.asyncio
async def test_deploy_writer_concurrent_first_publications(
    database: Database, evaluator: Evaluator
) -> None:
    equal_namespace, token_id = await seeded_namespace(database)
    different_namespace, _ = await seeded_namespace(database, "different")
    original = await prepared(evaluator)
    changed = await prepared(evaluator, with_prompt("Summarize the changes."))

    equal = await asyncio.gather(
        publish(database, original, equal_namespace, token_id),
        publish(database, original, equal_namespace, token_id),
    )
    different = await asyncio.gather(
        publish(database, original, different_namespace, token_id),
        publish(database, changed, different_namespace, token_id),
    )

    assert equal[0].graph_version_id == equal[1].graph_version_id
    assert sorted(result.reused for result in equal) == [False, True]
    assert different[0].graph_version_id != different[1].graph_version_id
    assert sorted(result.number for result in different) == [1, 2]
    assert not any(result.reused for result in different)
    assert await row_counts(database) == (2, 3)


@pytest.mark.asyncio
async def test_deploy_writer_rolls_back_and_releases(
    database: Database, evaluator: Evaluator
) -> None:
    namespace_id, token_id = await seeded_namespace(database)
    upcoming = IdentifierGenerator(FakeClock(AT).now, fixed_randomness(7))
    graph_id, colliding_version = upcoming.new(), upcoming.new()
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        other = await seeder.graph(namespace_id, "other")
        await seeder.graph_version(other, version_id=colliding_version)
    ledger = ConnectionLedger(database)
    deployment = await prepared(evaluator)

    with pytest.raises(IntegrityError):
        await publish_graph_deployment(
            database,
            deployment,
            GraphDeploymentTarget(namespace_id, principal(namespace_id, token_id)),
            identifiers=IdentifierGenerator(FakeClock(AT).now, fixed_randomness(7)),
            at=AT,
        )

    async with database.read_transaction() as reader:
        graphs = (await reader.execute(select(graph.c.id))).scalars().all()
    assert graphs == [other]
    assert graph_id not in graphs
    assert await row_counts(database) == (1, 1)
    assert database.writer_is_free()
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_deploy_writer_refuses_a_service_principal_before_reuse(
    database: Database, evaluator: Evaluator
) -> None:
    namespace_id, token_id = await seeded_namespace(database)
    deployment = await prepared(evaluator)
    await publish(database, deployment, namespace_id, token_id)
    service = principal(namespace_id, token_id).model_copy(
        update={
            "kind": "service",
            "runtime": "local",
            "approve": False,
            "platform": False,
        }
    )

    with pytest.raises(GraphValueRejected):
        await publish_graph_deployment(
            database,
            deployment,
            GraphDeploymentTarget(namespace_id, service),
            identifiers=IdentifierGenerator(FakeClock(AT).now),
            at=AT,
        )

    assert await row_counts(database) == (1, 1)
