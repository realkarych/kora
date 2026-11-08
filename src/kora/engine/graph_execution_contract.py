from dataclasses import dataclass
from typing import Never, cast

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import AcquiredGraphVersion, transport_limit
from kora.db.types import DIGEST_LENGTH
from kora.engine.graph_projection_paths import (
    GraphProjectionReadLimits,
    read_graph_cel_projection_paths,
)
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceReferenceLimits,
    GraphResourceReferences,
    GraphResourceScope,
    check_graph_resource_references,
)
from kora.engine.graph_version_identity import (
    GraphVersionIdentityLimits,
    GraphVersionPin,
    graph_version_hash_bytes,
    read_graph_version_identity,
)
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterSchemaArena,
)
from kora.engine.parameter_schema_source import (
    ParameterSchemaSourceFailure,
    ParameterSchemaSourceRejected,
    build_parameter_schema_arena,
)
from kora.ir.json import JsonObject
from kora.ir.structured_schema_work import PARAMETERS as SCHEMA_PREPARATION_LIMITS

MEMBERS = (
    "semantics",
    "type_contracts",
    "parameters",
    "outputs",
    "retry_policies",
    "cel_expressions",
    "cel_projection_paths",
    "command_resources",
)
NATIVE_TRANSPORT_COPIES = 4
CANONICAL_PAIR_COPIES = 4
DECIMAL_READ_WRITE_PASSES = 2


@dataclass(frozen=True, slots=True)
class ExecutionContractReadLimits:
    identity: GraphVersionIdentityLimits
    projection: GraphProjectionReadLimits
    resources: GraphResourceReferenceLimits
    physical_input_bytes: int
    max_retained_bytes: int
    max_work: int


@dataclass(frozen=True, slots=True)
class ParameterSchemaReadLimits:
    work: int
    payload_bytes: int
    entries: int
    max_container_depth: int
    max_integer_bits: int
    selection_work: int
    selection_payload_bytes: int
    selection_entries: int


class CheckedExecutionContract:
    __slots__ = (
        "scope",
        "pin",
        "canonical_ir",
        "canonical_contract",
        "resources",
        "parameter_schema",
    )
    scope: GraphResourceScope
    pin: GraphVersionPin
    canonical_ir: str
    canonical_contract: str
    resources: GraphResourceReferences
    parameter_schema: ParameterSchemaArena | None

    def __init__(
        self,
        scope: GraphResourceScope,
        pin: GraphVersionPin,
        canonical_ir: str,
        canonical_contract: str,
        resources: GraphResourceReferences,
        parameter_schema: ParameterSchemaArena | None = None,
    ) -> None:
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "pin", pin)
        object.__setattr__(self, "canonical_ir", canonical_ir)
        object.__setattr__(self, "canonical_contract", canonical_contract)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "parameter_schema", parameter_schema)

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("checked execution contract is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("checked execution contract is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("checked execution contract requires explicit access")


def structured_output_preparation_reservation(output_bytes: int) -> tuple[int, int]:
    preparations = output_bytes // DIGEST_LENGTH
    source = (
        SCHEMA_PREPARATION_LIMITS["max_input_bytes"]
        + SCHEMA_PREPARATION_LIMITS["max_input_nodes"]
    )
    scratch = (
        source
        + SCHEMA_PREPARATION_LIMITS["max_auxiliary_bytes"]
        + SCHEMA_PREPARATION_LIMITS["max_auxiliary_entries"]
    )
    return scratch, preparations * (
        source + SCHEMA_PREPARATION_LIMITS["max_preparation_work"]
    )


def execution_contract_read_reservation(
    limits: ExecutionContractReadLimits,
) -> tuple[int, int]:
    resources = limits.resources
    logical = resources.logical
    if any(
        type(value) is not int or value <= 0
        for value in (
            limits.physical_input_bytes,
            limits.max_retained_bytes,
            limits.max_work,
            resources.source_bytes,
            resources.scratch,
            resources.work,
            logical.ir_bytes,
            logical.scratch,
            logical.work,
        )
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    pair = limits.identity.max_encoded_bytes
    native = transport_limit(pair, 2) + 6 * DIGEST_LENGTH
    pair_copies = NATIVE_TRANSPORT_COPIES * native + CANONICAL_PAIR_COPIES * pair
    projection = limits.projection
    cel = projection.cel
    preparation_scratch, preparation_work = structured_output_preparation_reservation(
        cel.output_context_bytes
    )
    retained = (
        pair_copies
        + limits.physical_input_bytes
        + projection.source_bytes
        + projection.scratch_bytes
        + cel.typed_bytes
        + projection.section_bytes
        + logical.ir_bytes
        + logical.scratch
        + resources.source_bytes
        + resources.scratch
        + preparation_scratch
    )
    max_token = max(
        pair,
        cel.output_context_bytes,
        cel.parameter_context_bytes,
        cel.comparison_bytes,
        projection.retry_bytes,
        projection.section_bytes,
    )
    decimal_work = DECIMAL_READ_WRITE_PASSES * (
        pair * pair + max_token * projection.source_bytes
    )
    work = (
        pair_copies
        + decimal_work
        + min(limits.identity.max_hash_bytes, graph_version_hash_bytes(pair, 0))
        + projection.source_bytes
        + projection.work
        + cel.typed_work
        + projection.section_bytes
        + logical.work
        + resources.work
        + limits.physical_input_bytes
        + preparation_work
    )
    if retained > limits.max_retained_bytes or work > limits.max_work:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    return retained, work


def check_execution_contract_projection_members(
    canonical_ir: str, saved: JsonObject, limits: ExecutionContractReadLimits
) -> None:
    if len(saved) != len(MEMBERS) or tuple(saved) != MEMBERS:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    read_graph_cel_projection_paths(canonical_ir, saved, limits=limits.projection)


def check_execution_contract_members(
    canonical_ir: str,
    saved: JsonObject,
    physical: object,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    limits: ExecutionContractReadLimits,
) -> GraphResourceReferences:
    check_execution_contract_projection_members(canonical_ir, saved, limits)
    return check_graph_resource_references(
        canonical_ir,
        saved["command_resources"],
        physical,
        scope,
        owners,
        limits=limits.resources,
    )


def _prepare_parameter_schema(
    saved: JsonObject, limits: ParameterSchemaReadLimits
) -> ParameterSchemaArena:
    if type(limits) is not ParameterSchemaReadLimits:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    parameters = cast(JsonObject, saved["parameters"])
    return build_parameter_schema_arena(
        parameters["schema"],
        saved["type_contracts"],
        budget=ParameterReferenceBudget(
            limits.work, limits.payload_bytes, limits.entries
        ),
        max_container_depth=limits.max_container_depth,
        max_integer_bits=limits.max_integer_bits,
        selection_budget=ParameterReferenceBudget(
            limits.selection_work,
            limits.selection_payload_bytes,
            limits.selection_entries,
        ),
    )


def _checked_result(
    acquired: AcquiredGraphVersion,
    pin: GraphVersionPin,
    scope: GraphResourceScope,
    physical: object,
    owners: tuple[GraphResourceOwner, ...],
    limits: ExecutionContractReadLimits,
    parameter_schema_limits: ParameterSchemaReadLimits | None,
) -> CheckedExecutionContract | GraphValueFailure:
    try:
        execution_contract_read_reservation(limits)
        if (
            type(acquired) is not AcquiredGraphVersion
            or type(scope) is not GraphResourceScope
            or type(pin) is not GraphVersionPin
            or acquired.namespace_id != scope.namespace_id
            or scope.graph_id != pin.graph_id
            or scope.version_id != pin.version_id
        ):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        identity = read_graph_version_identity(acquired, pin, limits=limits.identity)
        saved = identity.values.execution_contract
        resources = check_execution_contract_members(
            identity.canonical_ir,
            saved,
            physical,
            scope,
            owners,
            limits,
        )
        parameter_schema = (
            None
            if parameter_schema_limits is None
            else _prepare_parameter_schema(saved, parameter_schema_limits)
        )
        return CheckedExecutionContract(
            scope,
            pin,
            identity.canonical_ir,
            identity.canonical_contract,
            resources,
            parameter_schema,
        )
    except GraphValueRejected as rejected:
        rejected.__traceback__ = None
        return rejected.reason
    except ParameterSchemaSourceRejected as rejected:
        rejected.__traceback__ = None
        return (
            GraphValueFailure.BUDGET
            if rejected.reason is ParameterSchemaSourceFailure.CAPACITY
            else GraphValueFailure.INVALID
        )


def check_detached_execution_contract(
    acquired: AcquiredGraphVersion,
    pin: GraphVersionPin,
    scope: GraphResourceScope,
    physical: object,
    owners: tuple[GraphResourceOwner, ...],
    *,
    limits: ExecutionContractReadLimits,
    parameter_schema_limits: ParameterSchemaReadLimits | None = None,
) -> CheckedExecutionContract:
    result = _checked_result(
        acquired, pin, scope, physical, owners, limits, parameter_schema_limits
    )
    if isinstance(result, CheckedExecutionContract):
        return result
    del acquired, pin, scope, physical, owners, parameter_schema_limits
    raise_safe(GraphValueRejected(result))
