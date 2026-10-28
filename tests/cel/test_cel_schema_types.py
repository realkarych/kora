import json
from pathlib import Path
from typing import Protocol, cast

import pytest
from jsonschema import Draft202012Validator

from kora.cel import (
    CheckedExpression,
    Environment,
    JsonValue,
    ResultKind,
    SchemaError,
    SchemaTypes,
    SchemaView,
    ValueBound,
    Variable,
    check_typed,
)
from kora.cel.cost import DATA_BOUND
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
from kora.cel_eval.bounds import admitted
from tests.cel.generator import SeededSource

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
        "definition_named_id",
        {"$defs": {"$id": {"type": "string"}}, "$ref": "#/$defs/$id"},
        SchemaView.PLAIN,
        STRING,
    ),
    (
        "pointer_through_property_named_id",
        {
            "$defs": {
                "T": {
                    "type": "object",
                    "properties": {
                        "$id": {"type": "integer"},
                        "a": {"type": "boolean"},
                    },
                }
            },
            "$ref": "#/$defs/T/properties/a",
        },
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
    "resource_on_pointer_path": {
        "type": "object",
        "$defs": {
            "c": {"type": "integer"},
            "E": {
                "$id": "urn:kora:artifact:embedded",
                "$defs": {"c": {"type": "string"}},
                "type": "object",
                "properties": {"x": {"$ref": "#/$defs/c"}},
            },
        },
        "properties": {"m": {"$ref": "#/$defs/E/properties/x"}},
        "required": ["m"],
    },
    "non_schema_pattern": {
        "patternProperties": {"^x": 5},
        "additionalProperties": False,
    },
    "malformed_enum_beside_const": {"const": "a", "enum": 5},
    "resource_under_content_schema": {
        "$defs": {"c": {"type": "integer"}},
        "contentSchema": {
            "$id": "urn:kora:artifact:embedded",
            "$defs": {"c": {"type": "string"}},
            "properties": {"x": {"$ref": "#/$defs/c"}},
        },
        "type": "object",
        "properties": {"m": {"$ref": "#/contentSchema/properties/x"}},
        "required": ["m"],
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


def recursive_closure(width: int) -> JsonValue:
    definitions: dict[str, JsonValue] = {
        f"d{index}": {
            "type": "object",
            "properties": {
                "left": {"$ref": f"#/$defs/d{index + 1}"},
                "right": {"$ref": f"#/$defs/d{index + 1}"},
                "items": {"type": "array", "items": {"$ref": f"#/$defs/d{index + 1}"}},
                "self": {"$ref": f"#/$defs/d{index}"},
                "value": {"type": "integer"},
            },
            "required": ["left", "right", "items", "self", "value"],
        }
        for index in range(width)
    }
    definitions[f"d{width}"] = {"$ref": "#/$defs/d0"}
    return {"$defs": definitions, "$ref": "#/$defs/d0"}


def closure_steps(width: int) -> int:
    meter = Meter()
    types = SchemaTypes(no_documents, meter)
    root = types.document(recursive_closure(width))
    reads = " && ".join(["v.left.right.self.value > 0"] * 8)
    environment = Environment({"v": Variable(root)}, ResultKind.BOOL)
    source = reads + " && v.items.all(i, i.left.value > 0)"
    checked = check_typed(source, environment, meter)
    assert isinstance(checked, CheckedExpression)
    return meter.steps


def test_cel_recursive_closure_checked_in_bounded_time() -> None:
    widths = (64, 128, 256, 512)
    meter = Meter()
    types = SchemaTypes(no_documents, meter)

    root = types.document(recursive_closure(4))
    counts = [closure_steps(width) for width in widths]

    assert isinstance(root, Record)
    assert root.fields["self"].type is root
    assert root.fields["left"].type is not root
    items = root.fields["items"].type
    assert isinstance(items, ListOf)
    assert items.element is root.fields["right"].type
    for width, count in zip(widths, counts, strict=True):
        assert count <= 32 * width + 4096
    for smaller, larger in zip(counts, counts[1:], strict=False):
        assert larger <= 2.5 * smaller


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


def test_cel_schema_types_forget_partial_types_after_a_resolver_failure() -> None:
    documents: dict[str, JsonValue] = {}
    faulty: JsonValue = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"$ref": CANDIDATE}},
        "required": ["a", "b"],
    }
    types = SchemaTypes(documents.__getitem__)

    with pytest.raises(KeyError):
        types.document(faulty)
    with pytest.raises(KeyError):
        types.document(faulty)


def test_cel_schema_types_resolve_each_document_against_its_own_root() -> None:
    inner: JsonValue = {"$ref": "#/$defs/n"}
    parent: JsonValue = {
        "$defs": {"n": {"type": "integer"}},
        "type": "object",
        "properties": {"p": inner},
    }
    types = SchemaTypes(no_documents)
    types.document(parent)

    with pytest.raises(SchemaError):
        types.document(inner)


def declared(
    items: int = 1000,
    string_bytes: int = 16384,
    element: ValueBound | None = None,
    fields: dict[str, ValueBound] | None = None,
) -> ValueBound:
    return ValueBound(4096, 256, items, string_bytes, element, fields or {})


BOUND_TABLE: list[tuple[str, JsonValue, ValueBound]] = [
    ("max_items", {"type": "array", "maxItems": 3}, declared(items=3)),
    ("integral_float", {"type": "array", "maxItems": 2.0}, declared(items=2)),
    ("max_length", {"type": "string", "maxLength": 5}, declared(string_bytes=20)),
    ("wide_length", {"type": "string", "maxLength": 10_000}, DATA_BOUND),
    ("wide_items", {"type": "array", "maxItems": 2000}, DATA_BOUND),
    (
        "element",
        {"type": "array", "items": {"type": "string", "maxLength": 2}},
        declared(element=declared(string_bytes=8)),
    ),
    (
        "record",
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "maxLength": 1},
                "b": {"type": "integer"},
            },
            "additionalProperties": {"type": "array", "maxItems": 0},
        },
        declared(
            element=declared(items=0),
            fields={"a": declared(string_bytes=4), "b": declared()},
        ),
    ),
    (
        "loose_property_beside_additional",
        {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "additionalProperties": {"type": "string", "maxLength": 1},
        },
        declared(element=declared(string_bytes=4), fields={"description": declared()}),
    ),
    (
        "property_beside_single_pattern",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "patternProperties": {"^x": {"type": "array", "maxItems": 1}},
            "additionalProperties": False,
        },
        declared(element=declared(items=1), fields={"name": declared()}),
    ),
    (
        "recursive_beside_additional",
        {
            "$defs": {
                "n": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/$defs/n"}},
                    "additionalProperties": {"type": "array", "maxItems": 0},
                }
            },
            "$ref": "#/$defs/n",
        },
        declared(element=declared(items=0), fields={"next": declared()}),
    ),
    (
        "map",
        {"type": "object", "additionalProperties": {"type": "string", "maxLength": 3}},
        declared(element=declared(string_bytes=12)),
    ),
    (
        "single_pattern",
        {
            "type": "object",
            "patternProperties": {"^x": {"type": "array", "maxItems": 1}},
            "additionalProperties": False,
        },
        declared(element=declared(items=1)),
    ),
    ("nullable_type", {"type": ["array", "null"], "maxItems": 4}, declared(items=4)),
    (
        "nullable_pair",
        {"anyOf": [{"type": "null"}, {"type": "string", "maxLength": 1}]},
        declared(string_bytes=4),
    ),
    (
        "reference",
        {"$defs": {"s": {"type": "string", "maxLength": 2}}, "$ref": "#/$defs/s"},
        declared(string_bytes=8),
    ),
    ("single_all_of", {"allOf": [{"type": "array", "maxItems": 5}]}, declared(items=5)),
    (
        "prefix_items",
        {
            "type": "array",
            "prefixItems": [{"type": "string", "maxLength": 1}],
            "maxItems": 2,
        },
        declared(items=2),
    ),
    (
        "reference_sibling",
        {"$defs": {"a": {"type": "array"}}, "$ref": "#/$defs/a", "maxItems": 1},
        DATA_BOUND,
    ),
    ("items_on_string", {"type": "string", "maxItems": 1}, DATA_BOUND),
    ("length_on_array", {"type": "array", "maxLength": 1}, DATA_BOUND),
    ("length_on_object", {"type": "object", "maxLength": 1}, DATA_BOUND),
    (
        "several_types",
        {"type": ["string", "array"], "maxLength": 1, "maxItems": 1},
        DATA_BOUND,
    ),
    (
        "recursive",
        {
            "$defs": {
                "n": {
                    "type": "object",
                    "properties": {
                        "next": {"$ref": "#/$defs/n"},
                        "tag": {"type": "string", "maxLength": 1},
                    },
                }
            },
            "$ref": "#/$defs/n",
        },
        declared(fields={"tag": declared(string_bytes=4)}),
    ),
]


@pytest.mark.parametrize("view", list(SchemaView))
@pytest.mark.parametrize(
    ("schema", "expected"),
    [(schema, expected) for _, schema, expected in BOUND_TABLE],
    ids=[name for name, _, _ in BOUND_TABLE],
)
def test_cel_schema_bounds_follow_max_items_and_max_length(
    schema: JsonValue, expected: ValueBound, view: SchemaView
) -> None:
    types = SchemaTypes(no_documents)

    variable = types.variable(schema, view)

    assert variable.bound == expected
    assert variable.type is types.document(schema, view)


def test_cel_schema_bounds_follow_references_through_the_resolver() -> None:
    documents: dict[str, JsonValue] = {
        CANDIDATE: {"type": "array", "items": {"type": "string", "maxLength": 3}}
    }
    types = SchemaTypes(documents.get)

    variable = types.reference_variable(CANDIDATE)

    assert variable.bound == declared(element=declared(string_bytes=12))


BOUNDED_SCHEMAS: list[JsonValue] = [
    {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 2}},
    {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
            "name": {"type": ["string", "null"], "maxLength": 4},
        },
        "additionalProperties": {"type": "string", "maxLength": 1},
    },
    {
        "type": "object",
        "additionalProperties": {
            "type": "array",
            "maxItems": 1,
            "items": {"type": "string", "maxLength": 1},
        },
    },
    {
        "type": "object",
        "properties": {"name": {"type": "string"}, "tags": {"type": "array"}},
        "additionalProperties": {"type": "string", "maxLength": 1},
    },
    {
        "type": "object",
        "properties": {"name": {}},
        "patternProperties": {"^x": {"type": "array", "maxItems": 1}},
        "additionalProperties": False,
    },
]


class SchemaValidator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


ALPHABET: str = "a\u00e9\u4e2d\U0001f600"


def random_text(chooser: SeededSource) -> str:
    return "".join(chooser.choice(ALPHABET) for _ in range(chooser.randrange(0, 7)))


def random_value(chooser: SeededSource, depth: int = 0) -> JsonValue:
    if depth >= 3:
        return random_text(chooser)
    kind = chooser.randrange(0, 4)
    if kind == 0:
        return random_text(chooser)
    if kind == 1:
        size = chooser.randrange(0, 5)
        return [random_value(chooser, depth + 1) for _ in range(size)]
    if kind == 2:
        names = ["tags", "name", "x", "y"]
        size = chooser.randrange(0, 4)
        return {
            chooser.choice(names): random_value(chooser, depth + 1) for _ in range(size)
        }
    return None


@pytest.mark.parametrize("schema", BOUNDED_SCHEMAS)
def test_cel_schema_bounds_admit_every_valid_value(schema: JsonValue) -> None:
    chooser = SeededSource(20261028)
    validator = cast(
        "SchemaValidator", Draft202012Validator(cast("dict[str, object]", schema))
    )
    bound = SchemaTypes(no_documents).variable(schema).bound
    candidates = [random_value(chooser) for _ in range(4000)]

    valid = [value for value in candidates if validator.is_valid(value)]
    invalid = [value for value in candidates if not validator.is_valid(value)]
    accepted = [admitted(value, bound) for value in valid]
    refused = [not admitted(value, bound) for value in invalid]

    assert len(valid) >= 50
    assert all(accepted)
    assert any(refused)


@pytest.mark.parametrize("keyword", ["maxItems", "maxLength"])
@pytest.mark.parametrize("value", [-1, 1.5, True, None, "3", float("inf")])
def test_cel_schema_bounds_refuse_malformed_keywords(
    keyword: str, value: JsonValue
) -> None:
    schema: JsonValue = {
        "type": "object",
        "properties": {"a": {"type": "string", keyword: value}},
    }
    types = SchemaTypes(no_documents)

    with pytest.raises(SchemaError):
        types.variable(schema)


def bounded_items(depth: int) -> JsonValue:
    schema: JsonValue = {"type": "string", "maxLength": 1}
    for _ in range(depth):
        schema = {"type": "array", "maxItems": 2, "items": schema}
    return schema


def bound_depth(bound: ValueBound) -> int:
    depth = 0
    element = bound.element
    while element is not None:
        depth, element = depth + 1, element.element
    return depth


def variable_steps(schema: JsonValue) -> tuple[int, int]:
    typed_meter, bound_meter = Meter(), Meter()
    SchemaTypes(no_documents, typed_meter).document(schema)
    SchemaTypes(no_documents, bound_meter).variable(schema)
    return typed_meter.steps, bound_meter.steps


def test_cel_schema_bounds_are_linear_in_steps() -> None:
    deep = SchemaTypes(no_documents).variable(bounded_items(5000))
    lengths = (250, 500, 1000, 2000)

    shared = [variable_steps(shared_chain(length)) for length in lengths]
    chains = [variable_steps(reference_chain(length)) for length in lengths]

    assert bound_depth(deep.bound) == 5000
    for typed_steps, bound_steps in [*shared, *chains]:
        assert bound_steps == typed_steps
    for smaller, larger in zip(shared, shared[1:], strict=False):
        assert larger[1] <= 2.5 * smaller[1]
