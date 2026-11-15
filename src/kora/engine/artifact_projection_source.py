from dataclasses import dataclass, field
from typing import cast

from kora.cel_eval.artifact_projection_codec import (
    ProjectionAdmission,
    ProjectionFailure,
    ProjectionLimits,
    ProjectionRejected,
)
from kora.cel_eval.artifact_projection_view import (
    STRUCTURED_OUTPUT_VIEW,
    ProjectionView,
    prepare_projection_view,
)
from kora.db.graph_value_json import ObjectReader
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    structured_output_preparation_reservation,
)
from kora.engine.graph_output_sources import output_path
from kora.engine.graph_schema_preparation import select_records
from kora.ir.json import JsonObject, JsonValue
from kora.ir.standard_output_preparation import StandardOutputInput
from kora.ir.standard_schema_policy import SchemaDomain


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionBinding:
    namespace_id: str
    graph_id: str
    version_id: str
    execution_contract_digest: str
    output_path: tuple[str, ...]
    contract_digest: str
    path_set_digest: str


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionSource:
    binding: ProjectionBinding
    schema: StandardOutputInput = field(repr=False)
    view: ProjectionView = field(repr=False)
    admission: ProjectionAdmission = field(repr=False)


def source_reservation(source_bytes: int, *, validating: bool) -> tuple[int, int, int]:
    scratch, work = structured_output_preparation_reservation(64)
    return (
        256 * source_bytes + (scratch if validating else 0),
        64 * source_bytes + (scratch if validating else 0),
        64 * source_bytes * source_bytes + (work if validating else 0),
    )


def selected_entry(entries: JsonValue, path: tuple[str, ...]) -> JsonObject:
    for item in cast(list[JsonValue], entries):
        entry = cast(JsonObject, item)
        if tuple(cast(list[str], entry["path"])) == path:
            return entry
    raise ProjectionRejected(ProjectionFailure.BINDING)


def output_schema(contract: JsonObject) -> StandardOutputInput:
    policy = cast(JsonObject | None, contract.get("schema_validation"))
    if (
        policy is None
        or policy.get("policy") != SchemaDomain.OUTPUT.value
        or contract.get("checker_binding") != STRUCTURED_OUTPUT_VIEW
    ):
        raise ProjectionRejected(ProjectionFailure.UNAVAILABLE)
    inline = contract["inline_schema"]
    records = cast(list[JsonObject], contract["type_contracts"])
    return StandardOutputInput(
        declared_type=contract["declared_type"],
        type_contracts=select_records(
            records,
            contract["declared_type"],
            None
            if inline is None
            else cast(JsonObject | bool, cast(JsonObject, inline)["schema"]),
        ),
        inline_schema=None
        if inline is None
        else {
            "source": cast(JsonObject, inline)["source"],
            "schema": cast(JsonObject, inline)["schema"],
        },
        execution_semantics=contract["execution_semantics"],
        checker_binding=contract["checker_binding"],
        schema_validation=policy,
    )


def projection_source(
    checked: CheckedExecutionContract,
    path: tuple[str, ...],
    limits: ProjectionLimits,
    *,
    validating: bool,
) -> ProjectionSource:
    if (
        type(checked) is not CheckedExecutionContract
        or type(limits) is not ProjectionLimits
    ):
        raise ProjectionRejected(ProjectionFailure.BINDING)
    if (
        type(path) is not tuple
        or len(path) not in (5, 7)
        or any(type(segment) is not str for segment in path)
    ):
        raise ProjectionRejected(ProjectionFailure.BINDING)
    source_bytes = len(checked.canonical_contract) + len(checked.canonical_ir)
    if source_bytes + sum(len(segment) for segment in path) > limits.source_bytes:
        raise ProjectionRejected(ProjectionFailure.CAPACITY)
    output_path(list(path))
    admission = ProjectionAdmission(limits.variable_bytes, limits.entries, limits.work)
    admission.reserve(*source_reservation(source_bytes, validating=validating))
    saved = ObjectReader(checked.canonical_contract).object()
    output = selected_entry(saved["outputs"], path)
    contract = cast(JsonObject, output["contract"])
    schema = output_schema(contract)
    path_section = cast(JsonObject, saved["cel_projection_paths"])
    path_entry = selected_entry(path_section["outputs"], path)
    selected = cast(JsonObject, path_entry["paths"])
    view = prepare_projection_view(contract, selected, admission)
    scope = checked.scope
    binding = ProjectionBinding(
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        checked.pin.execution_contract_digest,
        path,
        cast(str, output["contract_digest"]),
        view.path_set_digest,
    )
    return ProjectionSource(binding, schema, view, admission)
