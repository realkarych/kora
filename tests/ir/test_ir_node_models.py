from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

import kora.ir as ir
from tests.ir.document_cases import NODE_CASES, document


@pytest.mark.parametrize("kind", list(NODE_CASES))
def test_ir_node_kind_structure_is_closed(kind: str) -> None:
    adapter = TypeAdapter(ir.Node)
    value = {"kind": kind, **NODE_CASES[kind]}
    invalids = [
        {**value, "config": False},
        {**value, "unexpected": False},
        {key: item for key, item in value.items() if key != "config"},
    ]

    parsed = adapter.validate_python(value)
    for invalid in invalids:
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)

    assert ir.export_ir(parsed) == value


@pytest.mark.parametrize(
    ("kind", "role", "type_name"),
    [
        ("agent", "session", "AgentSession"),
        ("command", "result", "CommandResult"),
        ("human_input", "response", "StructuredOutput"),
        ("wait_event", "event", "StructuredOutput"),
    ],
)
def test_ir_role_outputs_are_fixed_in_nested_bodies(
    kind: str, role: str, type_name: str
) -> None:
    value = document()
    graph = cast("dict[str, object]", value["graph"])
    node = {"kind": kind, **NODE_CASES[kind]}
    graph["subgraphs"] = {"body": {"nodes": {"node": node}}}
    invalid_outputs = [
        {},
        {"alias": {"type": type_name}},
        {role: {"type": "Other"}},
        {role: {"type": type_name}, "alias": {"type": type_name}},
    ]

    parsed = ir.GraphDocument.model_validate(value)
    for outputs in invalid_outputs:
        node["outputs"] = outputs
        with pytest.raises(ValidationError):
            ir.GraphDocument.model_validate(value)

    assert ir.export_ir(parsed)["graph"] is not None


@pytest.mark.parametrize("kind", ["human_input", "wait_event"])
def test_human_and_wait_have_only_their_role(kind: str) -> None:
    node = {"kind": kind, **NODE_CASES[kind]}
    outputs = cast("dict[str, object]", node["outputs"])
    node["outputs"] = {**outputs, "extra": {"type": "Other"}}

    with pytest.raises(ValidationError):
        TypeAdapter(ir.Node).validate_python(node)


@pytest.mark.parametrize("kind", ["timer", "branch"])
def test_timer_and_branch_preserve_declared_outputs(kind: str) -> None:
    value = {
        "kind": kind,
        **NODE_CASES[kind],
        "outputs": {"authored": {"type": "Text"}},
    }

    parsed = TypeAdapter(ir.Node).validate_python(value)

    assert ir.export_ir(parsed) == value


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_fixed_role_schema_keeps_declaration_and_extra_output_rules(mode: str) -> None:
    from typing import Literal

    mode_value = cast("Literal['validation', 'serialization']", mode)
    human = ir.HumanInputNode.model_json_schema(mode=mode_value)
    agent = ir.AgentNode.model_json_schema(mode=mode_value)

    human_outputs = cast("dict[str, dict[str, object]]", human["properties"])["outputs"]
    agent_outputs = cast("dict[str, dict[str, object]]", agent["properties"])["outputs"]

    assert human_outputs["additionalProperties"] is False
    properties = cast("dict[str, dict[str, object]]", human_outputs["properties"])
    assert properties["response"]["allOf"] == [
        {"$ref": "#/$defs/OutputDecl"},
        {"properties": {"type": {"const": "StructuredOutput"}}},
    ]
    assert agent_outputs["additionalProperties"] == {
        "allOf": [
            {"$ref": "#/$defs/OutputDecl"},
            {"properties": {"type": {"not": {"const": "AgentSession"}}}},
        ]
    }


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_node_union_is_discriminated_in_both_schema_modes(mode: str) -> None:
    from typing import Literal

    schema = TypeAdapter(ir.Node).json_schema(
        mode=cast("Literal['validation', 'serialization']", mode)
    )

    discriminator = cast("dict[str, object]", schema["discriminator"])

    assert discriminator["propertyName"] == "kind"
    assert set(cast("dict[str, object]", discriminator["mapping"])) == set(NODE_CASES)
    assert len(cast("list[object]", schema["oneOf"])) == 8


@pytest.mark.parametrize("kind", list(NODE_CASES))
def test_node_refuses_another_kind_configuration(kind: str) -> None:
    kinds = list(NODE_CASES)
    wrong = NODE_CASES[kinds[(kinds.index(kind) + 1) % len(kinds)]]["config"]
    value = {"kind": kind, **NODE_CASES[kind], "config": wrong}

    with pytest.raises(ValidationError):
        TypeAdapter(ir.Node).validate_python(value)


@pytest.mark.parametrize("name", ["x-meta", "node\n", "Node", ""])
def test_named_node_maps_refuse_invalid_keys(name: str) -> None:
    value = document()
    graph = cast("dict[str, object]", value["graph"])
    graph["nodes"] = {name: {"kind": "timer", "config": {"duration": "0s"}}}

    with pytest.raises(ValidationError):
        ir.GraphDocument.model_validate(value)


@pytest.mark.parametrize(
    "field", ["subgraphs", "id", "namespace", "params", "triggers", "target", "overlap"]
)
def test_subgraph_has_only_shared_body_fields(field: str) -> None:
    value = {
        "nodes": {"timer": {"kind": "timer", "config": {"duration": "0s"}}},
        field: {},
    }

    with pytest.raises(ValidationError):
        ir.Subgraph.model_validate(value)


def test_node_dependencies_are_unique_but_input_independence_preserves_order() -> None:
    value = {
        "kind": "timer",
        "config": {"duration": "0s"},
        "independent_of": ["a", "a"],
        "depends_on": ["b", "a"],
    }

    parsed = ir.TimerNode.model_validate(value)
    with pytest.raises(ValidationError):
        ir.TimerNode.model_validate({**value, "depends_on": ["a", "a"]})

    assert ir.export_ir(parsed) == value
