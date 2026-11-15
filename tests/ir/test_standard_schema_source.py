import math
from typing import cast

import pytest

from kora.ir.json import JsonObject
from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_source import Sources
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, chain, decide, source
from tests.ir.structured_schema_cases import (
    HostileArray,
    HostileDictionary,
    HostileInteger,
    HostileString,
)


@pytest.mark.parametrize("domain", tuple(SchemaDomain))
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "old",
        "boolean",
        "float",
        "order",
        "outer_order",
        "missing",
        "unknown",
        "empty",
    ],
)
def test_standard_schema_source_bindings_are_exact(
    domain: SchemaDomain, change: str
) -> None:
    candidate = binding(domain)
    identity, parameters = tuple(candidate)
    limits = cast("dict[str, object]", candidate[parameters])
    if change == "old":
        candidate[identity] = domain.value[:-1] + "1"
    elif change in {"boolean", "float"}:
        limits["max_value_depth"] = True if change == "boolean" else 16.0
    elif change == "order":
        candidate[parameters] = dict(reversed(tuple(limits.items())))
    elif change == "outer_order":
        candidate = dict(reversed(tuple(candidate.items())))
    elif change == "missing":
        limits.pop("max_value_depth")
    elif change == "unknown":
        limits["unknown"] = 1
    elif change == "empty":
        candidate = {}

    result = check_standard_schema_source(source(True), domain, candidate)

    assert result.outcome == (
        "source_checks_completed" if change == "none" else "refused"
    )
    assert result.reason == ("none" if change == "none" else "unsupported_policy")


@pytest.mark.parametrize(
    "location", ["root", "unchosen", "definition", "reference_document"]
)
@pytest.mark.parametrize(
    "keyword",
    ["pattern", "patternProperties", "unevaluatedItems", "unevaluatedProperties"],
)
def test_standard_schema_source_checks_every_document(
    location: str, keyword: str
) -> None:
    unsupported = {keyword: "private"}
    schema: object = unsupported
    if location == "unchosen":
        schema = {"anyOf": [True, unsupported]}
    elif location == "definition":
        schema = {"$defs": {"hidden": unsupported}}
    candidate = source(schema)
    if location == "reference_document":
        candidate["documents"] = [
            {"name": None, "schema": True},
            {"name": "Other", "schema": unsupported},
        ]

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == "unsupported_schema"


def test_standard_schema_source_keeps_keyword_shaped_literal_data() -> None:
    literal = {
        "$id": "nested",
        "$schema": "unknown",
        "$ref": "https://example.invalid",
        "pattern": ".*",
    }

    result = decide(
        {"const": literal, "enum": [literal], "annotation": literal, "default": literal}
    )

    assert result.outcome == "source_checks_completed"
    assert result.accounting.schema_positions == 1
    assert result.accounting.reference_edges == 0


@pytest.mark.parametrize(
    ("schema", "supported"),
    [
        (True, True),
        (False, True),
        ({}, True),
        ({"$schema": "https://json-schema.org/draft/2020-12/schema"}, True),
        ({"$schema": "http://json-schema.org/draft-07/schema#"}, False),
        ({"$id": "urn:example#"}, True),
        ({"$id": "urn:example#fragment"}, False),
        ({"properties": {"a": {"$id": "urn:example"}}}, False),
        ({"$anchor": "_name-1.2", "$dynamicAnchor": "Name"}, True),
        ({"$anchor": "1name"}, False),
        ({"$anchor": "a/"}, False),
        ({"$recursiveAnchor": True}, False),
        ({"$recursiveRef": "https://example.invalid"}, True),
        ({"type": ["string", "null"]}, True),
        ({"type": []}, False),
        ({"type": ["string", "string"]}, False),
        ({"type": "invalid"}, False),
        ({"type": [1]}, False),
        ({"type": True}, False),
        ({"required": []}, True),
        ({"required": ["a", "a"]}, False),
        ({"required": ["", "a", "aa", "a\x00", "🌍", "é"]}, True),
        ({"required": ["🌍", "é", "🌍"]}, False),
        ({"required": ["x" * 1000 + "a", "x" * 1000 + "b"]}, True),
        ({"required": ["x" * 1000, "x" * 1000]}, False),
        ({"dependentRequired": {"a": []}}, True),
        ({"dependentRequired": {"a": [1]}}, False),
        ({"dependencies": {"a": ["b"], "b": True}}, True),
        ({"dependencies": {"a": ["b", "b"]}}, False),
        ({"properties": {"a": 1}}, False),
        ({"items": []}, False),
        ({"allOf": [True, False]}, True),
        ({"allOf": []}, False),
        ({"prefixItems": []}, False),
        ({"oneOf": [1]}, False),
        ({"enum": []}, True),
        ({"enum": [1, 1]}, True),
        ({"enum": {}}, False),
        ({"examples": []}, True),
        ({"examples": False}, False),
        ({"multipleOf": 0.3}, True),
        ({"multipleOf": 0}, False),
        ({"multipleOf": -1}, False),
        ({"minimum": True}, False),
        ({"minimum": -(1 << 13999)}, True),
        ({"minItems": 1.0}, True),
        ({"minItems": 0.5}, False),
        ({"maxLength": -1}, False),
        ({"minProperties": True}, False),
        ({"uniqueItems": False, "readOnly": True}, True),
        ({"uniqueItems": 1}, False),
        (
            {"$vocabulary": {"https://json-schema.org/draft/2020-12/vocab/core": True}},
            True,
        ),
        ({"$vocabulary": {"private": False}}, True),
        ({"$vocabulary": {"private": True}}, False),
        ({"$vocabulary": {"private": 1}}, False),
        ({"format": "not-a-known-format", "contentSchema": True}, True),
    ],
)
def test_standard_schema_source_arguments(schema: object, supported: bool) -> None:
    candidate = source(schema)

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("source_checks_completed" if supported else "refused")
    assert result.reason == ("none" if supported else "unsupported_schema")


@pytest.mark.parametrize(
    "reference",
    [
        "#/$defs/a~1b~0c",
        "#/%24defs/a~1b~0c",
        "urn:kora:artifact:example:Type#/$defs/a~1b~0c",
    ],
)
def test_standard_schema_source_original_references(reference: str) -> None:
    schema: JsonObject = {
        "$defs": {"a/b~c": False, "yes": True},
        "$ref": reference,
        "$dynamicRef": "#/$defs/yes",
    }
    candidate = source(schema)
    candidate["documents"] = [{"name": "example/Type", "schema": schema}]

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())
    prepared = Sources(candidate, cast("dict[str, int]", binding()["parameters"]))

    assert result.outcome == "source_checks_completed"
    assert result.accounting.reference_edges == 2
    assert prepared.sites[prepared.references[0, "$ref"]].schema is False
    assert prepared.sites[prepared.references[0, "$dynamicRef"]].schema is True


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid",
        "file:///private",
        "urn:kora:artifact:Missing",
        "#anchor",
        "#/$defs/missing",
        "#/const",
        "#/$defs",
        "#/$defs/a~2b",
        "#/%",
        "#/%00%FF",
        "#/%C0%AF",
        "#/%ED%A0%80",
        "#/%F4%90%80%80",
        "#/%E2%82",
        "#/$defs/é",
        "#/allOf/00",
        "#/allOf/9999999999999999999999",
        "#/allOf/-1",
        "#/allOf/1",
        "#/$defs/a#b",
    ],
)
def test_standard_schema_source_refuses_unresolved_or_non_schema_targets(
    reference: str,
) -> None:
    schema = {"$ref": reference, "$defs": {"a/b": True}, "allOf": [True], "const": True}

    result = decide(schema)

    assert result.reason == "unsupported_schema"


def test_standard_schema_source_percent_utf8_and_empty_pointer_segments() -> None:
    candidate = {
        "$defs": {"": {"$defs": {"🌍": True}}},
        "$ref": "#/$defs//$defs/%F0%9F%8C%8D",
    }

    result = decide(candidate)

    assert result.outcome == "source_checks_completed"


def test_standard_schema_source_equal_documents_keep_distinct_roots() -> None:
    root = {"$defs": {"leaf": True}, "$ref": "#/$defs/leaf"}
    candidate = source(root)
    candidate["documents"] = [
        {"name": "First", "schema": root},
        {"name": "Second", "schema": root},
    ]
    candidate["constraints"] = [0, 1, 0]

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())
    prepared = Sources(candidate, cast("dict[str, int]", binding()["parameters"]))

    assert result.outcome == "source_checks_completed"
    assert result.accounting.schema_documents == 2
    assert result.accounting.schema_positions == 4
    assert prepared.references[0, "$ref"] != prepared.references[2, "$ref"]
    assert list(prepared.roots()) == [0, 2, 0]


@pytest.mark.parametrize("hidden", [False, True])
def test_standard_schema_source_graph_support(hidden: bool) -> None:
    schema = chain(40, cycle=True)
    if hidden:
        schema.pop("$ref")
        definitions = cast("dict[str, object]", schema["$defs"])
        definitions["s38"] = {"$ref": "#/$defs/s0"}

    result = decide(schema)

    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize("count", [31, 32, 33])
def test_standard_schema_source_call_depth_uses_constraint_roots(count: int) -> None:
    candidate = chain(count)

    result = decide(candidate)
    candidate.pop("$ref")
    unused = decide(candidate)

    assert result.reason == ("evaluation_exhausted" if count > 32 else "none")
    assert unused.outcome == "source_checks_completed"


@pytest.mark.parametrize(
    "value",
    [
        math.inf,
        -math.inf,
        math.nan,
        "\ud800",
        {1: "invalid"},
        (1, 2),
        HostileString("x"),
        HostileInteger(2),
        HostileDictionary(),
        HostileArray(),
    ],
)
def test_standard_schema_source_rejects_non_native_data_without_callbacks(
    value: object,
) -> None:
    candidate = source(True, metadata=value)

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize("member", ["documents", "constraints", "metadata"])
def test_standard_schema_source_rejects_native_cycles(member: str) -> None:
    candidate = source(True)
    candidate[member] = candidate

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())
    candidate[member] = None

    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize("constraints", [[], [True], [-1], [1], [0.0], "0"])
def test_standard_schema_source_requires_actual_constraint_roots(
    constraints: object,
) -> None:
    candidate = source(True)
    candidate["constraints"] = constraints

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize(
    "documents",
    [
        [],
        {},
        [{"name": None, "schema": []}],
        [{"schema": True, "name": None}],
        [{"name": "Same", "schema": True}, {"name": "Same", "schema": False}],
        [{"name": "example:Type", "schema": True}],
        [{"name": "Bad#name", "schema": True}],
        [{"name": "", "schema": True}],
        [{"name": 1, "schema": True}],
    ],
)
def test_standard_schema_source_refuses_malformed_or_ambiguous_documents(
    documents: object,
) -> None:
    candidate = source(True)
    candidate["documents"] = documents

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == "unsupported_schema"


def test_standard_schema_source_root_id_does_not_rebase_local_references() -> None:
    candidate = {
        "$id": "https://example.invalid/private",
        "$defs": {"leaf": True},
        "$ref": "#/$defs/leaf",
        "$dynamicRef": "#/$defs/leaf",
    }

    result = decide(candidate)
    candidate["$dynamicRef"] = "#/$defs/missing"
    second = decide(candidate)

    assert result.outcome == "source_checks_completed"
    assert second.reason == "unsupported_schema"
