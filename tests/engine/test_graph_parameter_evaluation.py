import json
import sys
from dataclasses import dataclass
from types import FrameType
from typing import cast

import pytest

from kora.db.graph_value_json import GraphValueRejected, ObjectReader
from kora.engine.graph_parameter_evaluation import (
    ParameterPublicationBinding,
    parameter_preparation_reservation,
    parameter_request_reservation,
    parameter_source_budgets,
    prepare_graph_parameter_evaluation,
    read_parameter_profile,
    validate_graph_parameter_value,
)
from kora.engine.graph_parameters import build_graph_parameter_binding
from kora.engine.graph_types import build_graph_type_contracts
from kora.engine.parameter_schema_references import ParameterReferenceBudget
from kora.engine.start_records import (
    PreparedManualStartRequest,
    StartRecordRejected,
    prepare_manual_start_request,
)
from kora.ir import GraphDocument, render_ir_json
from kora.ir.json import JsonObject, JsonValue
from kora.ir.standard_parameter_preparation import PreparedStandardParameters
from kora.ir.standard_schema_policy import SchemaDomain, SourceDecision
from kora.ir.vcs_ref_json import canonical_bytes
from tests.engine.test_graph_types import custom, section_value
from tests.engine.test_start_records import GRAPH, NAMESPACE
from tests.ir.document_cases import document
from tests.ir.standard_schema_cases import binding


def budget() -> ParameterReferenceBudget:
    return ParameterReferenceBudget(100_000_000, 100_000_000, 10_000_000)


def parameter_inputs(
    schema: JsonObject | None, contracts: JsonObject | None = None
) -> tuple[JsonObject, list[JsonObject]]:
    raw = document()
    graph = cast(JsonObject, raw["graph"])
    if schema is not None:
        graph["params"] = schema
    raw["type_contracts"] = {} if contracts is None else contracts
    pinned = GraphDocument.model_validate(raw)
    ir = render_ir_json(pinned)
    parameters = ObjectReader(
        build_graph_parameter_binding(pinned, max_encoded_bytes=4_194_304)
    ).object()
    records = cast(
        list[JsonObject],
        section_value(build_graph_type_contracts(ir, max_encoded_bytes=4_194_304)),
    )
    return parameters, records


def prepare(schema: JsonObject | None) -> PreparedStandardParameters:
    parameters, records = parameter_inputs(schema)
    result = prepare_graph_parameter_evaluation(
        parameters,
        records,
        binding(SchemaDomain.PARAMETERS),
        source_budget=budget(),
        selection_budget=budget(),
    )
    assert isinstance(result, PreparedStandardParameters)
    return result


@pytest.mark.parametrize(("schema", "expected"), [(None, "invalid"), ({}, "valid")])
def test_parameter_composition_keeps_omission_distinct(
    schema: JsonObject | None, expected: str
) -> None:
    prepared = prepare(schema)
    request = prepare_manual_start_request(NAMESPACE, GRAPH, {"x": 0.3})
    original = request.parameter_bytes(), request.identity
    retained, work = parameter_request_reservation(request, prepared)

    result = validate_graph_parameter_value(
        request, prepared, max_retained_bytes=retained, max_work=work
    )

    assert result.outcome == expected
    assert (request.parameter_bytes(), request.identity) == original


@pytest.mark.parametrize("unrelated", [0, 20])
def test_parameter_composition_selects_only_the_exact_reference_closure(
    unrelated: int,
) -> None:
    schema: JsonObject = {
        "properties": {"x": {"$ref": "urn:kora:artifact:example:Selected"}},
        "$defs": {
            "inert": {"$recursiveRef": "urn:kora:artifact:example:Compatibility"}
        },
    }
    contracts: JsonObject = {
        "example/Selected": custom({"const": 0.3}, parent="example/Parent"),
        "example/Parent": custom({"not": {}}),
        "example/Compatibility": custom({"type": "string"}),
    }
    for number in range(unrelated):
        contracts[f"example/Unrelated{number}"] = custom({"pattern": "unused"})
    parameters, records = parameter_inputs(schema, contracts)
    selection = budget()

    result = prepare_graph_parameter_evaluation(
        parameters,
        records,
        binding(SchemaDomain.PARAMETERS),
        source_budget=budget(),
        selection_budget=selection,
    )

    assert isinstance(result, PreparedStandardParameters)
    saved = json.loads(result.source_bytes)
    assert [entry["name"] for entry in saved["documents"]] == [
        None,
        "example/Compatibility",
        "example/Selected",
    ]
    assert saved["constraints"] == [0]
    assert selection.work < budget().work


@dataclass
class DecodeObservation:
    calls: int = 0

    def observe(self, frame: FrameType, event: str, argument: object) -> None:
        if (
            event == "call"
            and frame.f_code is PreparedManualStartRequest.parameters.__code__
        ):
            self.calls += 1


@pytest.mark.parametrize("deficit", ["none", "retained", "work"])
def test_parameter_decode_requires_the_complete_outer_reservation(deficit: str) -> None:
    prepared = prepare({})
    request = prepare_manual_start_request(NAMESPACE, GRAPH, {"x": [0] * 300_000})
    retained, work = parameter_request_reservation(request, prepared)
    observation = DecodeObservation()
    previous = sys.getprofile()
    identity = request.identity

    sys.setprofile(observation.observe)
    try:
        result = validate_graph_parameter_value(
            request,
            prepared,
            max_retained_bytes=retained - int(deficit == "retained"),
            max_work=work - int(deficit == "work"),
        )
    finally:
        sys.setprofile(previous)

    assert len(request.parameter_bytes()) < 4_194_304
    assert result.outcome == "refused"
    assert result.reason == "evaluation_exhausted"
    assert observation.calls == int(deficit == "none")
    assert request.identity == identity


def test_parameter_source_capacity_is_not_schema_mismatch() -> None:
    parameters, records = parameter_inputs({"anyOf": [True, {"pattern": "private"}]})

    result = prepare_graph_parameter_evaluation(
        parameters,
        records,
        binding(SchemaDomain.PARAMETERS),
        source_budget=ParameterReferenceBudget(0, 0, 0),
        selection_budget=budget(),
    )

    assert isinstance(result, SourceDecision)
    assert result.outcome == "refused"
    assert result.reason == "evaluation_exhausted"


@pytest.mark.parametrize("named_count", [63, 64])
def test_parameter_document_capacity_includes_its_original_root(
    named_count: int,
) -> None:
    names = [f"example/Type{index}" for index in range(named_count)]
    contracts: JsonObject = {name: custom({}) for name in names}
    schema: JsonObject = {
        "allOf": [
            {"$ref": "urn:kora:artifact:" + name.replace("/", ":")} for name in names
        ]
    }
    parameters, records = parameter_inputs(schema, contracts)

    result = prepare_graph_parameter_evaluation(
        parameters,
        records,
        binding(SchemaDomain.PARAMETERS),
        source_budget=budget(),
        selection_budget=budget(),
    )

    if named_count == 63:
        assert isinstance(result, PreparedStandardParameters)
        assert result.accounting.schema_documents == 64
    else:
        assert result == SourceDecision("refused", "evaluation_exhausted")


def test_parameter_profile_freezes_the_explicit_binding() -> None:
    original = binding(SchemaDomain.PARAMETERS)
    selected = ParameterPublicationBinding(original)
    original["profile"] = "different"
    exported = selected.profile()
    exported["profile"] = "different"

    restored = read_parameter_profile(selected.profile())
    with pytest.raises(GraphValueRejected):
        ParameterPublicationBinding(binding(SchemaDomain.OUTPUT))

    assert restored == binding(SchemaDomain.PARAMETERS)
    assert tuple(restored) == ("profile", "limits")


def test_parameter_source_uses_its_reserved_arena_and_selection_budgets() -> None:
    parameters, records = parameter_inputs({})
    source, selection = parameter_source_budgets(4_194_304)
    retained, work = parameter_preparation_reservation(4_194_304)
    initial_work = source.work + selection.work

    result = prepare_graph_parameter_evaluation(
        parameters,
        records,
        binding(SchemaDomain.PARAMETERS),
        source_budget=source,
        selection_budget=selection,
    )

    assert isinstance(result, PreparedStandardParameters)
    assert source.work + selection.work < initial_work < work
    assert source.scratch_bytes + source.scratch_entries < retained
    assert selection.scratch_bytes + selection.scratch_entries < retained


@pytest.mark.parametrize("depth", [32, 33])
def test_parameter_input_keeps_root_zero_container_depth(depth: int) -> None:
    nested: JsonValue = None
    for _ in range(depth):
        nested = [nested]
    value = {"nested": nested}
    prepared = prepare({})
    if depth == 33:
        with pytest.raises(StartRecordRejected):
            prepare_manual_start_request(NAMESPACE, GRAPH, value)
        return
    request = prepare_manual_start_request(NAMESPACE, GRAPH, value)
    retained, work = parameter_request_reservation(request, prepared)

    result = validate_graph_parameter_value(
        request, prepared, max_retained_bytes=retained, max_work=work
    )

    assert result.outcome == "valid"


def test_original_four_mib_envelope_precedes_stricter_profile_admission() -> None:
    maximum = 4_194_304
    value: JsonObject = dict.fromkeys(("a", "b", "c"), "x" * 1_048_576)
    value["d"] = ""
    value["d"] = "y" * (maximum - len(canonical_bytes(value)))
    request = prepare_manual_start_request(NAMESPACE, GRAPH, value)
    prepared = prepare({})
    retained, work = parameter_request_reservation(request, prepared)
    original = request.parameter_bytes(), request.identity
    over = value.copy()
    over["d"] = cast(str, over["d"]) + "y"

    result = validate_graph_parameter_value(
        request, prepared, max_retained_bytes=retained, max_work=work
    )
    with pytest.raises(StartRecordRejected):
        prepare_manual_start_request(NAMESPACE, GRAPH, over)

    assert len(request.parameter_bytes()) == maximum
    assert result.outcome == "refused"
    assert result.reason == "evaluation_exhausted"
    assert (request.parameter_bytes(), request.identity) == original
