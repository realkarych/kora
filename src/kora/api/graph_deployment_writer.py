from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.exchange import COMPONENT
from kora.api.graph_deployment import PreparedDeployment
from kora.api.graph_deployment_attachment import attach_graph_deployment_references
from kora.api.graph_deployment_limits import ATTACHMENT, BINDS
from kora.api.graph_publication_first import first_graph_insert_statement
from kora.api.graph_publication_metadata import (
    PreparedGraphPublicationMetadata,
    prepare_graph_publication_metadata,
)
from kora.api.graph_publication_saved import (
    SAVED_LOCATOR,
    graph_publication_admission_statement,
    graph_publication_insert_statement,
    graph_publication_saved_statement,
)
from kora.api.graph_publication_sqlite import observe_sqlite_graph_publication
from kora.api.graph_publication_values import (
    PreparedGraphPublicationBinds,
    graph_publication_measurement_statement,
    prepare_graph_publication_binds,
)
from kora.db.connection import Database, Dialect
from kora.db.graph_deployment_reads import (
    active_version_by_deployment_digest,
    graph_id_by_name,
    latest_version_number,
)
from kora.db.graph_read_admission import GraphVersionReadAdmission
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.identifiers import IdentifierGenerator
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionDigests
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal

METADATA_BYTES = 4096
POSTGRESQL_FIXED_COLUMN_BYTES = 1024
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "api.graph_deployment.completed",
        COMPONENT,
        Level.DEBUG,
        "a graph deployment was published or reused",
        {"outcome": FieldKind.STRING},
    )
)


@dataclass(frozen=True, slots=True)
class GraphDeploymentTarget:
    namespace_id: str
    principal: Principal


@dataclass(frozen=True, slots=True)
class PublishedGraphVersion:
    graph_version_id: str
    number: int
    digests: GraphVersionDigests
    reused: bool


async def _read_admission(
    connection: AsyncConnection,
    dialect: Dialect,
    binds: PreparedGraphPublicationBinds,
    metadata: PreparedGraphPublicationMetadata,
) -> GraphVersionReadAdmission:
    if dialect is Dialect.SQLITE:
        measured = await observe_sqlite_graph_publication(connection, binds, metadata)
        return GraphVersionReadAdmission(
            measured.footprint.leaf_and_overflow_bytes,
            measured.resource_transport_bytes,
        )
    row = (
        await connection.execute(
            graph_publication_measurement_statement(dialect), binds.parameters()
        )
    ).one()
    ir_bytes, contract_bytes, resource_bytes, transport_bytes = (
        int(value) for value in row
    )
    texts = sum(len(value) for value in metadata.parameters().values())
    return GraphVersionReadAdmission(
        ir_bytes
        + contract_bytes
        + resource_bytes
        + texts
        + POSTGRESQL_FIXED_COLUMN_BYTES,
        transport_bytes,
    )


async def _graph_id(
    connection: AsyncConnection,
    dialect: Dialect,
    target: GraphDeploymentTarget,
    graph_name: str,
    identifiers: IdentifierGenerator,
    at: datetime,
) -> tuple[str, bool]:
    existing = await graph_id_by_name(connection, target.namespace_id, graph_name)
    if existing is not None:
        return existing, False
    graph_id = identifiers.new()
    await connection.execute(
        first_graph_insert_statement(dialect),
        {
            "id": graph_id,
            "namespace_id": target.namespace_id,
            "name": graph_name,
            "created_at": at,
            "updated_at": at,
        },
    )
    return graph_id, True


async def _publish(
    connection: AsyncConnection,
    dialect: Dialect,
    deployment: PreparedDeployment,
    target: GraphDeploymentTarget,
    identifiers: IdentifierGenerator,
    at: datetime,
) -> PublishedGraphVersion:
    metadata = prepare_graph_publication_metadata(
        dialect, target.principal, (), max_metadata_bytes=METADATA_BYTES
    )
    locks = TransitionLocks(connection)
    await locks.acquire_scopes((Scope(ScopeKind.NAMESPACE, (target.namespace_id,)),))
    digests = deployment.inputs.logical.digests
    graph_id, created = await _graph_id(
        connection, dialect, target, deployment.graph_name, identifiers, at
    )
    if not created:
        equal = await active_version_by_deployment_digest(
            connection, graph_id, digests.deployment_digest
        )
        if equal is not None:
            return PublishedGraphVersion(equal.version_id, equal.number, digests, True)
    number = await latest_version_number(connection, graph_id) + 1
    version_id = identifiers.new()
    attachment = attach_graph_deployment_references(
        deployment.inputs,
        GraphResourceScope(target.namespace_id, graph_id, version_id),
        (),
        limits=ATTACHMENT,
    )
    binds = prepare_graph_publication_binds(attachment, limits=BINDS)
    admission = await _read_admission(connection, dialect, binds, metadata)
    values = (
        binds.fixed_values(
            dialect, number=number, source_digest=deployment.source_digest, at=at
        )
        | binds.parameters()
        | metadata.parameters()
    )
    locator: object = await connection.scalar(
        graph_publication_insert_statement(dialect), values
    )
    saved: object = await connection.scalar(
        graph_publication_saved_statement(dialect), values | {SAVED_LOCATOR: locator}
    )
    if saved != 1:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    await connection.execute(
        graph_publication_admission_statement(dialect),
        binds.admission_values(admission),
    )
    return PublishedGraphVersion(version_id, number, digests, False)


async def publish_graph_deployment(
    database: Database,
    deployment: PreparedDeployment,
    target: GraphDeploymentTarget,
    *,
    identifiers: IdentifierGenerator,
    at: datetime,
) -> PublishedGraphVersion:
    async def unit(connection: AsyncConnection) -> PublishedGraphVersion:
        return await _publish(
            connection, database.dialect, deployment, target, identifiers, at
        )

    published = await database.run(unit)
    observed = current_diagnostics()
    if observed is not None:
        observed.logger(COMPONENT).emit(
            COMPLETED, outcome="reused" if published.reused else "published"
        )
    return published
