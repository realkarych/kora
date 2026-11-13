from kora.db.connection import Database
from kora.db.cost_reads import CostReadRefused, admit_projection
from kora.db.usage_context import acquire_usage_material, read_usage_binding
from kora.engine.attempt_usage import (
    UsageAccess,
    UsageAccessRejected,
    current_usage_target,
    require_usage_grant,
)
from kora.engine.graph_execution_contract import (
    ExecutionContractReadLimits,
    check_detached_execution_contract,
    execution_contract_read_reservation,
)
from kora.engine.graph_resource_references import (
    GraphResourceScope,
    NativeGraphResourceTransport,
)
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.usage_acceptance import UsageRejected
from kora.engine.usage_context import UsageContext, prepare_usage_context
from kora.protocols.authentication import Principal
from kora.protocols.time import Clock


async def read_usage_context(
    database: Database,
    attempt_id: str,
    dispatcher_id: str,
    epoch: int,
    principal: Principal,
    clock: Clock,
    limits: ExecutionContractReadLimits,
) -> UsageContext:
    if principal.kind != "service":
        raise UsageAccessRejected(UsageAccess.FORBIDDEN)
    async with database.read_snapshot() as connection:
        target = await current_usage_target(connection, None, attempt_id, principal)
        await require_usage_grant(
            connection,
            target,
            attempt_id,
            dispatcher_id,
            epoch,
            principal,
            clock.now(),
            lock_worker=False,
        )
        admit_projection(target)
        binding = await read_usage_binding(connection, attempt_id)
        if binding is None:
            raise UsageAccessRejected(UsageAccess.NOT_FOUND)
        execution_contract_read_reservation(limits)
        acquired, resources, owners = await acquire_usage_material(
            connection,
            binding,
            max_encoded_bytes=limits.identity.max_encoded_bytes,
            physical_input_bytes=limits.physical_input_bytes,
        )
    encoding = "utf-8" if acquired.encoding == "ascii" else acquired.encoding
    checked = check_detached_execution_contract(
        acquired,
        GraphVersionPin(
            binding.graph_id,
            binding.graph_version_id,
            binding.execution_contract_digest,
        ),
        GraphResourceScope(
            binding.namespace_id, binding.graph_id, binding.graph_version_id
        ),
        NativeGraphResourceTransport(resources, encoding),
        owners,
        limits=limits,
    )
    try:
        return prepare_usage_context(
            checked,
            binding.run_id,
            binding.node_path,
            max_ir_bytes=limits.identity.max_encoded_bytes,
        )
    except UsageRejected as error:
        raise CostReadRefused() from error
