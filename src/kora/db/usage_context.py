from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Dialect
from kora.db.cost_reads import admit_projection, required_text, scalar_projection
from kora.db.graph_resource_owner_reads import (
    AcquiredGraphResourceOwners,
    acquire_graph_resource_owners,
)
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import AcquiredGraphVersion, acquire_graph_version
from kora.db.tables import (
    attempt,
    graph_version,
    graph_version_resource_owner,
    node_instance,
    run,
)


@dataclass(frozen=True, repr=False)
class UsageBinding:
    namespace_id: str
    run_id: str
    graph_id: str
    graph_version_id: str
    execution_contract_digest: str
    node_path: str


async def read_usage_binding(
    connection: AsyncConnection, attempt_id: str
) -> UsageBinding | None:
    columns = (
        run.c.namespace_id,
        run.c.id.label("run_id"),
        run.c.graph_id,
        graph_version.c.id.label("graph_version_id"),
        graph_version.c.execution_contract_digest,
        node_instance.c.node_path,
    )
    result = await connection.execute(
        select(
            *(
                item
                for column in columns
                for item in scalar_projection(connection, column)
            ),
        )
        .select_from(attempt.join(node_instance).join(run).join(graph_version))
        .where(attempt.c.id == attempt_id, node_instance.c.top_level.is_(True))
    )
    try:
        row = result.mappings().one_or_none()
    finally:
        result.close()
    if row is None:
        return None
    admit_projection(row)
    return UsageBinding(*(required_text(row[str(column.key)]) for column in columns))


async def acquire_usage_material(
    connection: AsyncConnection,
    binding: UsageBinding,
    *,
    max_encoded_bytes: int,
    physical_input_bytes: int,
) -> tuple[AcquiredGraphVersion, bytes, AcquiredGraphResourceOwners]:
    acquired = await acquire_graph_version(
        connection,
        binding.namespace_id,
        binding.graph_id,
        binding.graph_version_id,
        max_encoded_bytes=max_encoded_bytes,
    )
    if acquired is None:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    native = graph_resource_binary_projection(
        Dialect(connection.dialect.name), graph_version.c.resources
    )
    resources: object = await connection.scalar(
        select(case((func.length(native) <= physical_input_bytes, native))).where(
            graph_version.c.id == binding.graph_version_id
        )
    )
    if type(resources) is not bytes:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    remaining = physical_input_bytes - len(resources)
    if remaining == 0:
        present = await connection.scalar(
            select(graph_version_resource_owner.c.ordinal)
            .where(
                graph_version_resource_owner.c.graph_version_id
                == binding.graph_version_id
            )
            .limit(1)
        )
        if present is not None:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        owners = AcquiredGraphResourceOwners(binding.graph_version_id, ())
    else:
        owners = await acquire_graph_resource_owners(
            connection, binding.graph_version_id, max_text_bytes=remaining
        )
    return acquired, resources, owners
