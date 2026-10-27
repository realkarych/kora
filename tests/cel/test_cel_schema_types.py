import json
from pathlib import Path

import pytest

from kora.cel import JsonValue, SchemaError, SchemaTypes, SchemaView
from kora.cel.meter import Meter
from kora.cel.types import (
    BOOL,
    DOUBLE,
    DYN,
    INT,
    NULL,
    STRING,
    CelType,
    Kind,
    ListOf,
    MapOf,
    Nullable,
    Primitive,
    Record,
    RecordField,
)

ROOT: Path = Path(__file__).resolve().parents[2]
VCS_REF_SCHEMA: Path = ROOT / "knowledge/specs/artifacts/VcsRef.schema.json"
CANDIDATE: str = "urn:kora:artifact:dwh_core:Candidate"


def no_documents(uri: str) -> JsonValue | None:
    return None


def mapped(schema: JsonValue, view: SchemaView = SchemaView.PLAIN) -> CelType:
    return SchemaTypes(no_documents).document(schema, view)


def enum_of(*values: str) -> CelType:
    return Primitive(Kind.STRING, frozenset(values))


MAPPING_TABLE: list[tuple[str, JsonValue, SchemaView, CelType]] = [
    ("pure_null_in_view", {"type": "null"}, SchemaView.STRUCTURED_OUTPUT, NULL),
    ("const_null_in_view", {"const": None}, SchemaView.STRUCTURED_OUTPUT, NULL),
    ("enum_null_in_view", {"enum": [None]}, SchemaView.STRUCTURED_OUTPUT, NULL),
    ("pure_null_outside_view", {"type": "null"}, SchemaView.PLAIN, DYN),
    ("type_pair", {"type": ["string", "null"]}, SchemaView.PLAIN, Nullable(STRING)),
    (
        "any_of_pair",
        {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        SchemaView.PLAIN,
        Nullable(INT),
    ),
    (
        "one_of_pair",
        {"oneOf": [{"const": None}, {"type": "boolean"}]},
        SchemaView.PLAIN,
        Nullable(BOOL),
    ),
    ("enum_with_null", {"enum": ["a", None]}, SchemaView.PLAIN, Nullable(enum_of("a"))),
    (
        "nullable_not_wrapped_twice",
        {"anyOf": [{"type": ["string", "null"]}, {"type": "null"}]},
        SchemaView.PLAIN,
        Nullable(STRING),
    ),
    (
        "record",
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
            "required": ["a"],
        },
        SchemaView.PLAIN,
        Record({"a": RecordField(INT, True), "b": RecordField(STRING, False)}),
    ),
    (
        "closed_record",
        {"properties": {"a": {"type": "integer"}}, "additionalProperties": False},
        SchemaView.PLAIN,
        Record({"a": RecordField(INT, False)}, None),
    ),
    (
        "typed_extra",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": {"type": "number"},
        },
        SchemaView.PLAIN,
        Record({}, DOUBLE),
    ),
    ("open_map", {"type": "object"}, SchemaView.PLAIN, MapOf(STRING, DYN)),
    (
        "typed_map",
        {"type": "object", "additionalProperties": {"type": "integer"}},
        SchemaView.PLAIN,
        MapOf(STRING, INT),
    ),
    (
        "pattern_map",
        {"type": "object", "patternProperties": {"^a": {"type": "boolean"}}},
        SchemaView.PLAIN,
        MapOf(STRING, BOOL),
    ),
    (
        "two_pattern_map",
        {
            "type": "object",
            "patternProperties": {"^a": {"type": "boolean"}, "^b": {}},
        },
        SchemaView.PLAIN,
        MapOf(STRING, DYN),
    ),
    ("string", {"type": "string"}, SchemaView.PLAIN, STRING),
    ("string_enum", {"enum": ["x", "y"]}, SchemaView.PLAIN, enum_of("x", "y")),
    ("string_const", {"const": "x"}, SchemaView.PLAIN, enum_of("x")),
    (
        "typed_string_enum",
        {"type": "string", "enum": ["x"]},
        SchemaView.PLAIN,
        enum_of("x"),
    ),
    ("integer", {"type": "integer"}, SchemaView.PLAIN, INT),
    ("integer_enum", {"type": "integer", "enum": [1, 2]}, SchemaView.PLAIN, INT),
    ("number", {"type": "number"}, SchemaView.PLAIN, DOUBLE),
    ("number_enum", {"enum": [1, 2.5]}, SchemaView.PLAIN, DOUBLE),
    ("number_const", {"const": 3}, SchemaView.PLAIN, DOUBLE),
    ("boolean", {"type": "boolean"}, SchemaView.PLAIN, BOOL),
    (
        "list",
        {"type": "array", "items": {"type": "string"}},
        SchemaView.PLAIN,
        ListOf(STRING),
    ),
    ("untyped_items", {"items": {"type": "integer"}}, SchemaView.PLAIN, ListOf(INT)),
    ("list_without_items", {"type": "array"}, SchemaView.PLAIN, ListOf(DYN)),
    (
        "prefix_items",
        {"prefixItems": [{"type": "string"}], "items": {"type": "string"}},
        SchemaView.PLAIN,
        ListOf(DYN),
    ),
    ("several_types", {"type": ["string", "integer"]}, SchemaView.PLAIN, DYN),
    (
        "any_of",
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        SchemaView.PLAIN,
        DYN,
    ),
    (
        "all_of",
        {"allOf": [{"type": "string"}, {"minLength": 1}]},
        SchemaView.PLAIN,
        DYN,
    ),
    ("mixed_enum", {"enum": ["a", 1]}, SchemaView.PLAIN, DYN),
    ("true_schema", True, SchemaView.PLAIN, DYN),
    ("false_schema", False, SchemaView.PLAIN, DYN),
    ("empty_schema", {}, SchemaView.PLAIN, DYN),
    ("single_all_of", {"allOf": [{"type": "integer"}]}, SchemaView.PLAIN, INT),
    (
        "local_reference",
        {"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"},
        SchemaView.PLAIN,
        INT,
    ),
    (
        "escaped_pointer",
        {"$defs": {"a/b~c": {"type": "boolean"}}, "$ref": "#/$defs/a~1b~0c"},
        SchemaView.PLAIN,
        BOOL,
    ),
    (
        "reference_cycle",
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}},
        SchemaView.PLAIN,
        DYN,
    ),
]


@pytest.mark.parametrize(
    ("schema", "view", "expected"),
    [case[1:] for case in MAPPING_TABLE],
    ids=[case[0] for case in MAPPING_TABLE],
)
def test_cel_schema_types_follow_the_mapping_table(
    schema: JsonValue, view: SchemaView, expected: CelType
) -> None:
    types = SchemaTypes(no_documents)

    result = types.document(schema, view)

    assert result == expected


def test_cel_schema_types_resolve_references_only_through_the_closure() -> None:
    candidate: JsonValue = {
        "$id": CANDIDATE,
        "type": "object",
        "properties": {"gain": {"type": "number"}},
        "required": ["gain"],
        "$defs": {"label": {"type": "string"}},
    }
    documents: dict[str, JsonValue] = {CANDIDATE: candidate}
    types = SchemaTypes(documents.get)
    schema: JsonValue = {
        "type": "object",
        "properties": {
            "best": {"$ref": CANDIDATE},
            "label": {"$ref": CANDIDATE + "#/$defs/label"},
        },
        "required": ["best", "label"],
    }

    result = types.document(schema)
    referenced = types.reference(CANDIDATE)

    assert isinstance(result, Record)
    assert result.fields["best"].type is referenced
    assert result.fields["label"].type == STRING
    assert referenced == Record({"gain": RecordField(DOUBLE, True)})


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "urn:kora:artifact:missing"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "#anchor"},
        {"type": 5},
        {"type": ["string", 5]},
        {"type": "text"},
        {"enum": "a"},
        {"properties": {"a": {"type": "string"}}, "required": "a"},
        [],
    ],
    ids=[
        "unknown_document",
        "unknown_pointer",
        "anchor",
        "numeric_type",
        "numeric_type_name",
        "unknown_type_name",
        "non_list_enum",
        "non_list_required",
        "non_schema",
    ],
)
def test_cel_schema_types_refuse_a_closure_the_validator_must_reject(
    schema: JsonValue,
) -> None:
    types = SchemaTypes(no_documents)

    with pytest.raises(SchemaError):
        types.document(schema)


def test_cel_schema_types_map_the_published_vcs_ref() -> None:
    schema: JsonValue = json.loads(VCS_REF_SCHEMA.read_text())

    result = mapped(schema)

    assert isinstance(result, Record)
    assert result.extra is None
    assert result.fields["commit"] == RecordField(STRING, True)
    assert result.fields["opened_by"].required is False
    principal = result.fields["pushed_by"].type
    assert isinstance(principal, Record)
    assert principal is result.fields["opened_by"].type
    assert principal.fields["kind"].required is True


def nested_items(depth: int) -> JsonValue:
    schema: JsonValue = {"type": "integer"}
    for _ in range(depth):
        schema = {"type": "array", "items": schema}
    return schema


def reference_chain(length: int) -> JsonValue:
    definitions: dict[str, JsonValue] = {
        f"d{index}": {
            "type": "object",
            "properties": {"next": {"$ref": f"#/$defs/d{index + 1}"}},
        }
        for index in range(length)
    }
    definitions[f"d{length}"] = {"type": "string"}
    return {"$defs": definitions, "$ref": "#/$defs/d0"}


def list_depth(cel_type: CelType) -> tuple[int, CelType]:
    depth = 0
    while isinstance(cel_type, ListOf):
        cel_type, depth = cel_type.element, depth + 1
    return depth, cel_type


def test_cel_schema_types_map_deep_closures_without_recursion() -> None:
    meter = Meter()
    types = SchemaTypes(no_documents, meter)

    lists = types.document(nested_items(5000))
    chain = types.document(reference_chain(5000))

    assert list_depth(lists) == (5000, INT)
    assert isinstance(chain, Record)
    assert meter.steps <= 16 * 10_000
