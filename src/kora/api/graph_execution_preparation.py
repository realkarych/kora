from asyncio import CancelledError
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Never, Self

from kora.api.graph_cel import GraphCelChecks, GraphCelResult, verify_graph_cel
from kora.cel.limits import MAX_CODE_POINTS
from kora.cel_eval.errors import EvaluatorUnavailable, raise_safe
from kora.cel_eval.thread import FIXED_REPLY_BYTES, Evaluator
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
)
from kora.db.graph_values import DecodedGraphValues, read_graph_values
from kora.engine.graph_cel_facts import EXECUTION_SEMANTICS, GraphCelFacts
from kora.engine.graph_command_manifests import (
    CommandManifestLimits,
    CommandResourceContent,
    PreparedCommandStageGroup,
    build_declared_command_manifests,
    check_declared_command_manifests,
    prepare_graph_command_stage_groups,
)
from kora.engine.graph_execution_contract import (
    ExecutionContractReadLimits,
    check_execution_contract_members,
    check_execution_contract_projection_members,
    execution_contract_read_reservation,
    structured_output_preparation_reservation,
)
from kora.engine.graph_output_inventory import build_graph_output_inventory
from kora.engine.graph_output_values import saved_array
from kora.engine.graph_parameters import build_graph_parameter_binding
from kora.engine.graph_projection_paths import (
    admit_graph_projection_source,
    build_graph_cel_projection_paths,
)
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceReferences,
    GraphResourceScope,
    PreparedGraphResourceReferences,
    build_graph_resource_references,
)
from kora.engine.graph_retry_policies import build_graph_retry_policies
from kora.engine.graph_types import build_graph_type_contracts, read_document
from kora.engine.graph_version_identity import (
    HASH_CHUNK_BYTES,
    GraphVersionDigests,
    PreparedGraphVersionIdentity,
    graph_version_hash_bytes,
    prepare_graph_version_identity,
)
from kora.ir.diagnostic_collection import MAX_DIAGNOSTIC_BYTES
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from kora.protocols.storage_models import StorageIdentity

SOURCE_COPIES = 4
SECTION_COPIES = 6
NATIVE_TYPED_COPIES = 2
NATIVE_FACT_COPIES = 3
PREPARED_OWNER_SLOT = 1
PATTERN_COPIES = 4
MAX_PATTERN_PROBE = 10 * MAX_CODE_POINTS + len('"".matches("")')
DEPLOYMENT_COMPOSITION_OVERHEAD = 32


@dataclass(frozen=True, slots=True)
class ExecutionPreparationLimits:
    reader: ExecutionContractReadLimits
    rendering_characters: int
    pattern_work: int
    facts_bytes: int
    max_retained_bytes: int
    max_work: int


@dataclass(frozen=True, slots=True)
class GraphDeploymentPreparationLimits:
    execution: ExecutionPreparationLimits
    contents: CommandManifestLimits
    max_retained_bytes: int
    max_work: int


class PreparedLogicalExecutionContract:
    __slots__ = ("canonical_ir", "canonical_contract", "digests")
    canonical_ir: str
    canonical_contract: str
    digests: GraphVersionDigests

    def __init__(
        self,
        canonical_ir: str,
        canonical_contract: str,
        digests: GraphVersionDigests,
    ) -> None:
        if hasattr(self, "canonical_ir"):
            raise AttributeError("prepared execution contract is immutable")
        object.__setattr__(self, "canonical_ir", canonical_ir)
        object.__setattr__(self, "canonical_contract", canonical_contract)
        object.__setattr__(self, "digests", digests)

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("prepared execution contract is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("prepared execution contract is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("prepared execution contract requires explicit access")


class PreparedExecutionContract(PreparedLogicalExecutionContract):
    __slots__ = ("scope", "resources", "owners")
    scope: GraphResourceScope
    resources: GraphResourceReferences
    owners: tuple[GraphResourceOwner, ...]

    def __init__(
        self,
        scope: GraphResourceScope,
        canonical_ir: str,
        canonical_contract: str,
        digests: GraphVersionDigests,
        resources: GraphResourceReferences,
        owners: tuple[GraphResourceOwner, ...],
    ) -> None:
        super().__init__(canonical_ir, canonical_contract, digests)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "owners", owners)


class _ProtectedGraphDeploymentPreparation:
    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("graph deployment preparation is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("graph deployment preparation is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("graph deployment preparation requires explicit access")


class PreparedGraphDeploymentInputs(_ProtectedGraphDeploymentPreparation):
    __slots__ = ("logical", "groups")
    logical: PreparedLogicalExecutionContract
    groups: tuple[PreparedCommandStageGroup, ...]

    def __new__(
        cls,
        logical: PreparedLogicalExecutionContract,
        groups: tuple[PreparedCommandStageGroup, ...],
    ) -> Self:
        prepared = super().__new__(cls)
        object.__setattr__(prepared, "logical", logical)
        object.__setattr__(prepared, "groups", groups)
        return prepared


class GraphDeploymentCelRefusal(_ProtectedGraphDeploymentPreparation):
    __slots__ = ("result",)
    result: GraphCelResult

    def __new__(cls, result: GraphCelResult) -> Self:
        refusal = super().__new__(cls)
        object.__setattr__(refusal, "result", result)
        return refusal


def _admit_deployment(limits: GraphDeploymentPreparationLimits) -> None:
    if (
        type(limits) is not GraphDeploymentPreparationLimits
        or type(limits.execution) is not ExecutionPreparationLimits
        or type(limits.contents) is not CommandManifestLimits
        or any(
            type(value) is not int or value <= 0
            for value in (
                limits.max_retained_bytes,
                limits.max_work,
                limits.execution.max_retained_bytes,
                limits.execution.max_work,
                limits.contents.ir_bytes,
                limits.contents.scratch,
                limits.contents.work,
            )
        )
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    retained = (
        limits.execution.max_retained_bytes
        + limits.contents.ir_bytes
        + limits.contents.scratch
        + DEPLOYMENT_COMPOSITION_OVERHEAD
    )
    work = (
        limits.execution.max_work
        + limits.contents.work
        + DEPLOYMENT_COMPOSITION_OVERHEAD
    )
    if retained > limits.max_retained_bytes or work > limits.max_work:
        raise GraphValueRejected(GraphValueFailure.BUDGET)


def _admit(limits: ExecutionPreparationLimits) -> None:
    if (
        any(
            type(value) is not int or value <= 0
            for value in (
                limits.pattern_work,
                limits.facts_bytes,
                limits.max_retained_bytes,
                limits.max_work,
            )
        )
        or type(limits.rendering_characters) is not int
        or limits.rendering_characters < 0
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    reader = limits.reader
    read_retained, read_work = execution_contract_read_reservation(reader)
    projection = reader.projection
    cel = projection.cel
    logical = reader.resources.logical
    pair = reader.identity.max_encoded_bytes
    output_preparation, output_work = structured_output_preparation_reservation(
        cel.output_context_bytes
    )
    source_views = SOURCE_COPIES * (cel.ir_bytes + cel.output_context_bytes)
    sections = (
        cel.ir_bytes
        + 2 * cel.output_context_bytes
        + cel.parameter_context_bytes
        + projection.retry_bytes
        + limits.facts_bytes
        + projection.section_bytes
        + logical.scratch
    )
    native_requests = (
        4 * limits.rendering_characters + 4 * MAX_PATTERN_PROBE + FIXED_REPLY_BYTES
    )
    native_retained = (
        NATIVE_TYPED_COPIES * cel.typed_bytes
        + cel.typed_work
        + source_views
        + PATTERN_COPIES * limits.pattern_work
        + NATIVE_FACT_COPIES * limits.facts_bytes
        + native_requests
        + 2 * MAX_DIAGNOSTIC_BYTES
    )
    construction_copies = SECTION_COPIES * sections + 2 * HASH_CHUNK_BYTES
    retained = (
        2 * read_retained
        + logical.ir_bytes
        + logical.scratch
        + output_preparation
        + construction_copies
        + native_retained
        + PREPARED_OWNER_SLOT
    )
    scalar_work = 4 * (sections * sections + pair * pair)
    native_work = (
        cel.typed_work
        + source_views * source_views
        + native_retained
        + limits.pattern_work * (MAX_PATTERN_PROBE + FIXED_REPLY_BYTES)
    )
    work = (
        2 * read_work
        + logical.work
        + output_work
        + construction_copies
        + scalar_work
        + native_work
        + PREPARED_OWNER_SLOT
    )
    if retained > limits.max_retained_bytes or work > limits.max_work:
        raise GraphValueRejected(GraphValueFailure.BUDGET)


def _contract(
    ir_text: str, facts: GraphCelFacts, limits: ExecutionPreparationLimits
) -> JsonObject:
    reader = limits.reader
    projection = reader.projection
    cel = projection.cel
    types = saved_array(
        build_graph_type_contracts(ir_text, max_encoded_bytes=cel.output_context_bytes)
    )
    outputs = build_graph_output_inventory(
        ir_text, types, max_encoded_bytes=cel.output_context_bytes
    )
    if not isinstance(outputs, str):
        reason = (
            GraphValueFailure.BUDGET
            if outputs.reason == "preparation_exhausted"
            else GraphValueFailure.VERSION
        )
        raise GraphValueRejected(reason)
    parameters = build_graph_parameter_binding(
        read_document(ir_text, TextBudget(cel.ir_bytes)),
        max_encoded_bytes=cel.parameter_context_bytes,
    )
    retry = build_graph_retry_policies(
        ir_text, max_encoded_bytes=projection.retry_bytes
    )
    contract: JsonObject = {
        "semantics": EXECUTION_SEMANTICS,
        "type_contracts": types,
        "parameters": ObjectReader(parameters).object(),
        "outputs": saved_array(outputs),
        "retry_policies": ObjectReader(retry).object(),
        "cel_expressions": ObjectReader(facts.text).object(),
    }
    paths = build_graph_cel_projection_paths(ir_text, contract, limits=projection).text
    logical = build_declared_command_manifests(ir_text, limits=reader.resources.logical)
    contract["cel_projection_paths"] = ObjectReader(paths).object()
    contract["command_resources"] = ObjectReader(logical.text).object()
    return contract


def _checked_pair(
    ir_text: str, facts: GraphCelFacts, limits: ExecutionPreparationLimits
) -> tuple[PreparedGraphVersionIdentity, DecodedGraphValues]:
    reader = limits.reader
    prepared = prepare_graph_version_identity(
        ObjectReader(ir_text).object(),
        _contract(ir_text, facts, limits),
        limits=reader.identity,
    )
    values = prepared.values
    if values.ir != ir_text:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    saved = read_graph_values(
        values.value_encoding_version,
        values.ir,
        values.execution_contract,
        max_encoded_bytes=reader.identity.max_encoded_bytes,
    )
    if not isinstance(saved, DecodedGraphValues):
        raise GraphValueRejected(GraphValueFailure.VERSION)
    return prepared, saved


def _draft(
    ir_text: str,
    facts: GraphCelFacts,
    physical: PreparedGraphResourceReferences,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    limits: ExecutionPreparationLimits,
) -> PreparedExecutionContract:
    prepared, saved = _checked_pair(ir_text, facts, limits)
    values = prepared.values
    resources, _ = check_execution_contract_members(
        values.ir,
        saved.execution_contract,
        ObjectReader(physical.text).object(),
        scope,
        owners,
        limits.reader,
    )
    return PreparedExecutionContract(
        scope,
        values.ir,
        values.execution_contract,
        prepared.digests,
        resources,
        physical.owners,
    )


def _prepare_document(
    ir_text: str, limits: ExecutionPreparationLimits
) -> GraphDocument:
    _admit(limits)
    reader = limits.reader
    cel = reader.projection.cel
    if type(ir_text) is not str:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    source_bytes = min(
        cel.ir_bytes,
        reader.resources.logical.ir_bytes,
        reader.identity.max_encoded_bytes - 2,
    )
    if (
        len(ir_text) > source_bytes
        or graph_version_hash_bytes(len(ir_text), 2) > reader.identity.max_hash_bytes
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    admit_graph_projection_source(ir_text, reader.projection)
    return read_document(ir_text, TextBudget(source_bytes))


def _verification(
    document: GraphDocument, evaluator: Evaluator, limits: ExecutionPreparationLimits
) -> Awaitable[GraphCelResult]:
    cel = limits.reader.projection.cel
    return verify_graph_cel(
        document,
        evaluator,
        max_declarations=cel.declarations,
        max_triggers=cel.triggers,
        max_rendering_characters=limits.rendering_characters,
        max_pattern_work=limits.pattern_work,
        max_facts_bytes=limits.facts_bytes,
        max_typed_work=cel.typed_work,
        max_typed_bytes=cel.typed_bytes,
    )


def _complete_facts(result: GraphCelResult) -> GraphCelFacts | None:
    if not isinstance(result, GraphCelChecks) or not result.diagnostics.valid:
        return None
    if result.facts is None:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return result.facts


async def _prepare(
    ir_text: str,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    evaluator: Evaluator,
    limits: ExecutionPreparationLimits,
) -> PreparedExecutionContract | GraphCelResult:
    document = _prepare_document(ir_text, limits)
    reader = limits.reader
    physical = build_graph_resource_references(
        ir_text, scope, owners, limits=reader.resources
    )
    if len(physical.text) > reader.physical_input_bytes:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    verification = _verification(document, evaluator, limits)
    del document
    result = await verification
    facts = _complete_facts(result)
    if facts is None:
        return result
    return _draft(ir_text, facts, physical, scope, owners, limits)


async def _prepare_logical(
    ir_text: str, evaluator: Evaluator, limits: ExecutionPreparationLimits
) -> PreparedLogicalExecutionContract | GraphCelResult:
    result = await _verification(_prepare_document(ir_text, limits), evaluator, limits)
    facts = _complete_facts(result)
    if facts is None:
        return result
    prepared, saved = _checked_pair(ir_text, facts, limits)
    values = prepared.values
    reader = limits.reader
    check_execution_contract_projection_members(
        values.ir, saved.execution_contract, reader
    )
    check_declared_command_manifests(
        values.ir,
        saved.execution_contract["command_resources"],
        limits=reader.resources.logical,
    )
    return PreparedLogicalExecutionContract(
        values.ir, values.execution_contract, prepared.digests
    )


async def prepare_logical_execution_contract(
    ir_text: str,
    evaluator: Evaluator,
    *,
    limits: ExecutionPreparationLimits,
) -> PreparedLogicalExecutionContract | GraphCelResult:
    error: BaseException
    try:
        return await _prepare_logical(ir_text, evaluator, limits)
    except GraphValueRejected as rejected:
        error = GraphValueRejected(rejected.reason)
    except EvaluatorUnavailable as unavailable:
        error = EvaluatorUnavailable(unavailable.reason)
    except CancelledError:
        error = CancelledError()
    del ir_text, evaluator
    raise_safe(error)


async def prepare_graph_deployment_inputs(
    ir_text: str,
    contents: tuple[CommandResourceContent, ...],
    evaluator: Evaluator,
    *,
    storage_identity: StorageIdentity | None,
    limits: GraphDeploymentPreparationLimits,
) -> PreparedGraphDeploymentInputs | GraphDeploymentCelRefusal:
    groups: tuple[PreparedCommandStageGroup, ...] = ()
    error: BaseException
    try:
        _admit_deployment(limits)
        groups = prepare_graph_command_stage_groups(
            ir_text, contents, storage_identity=storage_identity, limits=limits.contents
        )
        contents = ()
        storage_identity = None
        result = await _prepare_logical(ir_text, evaluator, limits.execution)
        if isinstance(result, PreparedLogicalExecutionContract):
            return PreparedGraphDeploymentInputs(result, groups)
        return GraphDeploymentCelRefusal(result)
    except GraphValueRejected as rejected:
        error = GraphValueRejected(rejected.reason)
    except EvaluatorUnavailable as unavailable:
        error = EvaluatorUnavailable(unavailable.reason)
    except CancelledError:
        error = CancelledError()
    del ir_text, contents, storage_identity, evaluator, groups, limits
    raise_safe(error)


async def prepare_execution_contract(
    ir_text: str,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    evaluator: Evaluator,
    *,
    limits: ExecutionPreparationLimits,
) -> PreparedExecutionContract | GraphCelResult:
    error: BaseException
    try:
        return await _prepare(ir_text, scope, owners, evaluator, limits)
    except GraphValueRejected as rejected:
        error = GraphValueRejected(rejected.reason)
    except EvaluatorUnavailable as unavailable:
        error = EvaluatorUnavailable(unavailable.reason)
    except CancelledError:
        error = CancelledError()
    del ir_text, scope, owners, evaluator
    raise_safe(error)
