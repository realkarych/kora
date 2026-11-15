import json
from dataclasses import dataclass
from typing import cast

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceFailure,
    ParameterReferenceRejected,
)
from kora.engine.parameter_schema_source import (
    ParameterSchemaSourceFailure,
    ParameterSchemaSourceRejected,
    build_parameter_schema_arena,
)
from kora.engine.start_records import PreparedManualStartRequest
from kora.ir.json import JsonObject
from kora.ir.standard_parameter_preparation import (
    PreparedStandardParameters,
    StandardParameterInput,
    prepare_standard_parameters,
    validate_standard_parameters,
)
from kora.ir.standard_schema_policy import (
    SchemaDecision,
    SchemaDomain,
    SchemaRefused,
    SourceDecision,
    bind_policy,
    domain_limits,
)
from kora.ir.standard_schema_scan import SourceScan


def parameter_source_budgets(
    type_record_bytes: int,
) -> tuple[ParameterReferenceBudget, ParameterReferenceBudget]:
    if type(type_record_bytes) is not int or type_record_bytes <= 0:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    limits = domain_limits(SchemaDomain.PARAMETERS)
    source = limits["max_schema_source_bytes"]
    nodes = limits["max_schema_nodes"]
    work = 8 * (source + type_record_bytes + 1) ** 2
    return (
        ParameterReferenceBudget(work, 32 * source, 64 * nodes),
        ParameterReferenceBudget(work, 32 * source + type_record_bytes, 16 * source),
    )


def parameter_preparation_reservation(type_record_bytes: int) -> tuple[int, int]:
    source, selection = parameter_source_budgets(type_record_bytes)
    limits = domain_limits(SchemaDomain.PARAMETERS)
    retained = (
        source.scratch_bytes
        + source.scratch_entries
        + selection.scratch_bytes
        + selection.scratch_entries
        + limits["max_schema_source_bytes"]
    )
    work = 2 * source.work + selection.work + limits["max_validation_score"]
    return retained, work


@dataclass(frozen=True, slots=True, repr=False)
class ParameterPublicationBinding:
    profile_text: str

    def __init__(self, profile: object) -> None:
        if hasattr(self, "profile_text"):
            raise AttributeError("parameter publication binding is immutable")
        try:
            limits = bind_policy(SchemaDomain.PARAMETERS, profile)
        except SchemaRefused:
            raise GraphValueRejected(GraphValueFailure.VERSION) from None
        text = json.dumps(
            {"profile": SchemaDomain.PARAMETERS.value, "limits": dict(limits)},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "profile_text", text)

    def profile(self) -> JsonObject:
        return ObjectReader(self.profile_text).object()


def read_parameter_profile(value: object) -> JsonObject:
    try:
        bind_policy(SchemaDomain.PARAMETERS, value)
    except SchemaRefused:
        raise GraphValueRejected(GraphValueFailure.VERSION) from None
    return cast(JsonObject, value)


def prepare_graph_parameter_evaluation(
    parameters: JsonObject,
    type_contracts: list[JsonObject],
    profile: object,
    *,
    source_budget: ParameterReferenceBudget,
    selection_budget: ParameterReferenceBudget,
) -> PreparedStandardParameters | SourceDecision:
    try:
        limits = bind_policy(SchemaDomain.PARAMETERS, profile)
        SourceScan(limits).measure(parameters["schema"], complete_source=False)
        arena = build_parameter_schema_arena(
            parameters["schema"],
            type_contracts,
            budget=source_budget,
            max_container_depth=limits["max_schema_depth"],
            max_integer_bits=limits["max_schema_integer_bits"],
            selection_budget=selection_budget,
        )
        members = arena.nodes[arena.type_roots].members
        selection_budget.reserve(
            len(members) + len(type_contracts), entries=5 * len(members)
        )
        names = {cast(str, arena.nodes[key].value) for key, _ in members}
        del arena, members
        documents: list[JsonObject] = []
        for record in type_contracts:
            name = cast(str, record["name"])
            selection_budget.reserve(len(name))
            if name in names:
                selection_budget.reserve(1, entries=8)
                documents.append({"name": name, "schema": record["schema"]})
        return prepare_standard_parameters(
            StandardParameterInput(parameters, documents, profile)
        )
    except SchemaRefused as refused:
        return SourceDecision("refused", refused.reason)
    except ParameterReferenceRejected as refused:
        return SourceDecision(
            "refused",
            "evaluation_exhausted"
            if refused.reason is ParameterReferenceFailure.CAPACITY
            else "unsupported_schema",
        )
    except ParameterSchemaSourceRejected as refused:
        return SourceDecision(
            "refused",
            "evaluation_exhausted"
            if refused.reason is ParameterSchemaSourceFailure.CAPACITY
            else "unsupported_schema",
        )


def parameter_request_reservation(
    request: PreparedManualStartRequest, prepared: PreparedStandardParameters
) -> tuple[int, int]:
    if (
        type(request) is not PreparedManualStartRequest
        or type(prepared) is not PreparedStandardParameters
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    encoded = len(request.parameter_bytes())
    source = len(prepared.source_bytes)
    limits = domain_limits(SchemaDomain.PARAMETERS)
    retained = (
        16 * encoded
        + 16 * source
        + limits["max_variable_bytes"]
        + limits["max_auxiliary_entries"]
    )
    work = 8 * (encoded + source + 1) ** 2 + limits["max_validation_score"]
    return retained, work


def validate_graph_parameter_value(
    request: PreparedManualStartRequest,
    prepared: PreparedStandardParameters,
    *,
    max_retained_bytes: int,
    max_work: int,
) -> SchemaDecision:
    retained, work = parameter_request_reservation(request, prepared)
    if (
        type(max_retained_bytes) is not int
        or type(max_work) is not int
        or retained > max_retained_bytes
        or work > max_work
    ):
        return SchemaDecision("refused", "evaluation_exhausted")
    return validate_standard_parameters(request.parameters(), prepared)
