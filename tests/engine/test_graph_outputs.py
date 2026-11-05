import cProfile
import hashlib
import math
from copy import deepcopy
from typing import cast

import pytest

from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_outputs import (
    build_graph_output_contract,
    read_graph_output_contract,
)
from kora.engine.graph_type_references import root_reference_text
from kora.ir.approval_form import default_approval_form
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_schema_input import StructuredSchemaPreparationRefusal
from tests.engine.graph_output_cases import (
    LIMIT,
    built,
    child,
    input_bytes,
    output_arguments,
    output_document,
    output_preparation_capacity_document,
)
from tests.engine.graph_schema_cases import declaration


@pytest.mark.parametrize("config_present", [False, True])
@pytest.mark.parametrize("declaration_present", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_graph_output_agent_source_cases(
    config_present: bool, declaration_present: bool, nested: bool
) -> None:
    raw = output_document()
    node = child(raw, "graph", "nodes", "producer")
    config_schema: JsonObject = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
    }
    declaration_schema: JsonObject = {
        "properties": {"value": {"type": "integer"}},
        "type": "object",
    }
    if config_present:
        child(node, "config")["output_schema"] = config_schema
    if declaration_present:
        child(node, "outputs", "value")["schema"] = declaration_schema
    arguments = output_arguments(raw, nested)

    result = built(arguments)
    saved = ObjectReader(result.text).object()
    checked = read_graph_output_contract(
        *arguments, saved, result.digest, max_encoded_bytes=LIMIT
    )

    assert checked == result
    assert list(saved) == [
        "execution_semantics",
        "declared_type",
        "checker_binding",
        "type_contracts",
        "inline_schema",
        "checker",
        "constraints",
        "schema_validation",
    ]
    assert saved["execution_semantics"] == "kora.execution.v1"
    assert saved["checker_binding"] == "kora.structured-output-view.v1"
    assert result.digest == hashlib.sha256(result.text.encode("ascii")).hexdigest()
    assert len(result.digest) == 64
    roles: list[JsonValue] = [{"kind": "type_schema", "name": "StructuredOutput"}]
    if config_present or declaration_present:
        inline = cast(JsonObject, saved["inline_schema"])
        schema = config_schema if config_present else declaration_schema
        expected_path = (
            [*arguments[2][:-2], "config", "output_schema"]
            if config_present
            else [*arguments[2], "schema"]
        )
        assert list(inline) == ["origin", "source", "schema", "references"]
        assert inline["origin"] == "authored"
        assert inline["source"] == {
            "kind": "inline_schema",
            "path": expected_path,
            "location": node["source"],
        }
        assert list(cast(JsonObject, inline["schema"])) == list(schema)
        assert inline["schema"] == schema
        roles.append({"kind": "inline_schema"})
        assert saved["checker"] == {"kind": "inline_schema"}
    else:
        assert saved["inline_schema"] is None
        assert saved["checker"] == roles[0]
    assert saved["constraints"] == roles


@pytest.mark.parametrize("kind", ["agent", "human_input", "wait_event", "command"])
@pytest.mark.parametrize("nested", [False, True])
def test_graph_output_explicit_empty_schema_is_authored(
    kind: str, nested: bool
) -> None:
    raw = output_document(kind)
    node = child(raw, "graph", "nodes", "producer")
    if kind == "human_input":
        child(node, "config")["form"] = {}
    else:
        name = "event" if kind == "wait_event" else "value"
        child(node, "outputs", name)["schema"] = {}
    arguments = output_arguments(raw, nested)

    result = built(arguments)
    saved = ObjectReader(result.text).object()

    inline = cast(JsonObject, saved["inline_schema"])
    assert inline["origin"] == "authored"
    assert inline["schema"] == {}
    expected_path = (
        [*arguments[2][:-2], "config", "form"]
        if kind == "human_input"
        else [*arguments[2], "schema"]
    )
    assert child(inline, "source")["path"] == expected_path
    assert saved["checker"] == {"kind": "inline_schema"}


def test_graph_output_default_origin_changes_identity() -> None:
    implicit = output_document("human_input")
    explicit = deepcopy(implicit)
    child(explicit, "graph", "nodes", "producer", "config")["form"] = (
        default_approval_form()
    )

    first = built(output_arguments(implicit))
    second = built(output_arguments(explicit))
    first_value = ObjectReader(first.text).object()
    second_value = ObjectReader(second.text).object()

    first_inline = cast(JsonObject, first_value["inline_schema"])
    second_inline = cast(JsonObject, second_value["inline_schema"])
    assert first_inline["origin"] == "approval_form_default"
    assert second_inline["origin"] == "authored"
    assert first_inline["schema"] == second_inline["schema"] == default_approval_form()
    assert first_inline["source"] == second_inline["source"]
    assert first.digest != second.digest
    assert "form" not in child(implicit, "graph", "nodes", "producer", "config")


def test_graph_output_general_closure_roles_and_original_targets() -> None:
    raw = output_document()
    child(raw, "type_contracts").update(
        {
            "example/Base": declaration({"type": "object"}),
            "example/Child": declaration(
                {
                    "$defs": {
                        "unused": {
                            "$dynamicRef": "urn:kora:artifact:example:Dependency"
                        }
                    },
                    "$recursiveRef": "urn:kora:artifact:example:Inert",
                },
                "example/Base",
            ),
            "example/Dependency": declaration({"type": "object"}),
            "example/Inert": declaration({"pattern": "unsupported-but-inert"}),
            "example/Unrelated": declaration({"pattern": "unrelated"}),
        }
    )
    child(raw, "graph", "nodes", "producer", "outputs", "value").update(
        {
            "type": "example/Child",
            "schema": {
                "$defs": {"a/b~c": {"allOf": [{"type": "object"}]}},
                "$ref": "#/$defs/a~1b~0c/allOf/0",
            },
        }
    )
    arguments = output_arguments(raw)

    result = built(arguments)
    saved = ObjectReader(result.text).object()

    records = cast(list[JsonObject], saved["type_contracts"])
    assert [record["name"] for record in records] == [
        "StructuredOutput",
        "example/Base",
        "example/Child",
        "example/Dependency",
        "example/Inert",
    ]
    assert saved["checker"] == {"kind": "type_schema", "name": "example/Child"}
    assert saved["constraints"] == [
        {"kind": "type_schema", "name": "StructuredOutput"},
        {"kind": "type_schema", "name": "example/Base"},
        {"kind": "type_schema", "name": "example/Child"},
        {"kind": "inline_schema"},
    ]
    inline = cast(JsonObject, saved["inline_schema"])
    assert inline["references"] == [
        {
            "path": ["$ref"],
            "target_type": None,
            "target_path": ["$defs", "a/b~c", "allOf", 0],
        }
    ]
    assert child(inline, "schema")["$ref"] == "#/$defs/a~1b~0c/allOf/0"
    assert (
        cast(JsonObject, records[2]["schema"])["$recursiveRef"]
        == "urn:kora:artifact:example:Inert"
    )


@pytest.mark.parametrize(
    "mode",
    [
        "agent_mismatch",
        "human_mismatch",
        "agent_duplicate",
        "command_missing_file",
        "command_extra",
        "unknown_type",
        "list",
        "unrelated",
        "invalid_reference",
    ],
)
def test_graph_output_refuses_inconsistent_source(mode: str) -> None:
    kind = (
        "human_input"
        if mode == "human_mismatch"
        else "command"
        if mode.startswith("command")
        else "agent"
    )
    raw = output_document(kind)
    original = output_arguments(raw)
    node = child(raw, "graph", "nodes", "producer")
    if mode == "agent_mismatch":
        child(node, "config")["output_schema"] = {"type": "integer"}
        child(node, "outputs", "value")["schema"] = {"type": "string"}
    elif mode == "human_mismatch":
        child(node, "outputs", "response")["schema"] = {}
    elif mode == "agent_duplicate":
        child(node, "outputs")["second"] = {"type": "StructuredOutput"}
    elif mode == "command_missing_file":
        del child(node, "config")["output_file"]
    elif mode == "command_extra":
        child(node, "outputs")["second"] = {"type": "StructuredOutput"}
    elif mode == "invalid_reference":
        child(node, "outputs", "value")["schema"] = {
            "$ref": "https://private.invalid/schema"
        }
    else:
        child(node, "outputs", "value")["type"] = {
            "unknown_type": "example/Missing",
            "list": "List[StructuredOutput]",
            "unrelated": "AgentSession",
        }[mode]
        if mode == "unrelated":
            del child(node, "outputs")["session"]
            child(node, "outputs")["session"] = child(node, "outputs").pop("value")
    path = [*original[2][:-1], "session"] if mode == "unrelated" else original[2]
    arguments = (render_object(raw, TextBudget(LIMIT)), original[1], path)

    with pytest.raises(GraphValueRejected) as failure:
        build_graph_output_contract(*arguments, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID
    assert str(failure.value) == "invalid graph value representation"


@pytest.mark.parametrize(
    "path",
    [
        [],
        ["graph"],
        ["graph", "nodes", "missing", "outputs", "value"],
        ["graph", "nodes", "producer", "inputs", "value"],
        ["graph", "nodes", "producer", "outputs", 0],
        ["graph", "subgraphs", "absent", "nodes", "producer", "outputs", "value"],
    ],
)
def test_graph_output_refuses_wrong_selection(path: list[JsonValue]) -> None:
    ir, types, _ = output_arguments(output_document())

    with pytest.raises(GraphValueRejected) as failure:
        build_graph_output_contract(ir, types, path, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "origin",
        "schema",
        "references",
        "checker",
        "constraints",
        "policy",
        "type",
        "order",
        "missing",
        "extra",
    ],
)
def test_graph_output_reader_rejects_rehashed_substitution(field: str) -> None:
    raw = output_document()
    child(raw, "graph", "nodes", "producer", "config")["output_schema"] = {}
    arguments = output_arguments(raw)
    result = built(arguments)
    saved = ObjectReader(result.text).object()
    inline = cast(JsonObject, saved["inline_schema"])
    if field == "source":
        child(inline, "source")["path"] = [
            "graph",
            "nodes",
            "producer",
            "outputs",
            "value",
            "schema",
        ]
    elif field == "origin":
        inline["origin"] = "approval_form_default"
    elif field == "schema":
        inline["schema"] = {"type": "string"}
    elif field == "references":
        inline["references"] = [
            {"path": ["$ref"], "target_type": None, "target_path": []}
        ]
    elif field == "checker":
        saved["checker"] = {"kind": "type_schema", "name": "StructuredOutput"}
    elif field == "constraints":
        saved["constraints"] = []
    elif field == "policy":
        child(saved, "schema_validation", "parameters")["max_input_bytes"] = 1
    elif field == "type":
        cast(JsonObject, cast(list[JsonValue], saved["type_contracts"])[0])[
            "schema"
        ] = {"type": "string"}
    elif field == "order":
        saved["execution_semantics"] = saved.pop("execution_semantics")
    elif field == "missing":
        del saved["schema_validation"]
    else:
        saved["extra"] = None
    changed = render_object(saved, TextBudget(LIMIT))
    digest = hashlib.sha256(changed.encode("ascii")).hexdigest()

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_output_contract(*arguments, saved, digest, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID
    assert saved != ObjectReader(result.text).object() or field == "order"


@pytest.mark.parametrize("digest", ["", "a" * 64, "A" * 64, "sha256:" + "a" * 64])
def test_graph_output_reader_rejects_digest(digest: str) -> None:
    arguments = output_arguments(output_document())
    result = built(arguments)
    saved = ObjectReader(result.text).object()

    with pytest.raises(GraphValueRejected) as failure:
        read_graph_output_contract(*arguments, saved, digest, max_encoded_bytes=LIMIT)

    assert failure.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize(
    "schema", [{"pattern": "a"}, {"$ref": "#"}, {"description": "\ud800"}]
)
def test_graph_output_retains_actual_preparation_refusal(schema: JsonObject) -> None:
    raw = output_document()
    child(raw, "graph", "nodes", "producer", "config")["output_schema"] = schema
    arguments = output_arguments(raw)

    result = build_graph_output_contract(*arguments, max_encoded_bytes=LIMIT)

    assert isinstance(result, StructuredSchemaPreparationRefusal)
    assert result.reason == "unsupported_schema"


def test_graph_output_exact_aggregate_admission_and_detachment() -> None:
    raw = output_document()
    child(raw, "graph", "nodes", "producer", "config")["output_schema"] = {
        "examples": [1, 1.0, -0.0, "\0", 10**1000],
        "description": "\ud83d\ude00",
    }
    arguments = output_arguments(raw)
    expected = built(arguments)
    exact = input_bytes(arguments) + len(expected.text)
    saved = ObjectReader(expected.text).object()
    reader_profile = cProfile.Profile()

    actual = built(arguments, exact)
    checked = read_graph_output_contract(
        *arguments, saved, expected.digest, max_encoded_bytes=exact
    )
    with pytest.raises(GraphValueRejected) as too_small:
        build_graph_output_contract(*arguments, max_encoded_bytes=exact - 1)
    with pytest.raises(GraphValueRejected) as read_small:
        reader_profile.runcall(
            read_graph_output_contract,
            *arguments,
            saved,
            expected.digest,
            max_encoded_bytes=exact - 1,
        )
    cast(JsonObject, arguments[1][0])["name"] = "mutated"
    saved.clear()
    child(raw, "graph", "nodes", "producer", "config").clear()

    assert actual == checked == expected
    assert too_small.value.reason is read_small.value.reason is GraphValueFailure.BUDGET
    entered = {
        entry.code.co_name
        for entry in reader_profile.getstats()
        if not isinstance(entry.code, str)
    }
    assert entered.isdisjoint(
        {"derive_output", "read_document", "prepare_graph_schema"}
    )
    schema = child(ObjectReader(actual.text).object(), "inline_schema", "schema")
    values = cast(list[JsonValue], schema["examples"])
    assert type(values[0]) is int
    assert type(values[1]) is float
    assert math.copysign(1, cast(float, values[2])) == -1
    assert values[3:] == ["\0", 10**1000]
    assert schema["description"] == "\U0001f600"


def test_graph_output_input_refuses_before_dependent_work() -> None:
    arguments = output_arguments(output_document())
    profile = cProfile.Profile()

    with pytest.raises(GraphValueRejected) as failure:
        profile.runcall(
            build_graph_output_contract,
            *arguments,
            max_encoded_bytes=input_bytes(arguments) - 1,
        )

    names = {
        entry.code.co_name
        for entry in profile.getstats()
        if not isinstance(entry.code, str)
    }
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert names.isdisjoint(
        {
            "object",
            "read_document",
            "derive_output",
            "graph_output_source",
            "prepare_graph_schema",
        }
    )


def test_graph_output_generated_reference_refuses_before_path_materialization() -> None:
    depth = 128
    root: JsonObject = {}
    current = root
    for _ in range(depth):
        nested: JsonObject = {}
        current["not"] = nested
        current = nested
    current["$ref"] = "#"
    profile = cProfile.Profile()
    reserved_wrapper = len('{"path":,"target_type":,"target_path":}') + 2

    with pytest.raises(GraphValueRejected) as failure:
        profile.runcall(
            root_reference_text,
            root,
            None,
            {},
            {},
            TextBudget(reserved_wrapper + 2 * depth),
        )

    path_entries = [
        entry
        for entry in profile.getstats()
        if not isinstance(entry.code, str) and entry.code.co_name == "path_text"
    ]
    assert failure.value.reason is GraphValueFailure.BUDGET
    assert len(path_entries) == 1
    assert not any(
        isinstance(call.code, str) and ("append" in call.code or "reverse" in call.code)
        for call in path_entries[0].calls or []
    )


def test_graph_output_retains_actual_preparation_capacity() -> None:
    arguments = output_arguments(output_preparation_capacity_document())

    result = build_graph_output_contract(*arguments, max_encoded_bytes=LIMIT)

    assert isinstance(result, StructuredSchemaPreparationRefusal)
    assert result.reason == "preparation_exhausted"


@pytest.mark.parametrize("kind", ["wait_event", "command"])
def test_graph_output_direct_schema_omission(kind: str) -> None:
    arguments = output_arguments(output_document(kind))

    result = built(arguments)
    saved = ObjectReader(result.text).object()

    assert saved["inline_schema"] is None
    assert saved["checker"] == {"kind": "type_schema", "name": "StructuredOutput"}
    assert saved["constraints"] == [{"kind": "type_schema", "name": "StructuredOutput"}]


def test_graph_output_inline_remote_closure_keeps_distinct_equal_roots() -> None:
    raw = output_document()
    child(raw, "type_contracts").update(
        {"example/Left": declaration({}), "example/Right": declaration({})}
    )
    child(raw, "graph", "nodes", "producer", "outputs", "value")["schema"] = {
        "allOf": [
            {"$ref": "urn:kora:artifact:example:Right"},
            {"$ref": "urn:kora:artifact:example:Left"},
        ]
    }
    arguments = output_arguments(raw)

    result = built(arguments)
    saved = ObjectReader(result.text).object()

    records = cast(list[JsonObject], saved["type_contracts"])
    assert [record["name"] for record in records] == [
        "StructuredOutput",
        "example/Left",
        "example/Right",
    ]
    assert records[1]["schema"] == records[2]["schema"]
    assert records[1]["source"] != records[2]["source"]
    assert child(saved, "inline_schema")["references"] == [
        {
            "path": ["allOf", 0, "$ref"],
            "target_type": "example/Right",
            "target_path": [],
        },
        {
            "path": ["allOf", 1, "$ref"],
            "target_type": "example/Left",
            "target_path": [],
        },
    ]
    assert saved["constraints"] == [
        {"kind": "type_schema", "name": "StructuredOutput"},
        {"kind": "inline_schema"},
    ]
