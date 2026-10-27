from decimal import Decimal
from typing import cast

import pytest
from pydantic import BaseModel, Field, PrivateAttr, StrictStr, ValidationError
from pydantic_core import PydanticCustomError

from kora.ir import AgentConfig, IrJsonObjectField
from kora.ir.json import JsonObject, JsonValue
from kora.ir.record import IrRecord
from tests.ir.support import agent_document, deep_data


def test_ir_open_json_fields_preserve_nested_values() -> None:
    model = AgentConfig
    document = agent_document(
        {"x-open": {"boolean": False, "null": None}, "items": [True, -1, 1.25, ""]}
    )

    parsed = model.model_validate(document)

    assert parsed.model_dump(exclude_unset=True) == document


@pytest.mark.parametrize(
    "value",
    [
        {"v": object()},
        {"v": {1: "value"}},
        {"v": float("nan")},
        {"v": float("inf")},
        {"v": (1,)},
        {"v": {1}},
        [],
        False,
        None,
    ],
)
def test_ir_open_json_fields_reject_non_json_values(value: object) -> None:
    model = AgentConfig
    document = agent_document(value)

    with pytest.raises(ValidationError):
        model.model_validate(document)


def test_ir_open_json_data_and_exports_are_detached() -> None:
    from kora.ir import export_ir

    model = AgentConfig
    data: dict[str, object] = {"items": [{"value": 1}]}
    document = agent_document(data)

    parsed = model.model_validate(document)
    data["items"] = False
    exported = export_ir(parsed)
    cast("JsonObject", exported["output_schema"])["items"] = None

    assert export_ir(parsed)["output_schema"] == {"items": [{"value": 1}]}


@pytest.mark.parametrize("depth", [17, 64, 128, 255, 256, 512, 1024])
def test_ir_open_json_fields_do_not_inherit_protocol_bounds(depth: int) -> None:
    from kora.ir import render_ir_json

    model = AgentConfig
    document = agent_document(deep_data(depth))

    parsed = model.model_validate(document)
    rendered = render_ir_json(parsed)

    expected = (
        '{"x":[' * (depth // 2)
        + ('{"x":0}' if depth % 2 else "0")
        + "]}" * (depth // 2)
    )
    assert (
        rendered
        == '{"harness":"claude","prompt":"","workspace":{"repo":""},"output_schema":'
        + expected
        + "}"
    )


@pytest.mark.parametrize(
    "value", [None, False, 0, "", [], [True, None], {"items": False}]
)
def test_default_value_preserves_false_null_and_scalar_values(value: JsonValue) -> None:
    from kora.ir import HumanInputConfig, export_ir

    document = {
        "channel": "cli",
        "timeout": "0s",
        "on_expiry": "default_value",
        "default_value": value,
    }

    parsed = HumanInputConfig.model_validate(document)

    assert export_ir(parsed) == document


def test_embedded_schemas_remain_open_object_data() -> None:
    from kora.ir import AgentConfig, HumanInputConfig, SensorPoll, export_ir

    schema: JsonObject = {
        "properties": {"anything": True, "nothing": False},
        "x-data": [None],
    }
    documents = [
        (AgentConfig, agent_document(schema)),
        (
            HumanInputConfig,
            {"channel": "", "timeout": "0s", "on_expiry": "fail", "form": schema},
        ),
        (SensorPoll, {"sensor": "http", "until": "true", "config": schema}),
    ]

    exported = [
        export_ir(model.model_validate(document)) for model, document in documents
    ]

    assert exported == [document for _, document in documents]


def test_ir_open_json_alias_amplification_is_refused() -> None:
    from kora.ir import export_ir

    model = AgentConfig
    previous: dict[str, object] = {}
    for _ in range(64):
        previous = {"a": previous, "b": previous}
    repeated_empty: dict[str, object] = {}
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    fixtures: list[dict[str, object]] = [
        previous,
        {"a": repeated_empty, "b": repeated_empty},
        cycle,
    ]
    distinct: dict[str, object] = {"a": [], "b": []}

    parsed = model.model_validate(agent_document(distinct))
    for fixture in fixtures:
        with pytest.raises(ValidationError):
            model.model_validate(agent_document(fixture))

    assert export_ir(parsed) == agent_document(distinct)


@pytest.mark.parametrize(
    "integer",
    [2**13999, 2**14499, 2**19999, 10**5000 - 1],
    ids=["14000-bits", "14500-bits", "20000-bits", "5000-nines"],
)
def test_ir_canonical_export_handles_deep_and_large_values(integer: int) -> None:

    from kora.ir import export_ir, render_ir_json

    model = AgentConfig
    document = agent_document({"n": integer, "payload": "a" * 1048576})

    parsed = model.model_validate(document)
    exported = export_ir(parsed)
    rendered = render_ir_json(parsed)

    assert cast("JsonObject", exported["output_schema"])["n"] == integer
    expected_integer = "9" * 5000 if integer == 10**5000 - 1 else str(Decimal(integer))
    assert (
        rendered == '{"harness":"claude","prompt":"","workspace":{"repo":""},'
        '"output_schema":{"n":'
        + expected_integer
        + ',"payload":"'
        + "a" * 1048576
        + '"}}'
    )


def test_ir_canonical_export_preserves_model_wire_contract() -> None:
    from kora.ir import BranchConfig, Retry, ToolPolicy, export_ir, render_ir_json
    from tests.ir.remaining_config_cases import MINIMAL, POPULATED

    documents: list[tuple[type[IrRecord], dict[str, object]]] = [
        *MINIMAL.items(),
        *POPULATED.items(),
        (BranchConfig, {"cases": [{"when": "true", "to": ["a"]}], "else": []}),
        (Retry, {"retry_on": ["timeout"]}),
        (ToolPolicy, {}),
        (ToolPolicy, {"x-meta": False}),
        (AgentConfig, {**MINIMAL[AgentConfig], "tool_policy": {}}),
    ]

    exported = [
        export_ir(model.model_validate(document)) for model, document in documents
    ]
    rendered = render_ir_json(
        AgentConfig.model_validate(
            agent_document({"é\n😀": "\ud800\t😀", "float": -0.0})
        )
    )

    assert exported == [document for _, document in documents]
    assert (
        rendered == '{"harness":"claude","prompt":"","workspace":{"repo":""},'
        '"output_schema":{"\\u00e9\\n\\ud83d\\ude00":'
        '"\\ud800\\t\\ud83d\\ude00","float":-0.0}}'
    )


def test_ir_canonical_export_refuses_mutated_non_json_data() -> None:
    from kora.ir import export_ir, render_ir_json

    model = AgentConfig
    parsed = model.model_validate(agent_document({"v": 1}))
    data = cast("dict[str, object]", parsed.output_schema)
    data["v"] = object()

    refusals: list[str] = []
    for export in (export_ir, render_ir_json):
        with pytest.raises(PydanticCustomError) as refused:
            export(parsed)
        refusals.append(refused.value.type)

    assert refusals == ["ir_data_invalid", "ir_data_invalid"]


class NonIrRecord(BaseModel):
    value: StrictStr = "record"


@pytest.mark.parametrize("root", [{}, [], False, None, 1, "record", NonIrRecord()])
def test_ir_canonical_export_refuses_non_record_roots(root: object) -> None:
    from kora.ir import export_ir, render_ir_json

    unsupported = cast("IrRecord", root)

    refusals: list[str] = []
    for export in (export_ir, render_ir_json):
        with pytest.raises(PydanticCustomError) as refused:
            export(unsupported)
        refusals.append(refused.value.type)

    assert refusals == ["ir_data_invalid", "ir_data_invalid"]


@pytest.mark.parametrize("field", ["hooks", "output_schema"])
def test_open_data_alias_refusal_tracks_sibling_lists_and_preserves_separate_fields(
    field: str,
) -> None:
    from kora.ir import export_ir

    empty: list[JsonValue] = []
    aliased: JsonObject = {"a": empty, "b": empty}
    shared: JsonObject = {"v": []}
    document = {
        "harness": "claude",
        "prompt": "",
        "workspace": {"repo": ""},
        "hooks": shared,
        "output_schema": shared,
    }

    parsed = AgentConfig.model_validate(document)
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**document, field: aliased})
    exported = export_ir(parsed)

    assert exported["hooks"] == exported["output_schema"] == {"v": []}
    assert exported["hooks"] is not exported["output_schema"]


def test_any_value_data_is_detached_and_revalidated_during_export() -> None:
    from kora.ir import HumanInputConfig, export_ir, render_ir_json
    from kora.ir.json import JsonValue

    source: list[JsonValue] = [{"v": 1}]
    parsed = HumanInputConfig.model_validate(
        {
            "channel": "",
            "timeout": "0s",
            "on_expiry": "default_value",
            "default_value": source,
        }
    )

    source.append(False)
    first = export_ir(parsed)
    cast("list[JsonValue]", first["default_value"]).append(None)
    second = export_ir(parsed)
    data = cast("list[object]", parsed.default_value)
    data.append(data)

    refusals: list[str] = []
    for export in (export_ir, render_ir_json):
        with pytest.raises(PydanticCustomError) as refused:
            export(parsed)
        refusals.append(refused.value.type)

    assert second["default_value"] == [{"v": 1}]
    assert refusals == ["ir_data_invalid", "ir_data_invalid"]


@pytest.mark.parametrize("shape", ["maps", "arrays"])
@pytest.mark.parametrize("depth", [17, 64, 128, 255, 256, 512, 1024])
def test_canonical_export_preserves_deep_single_container_shapes(
    shape: str, depth: int
) -> None:
    from kora.ir import render_ir_json

    value: JsonValue = 0
    for _ in range(depth):
        value = {"x": value} if shape == "maps" else [value]
    document = agent_document({"deep": value})
    expected = (
        ('{"x":' * depth + "0" + "}" * depth)
        if shape == "maps"
        else ("[" * depth + "0" + "]" * depth)
    )

    rendered = render_ir_json(AgentConfig.model_validate(document))

    assert (
        rendered == '{"harness":"claude","prompt":"","workspace":{"repo":""},'
        '"output_schema":{"deep":' + expected + "}}"
    )


def test_ir_open_json_fields_release_dense_and_late_invalid_data() -> None:
    values: list[object] = [{"v": index} for index in range(9999)]
    values.append({"v": object()})
    dense: list[object] = [{"v": object()} for _ in range(10000)]
    documents = [agent_document({"values": values}), agent_document({"values": dense})]

    for document in documents:
        with pytest.raises(ValidationError) as raised:
            AgentConfig.model_validate(document)
        types = [error["type"] for error in raised.value.errors(include_input=False)]

        assert types == ["ir_data_invalid"]
        assert "object at" not in str(raised.value)


class CanonicalAliasRecord(IrRecord):
    schema_uri: StrictStr = Field(alias="$schema")
    schema_data: IrJsonObjectField = Field(alias="schema")
    source: StrictStr = Field(alias="from")
    default: StrictStr = "default"
    _private_count: int = PrivateAttr(default=3)


def test_canonical_export_omits_private_fields_and_preserves_wire_aliases() -> None:
    from kora.ir import export_ir, render_ir_json

    document = {
        "$schema": " raw value ",
        "schema": {"nested": False},
        "from": "$inputs",
    }

    parsed = CanonicalAliasRecord.model_validate(document)
    for name in ("schema_uri", "schema_data", "source"):
        with pytest.raises(ValidationError):
            CanonicalAliasRecord.model_validate({**document, name: "internal"})

    assert export_ir(parsed) == document
    assert (
        render_ir_json(parsed)
        == '{"$schema":" raw value ","schema":{"nested":false},"from":"$inputs"}'
    )


def test_canonical_export_refuses_mutated_non_extension_keys() -> None:
    from kora.ir import export_ir, render_ir_json

    document = {"harness": "claude", "prompt": "", "workspace": {"repo": ""}}
    parsed = AgentConfig.model_validate(document)
    parsed.extensions["harness"] = "codex"

    refusals: list[str] = []
    for export in (export_ir, render_ir_json):
        with pytest.raises(PydanticCustomError) as refused:
            export(parsed)
        refusals.append(refused.value.type)

    assert refusals == ["ir_data_invalid", "ir_data_invalid"]


CANONICAL_WRITER_CASES: list[tuple[JsonValue, str]] = [
    (None, "null"),
    (True, "true"),
    (False, "false"),
    (0, "0"),
    (-1, "-1"),
    (1.0, "1.0"),
    (-0.0, "-0.0"),
    (1e20, "1e+20"),
    (5e-324, "5e-324"),
    ('\b\f\n\r\t\\"\ud800', '"\\b\\f\\n\\r\\t\\\\\\"\\ud800"'),
    ({"first": [], "second": {}}, '{"first":[],"second":{}}'),
    ([None, True, {"x": False}, []], '[null,true,{"x":false},[]]'),
]


@pytest.mark.parametrize(("value", "encoded"), CANONICAL_WRITER_CASES)
def test_canonical_writer_preserves_scalar_bytes_and_container_separators(
    value: JsonValue, encoded: str
) -> None:
    from kora.ir import render_ir_json

    parsed = AgentConfig.model_validate(agent_document({"value": value}))

    rendered = render_ir_json(parsed)

    assert rendered == (
        '{"harness":"claude","prompt":"","workspace":{"repo":""},'
        '"output_schema":{"value":' + encoded + "}}"
    )
