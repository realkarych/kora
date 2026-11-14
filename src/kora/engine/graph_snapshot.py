from dataclasses import dataclass
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Dialect
from kora.db.graph_read_admission import read_graph_version_admission
from kora.db.graph_resource_owner_reads import (
    AcquiredGraphResourceOwners,
    acquire_graph_resource_owners,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_projection import (
    SQLiteEncoding,
    graph_version_admitted_projection_statement,
)
from kora.db.graph_version_reads import AcquiredGraphVersion
from kora.engine.graph_execution_contract import ExecutionContractReadLimits
from kora.engine.graph_resource_references import (
    GraphResourceScope,
    NativeGraphResourceTransport,
)


@dataclass(frozen=True, slots=True)
class GraphSnapshotLimits:
    graph: ExecutionContractReadLimits
    native_bytes: int
    owner_text_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredGraphSnapshot:
    graph: AcquiredGraphVersion
    physical: NativeGraphResourceTransport
    owners: AcquiredGraphResourceOwners


async def acquire_graph_snapshot(
    connection: AsyncConnection,
    scope: GraphResourceScope,
    limits: GraphSnapshotLimits,
) -> AcquiredGraphSnapshot:
    admission = await read_graph_version_admission(
        connection,
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        max_db_input_bytes=limits.native_bytes,
        max_resource_transport_bytes=limits.graph.physical_input_bytes,
    )
    if admission is None:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    encoding: SQLiteEncoding = "utf-8"
    dialect = Dialect(connection.dialect.name)
    if dialect is Dialect.SQLITE:
        observed: object = await connection.scalar(text("PRAGMA encoding"))
        if observed not in ("UTF-8", "UTF-16le", "UTF-16be"):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        encoding = cast(SQLiteEncoding, str(observed).lower())
    with await connection.execute(
        graph_version_admitted_projection_statement(
            dialect,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=limits.graph.identity.max_encoded_bytes,
            resource_transport_bytes=admission.resource_transport_bytes,
            sqlite_encoding=encoding if dialect is Dialect.SQLITE else None,
        )
    ) as result:
        row = result.first()
    if row is None or len(row) != 6 or any(type(value) is not bytes for value in row):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    ir, ir_digest, contract, contract_digest, deployment_digest, resources = cast(
        tuple[bytes, bytes, bytes, bytes, bytes, bytes], tuple(row)
    )
    owners = await acquire_graph_resource_owners(
        connection, scope.version_id, max_text_bytes=limits.owner_text_bytes
    )
    return AcquiredGraphSnapshot(
        AcquiredGraphVersion(
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            encoding if dialect is Dialect.SQLITE else "ascii",
            (ir, contract),
            (ir_digest, contract_digest, deployment_digest),
        ),
        NativeGraphResourceTransport(resources, encoding),
        owners,
    )
