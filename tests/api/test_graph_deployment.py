import asyncio
from hashlib import sha256
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from kora.api.graph_deployment import (
    DeploymentFindings,
    DeploymentRefusal,
    PreparedDeployment,
    prepare_graph_deployment,
)
from kora.api.graph_deployment_limits import READ
from kora.api.graph_deployment_writer import (
    GraphDeploymentTarget,
    PublishedGraphVersion,
    publish_graph_deployment,
)
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.graph_read_admission import read_graph_version_admission
from kora.db.graph_version_reads import MAX_SQL_LENGTH, acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import graph, graph_version, graph_version_resource_owner
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.engine.graph_version_identity import (
    GraphVersionPin,
    read_graph_version_identity,
)
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.json import JsonObject
from tests.api.deployment_cases import (
    AT,
    GRAPH,
    HARNESS,
    NAMESPACE,
    OWNER,
    evaluator,
    principal,
    seeded_namespace,
    skeleton_ir,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, fixed_randomness

__all__ = ["evaluator"]

SELECTORS = GraphDeploymentSelectors(
    authorized_namespace_name=NAMESPACE, route_graph=GRAPH, request_graph=GRAPH
)
SELECTION = DeploymentSelection(HARNESS)
EMPTY_RESOURCES: JsonObject = {
    "interpretation": "kora.graph-resource-refs.v1",
    "commands": [],
}


async def prepared(
    evaluator: Evaluator, ir: JsonObject | None = None
) -> PreparedDeployment:
    result = await prepare_graph_deployment(
        skeleton_ir() if ir is None else ir, SELECTORS, SELECTION, evaluator
    )
    assert isinstance(result, PreparedDeployment)
    return result


async def publish(
    database: Database, deployment: PreparedDeployment, namespace_id: str, token_id: str
) -> PublishedGraphVersion:
    return await publish_graph_deployment(
        database,
        deployment,
        GraphDeploymentTarget(namespace_id, GRAPH, principal(namespace_id, token_id)),
        identifiers=IdentifierGenerator(FakeClock(AT).now),
        at=AT,
    )


def changed_ir() -> JsonObject:
    ir = skeleton_ir()
    nodes = cast(
        "dict[str, dict[str, dict[str, object]]]",
        cast("dict[str, object]", ir["graph"])["nodes"],
    )
    nodes["agent"]["config"]["prompt"] = "Summarize the changes."
    return ir


async def row_counts(database: Database) -> tuple[int, int]:
    async with database.read_transaction() as reader:
        graphs = await reader.scalar(select(func.count()).select_from(graph))
        versions = await reader.scalar(select(func.count()).select_from(graph_version))
    return int(graphs or 0), int(versions or 0)


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

    outcomes = [
        await prepare_graph_deployment(ir, SELECTORS, SELECTION, evaluator)
        for ir in (foreign, renamed, unselected, malformed)
    ]

    assert outcomes[:2] == [DeploymentRefusal.SELECTORS, DeploymentRefusal.SELECTORS]
    assert isinstance(outcomes[2], DeploymentFindings)
    assert [error.code for error in outcomes[2].diagnostics.errors] == ["IR018"]
    assert outcomes[3] == DeploymentRefusal.DOCUMENT


@pytest.mark.asyncio
async def test_deploy_writer_publishes_first_graph_without_files(
    database: Database, evaluator: Evaluator
) -> None:
    namespace_id, token_id = await seeded_namespace(database)
    deployment = await prepared(evaluator)

    published = await publish(database, deployment, namespace_id, token_id)

    async with database.read_transaction() as reader:
        graph_row = (await reader.execute(select(graph))).one()
        version = (await reader.execute(select(graph_version))).one()
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
        database, await prepared(evaluator, changed_ir()), namespace_id, token_id
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
    changed = await prepared(evaluator, changed_ir())

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
    ledger = ConnectionLedger(database)
    deployment = await prepared(evaluator)
    missing_namespace = IdentifierGenerator(
        FakeClock(AT).now, fixed_randomness(7)
    ).new()

    with pytest.raises(IntegrityError):
        await publish(database, deployment, missing_namespace, missing_namespace)

    assert await row_counts(database) == (0, 0)
    assert database.writer_is_free()
    assert ledger.outstanding == 0
