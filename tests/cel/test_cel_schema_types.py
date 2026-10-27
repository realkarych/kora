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


def nested_list(depth: int) -> JsonValue:
    value: JsonValue = "a"
    for _ in range(depth):
        value = [value]
    return value


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
        "open_pattern_map",
        {"type": "object", "patternProperties": {"^a": {"type": "boolean"}}},
        SchemaView.PLAIN,
        MapOf(STRING, DYN),
    ),
    (
        "closed_pattern_map",
        {
            "type": "object",
            "patternProperties": {"^a": {"type": "boolean"}},
            "additionalProperties": False,
        },
        SchemaView.PLAIN,
        MapOf(STRING, BOOL),
    ),
    (
        "pattern_beside_additional",
        {
            "type": "object",
            "patternProperties": {"^a": {"type": "number"}},
            "additionalProperties": {"type": "integer"},
        },
        SchemaView.PLAIN,
        MapOf(STRING, DYN),
    ),
    (
        "closed_pattern_record",
        {
            "properties": {"a": {"type": "string"}},
            "patternProperties": {"^x": {"type": "integer"}},
            "additionalProperties": False,
        },
        SchemaView.PLAIN,
        Record({"a": RecordField(STRING, False)}, INT),
    ),
    (
        "pattern_record_beside_additional",
        {
            "properties": {},
            "patternProperties": {"^x": {"type": "integer"}},
            "additionalProperties": {"type": "string"},
        },
        SchemaView.PLAIN,
        Record({}, DYN),
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


MALFORMED_CLOSURES: dict[str, JsonValue] = {
    "unknown_document": {"$ref": "urn:kora:artifact:missing"},
    "unknown_pointer": {"$ref": "#/$defs/missing"},
    "anchor": {"$ref": "#anchor"},
    "numeric_type": {"type": 5},
    "numeric_type_name": {"type": ["string", 5]},
    "unknown_type_name": {"type": "text"},
    "non_list_enum": {"enum": "a"},
    "non_list_required": {"properties": {"a": {"type": "string"}}, "required": "a"},
    "non_string_required": {"properties": {"a": {}}, "required": ["a", 1]},
    "nested_required": {"properties": {"a": {}}, "required": [nested_list(1200)]},
    "list_properties": {"type": "object", "properties": []},
    "numeric_additional_properties": {"type": "object", "additionalProperties": 7},
    "numeric_items": {"type": "array", "items": 3},
    "numeric_prefix_items": {"type": "array", "prefixItems": 5},
    "object_any_of": {"anyOf": {"type": "string"}},
    "long_pointer_index": {
        "allOf": [{}, {}],
        "properties": {"x": {"$ref": "#/allOf/" + "1" * 5000}},
    },
    "nested_resource": {
        "properties": {
            "inner": {
                "$id": "urn:other",
                "$defs": {"x": {"type": "integer"}},
                "$ref": "#/$defs/x",
            }
        },
        "$defs": {"x": {"type": "string"}},
    },
    "null_reference": {"$ref": None},
    "null_type": {"type": None},
    "null_enum": {"enum": None},
    "leading_zero_index": {"$ref": "#/allOf/01", "allOf": [{}, {}]},
    "non_schema": [],
}


@pytest.mark.parametrize("name", sorted(MALFORMED_CLOSURES))
def test_cel_schema_types_refuse_a_closure_the_validator_must_reject(
    name: str,
) -> None:
    schema = MALFORMED_CLOSURES[name]
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


def shared_chain(length: int) -> JsonValue:
    definitions: dict[str, JsonValue] = {
        f"d{index}": {"$ref": f"#/$defs/d{index + 1}"} for index in range(length)
    }
    definitions[f"d{length}"] = {"type": "string"}
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {
            f"p{index}": {"anyOf": [{"$ref": "#/$defs/d0"}, {"type": "null"}]}
            for index in range(length)
        },
    }


def shared_chain_steps(length: int) -> int:
    meter = Meter()
    SchemaTypes(no_documents, meter).document(shared_chain(length))
    return meter.steps


def test_cel_schema_types_map_shared_reference_chains_in_linear_steps() -> None:
    lengths = (250, 500, 1000, 2000)

    counts = [shared_chain_steps(length) for length in lengths]

    for length, count in zip(lengths, counts, strict=True):
        assert count <= 16 * length
    for smaller, larger in zip(counts, counts[1:], strict=False):
        assert larger <= 2.5 * smaller


def test_cel_schema_types_refuse_again_after_a_closure_fault() -> None:
    faulty: JsonValue = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"$ref": "#/$defs/missing"}},
        "required": ["a", "b"],
    }
    valid: JsonValue = {"type": "object", "properties": {"a": {"type": "integer"}}}
    types = SchemaTypes(no_documents)

    with pytest.raises(SchemaError):
        types.document(faulty)
    with pytest.raises(SchemaError):
        types.document(faulty)
    result = types.document(valid)

    assert result == Record({"a": RecordField(INT, False)})
