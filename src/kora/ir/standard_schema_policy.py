from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Never, cast


class SchemaDomain(StrEnum):
    OUTPUT = "kora.structured-output-schema-policy.v2"
    PARAMETERS = "kora.graph-parameter-evaluation.v2"


Reason = Literal["unsupported_policy", "unsupported_schema", "evaluation_exhausted"]
LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "max_value_raw_bytes": 1048576,
        "max_value_canonical_bytes": 1048576,
        "max_value_depth": 16,
        "max_value_integer_bits": 14000,
        "max_value_nodes": 262144,
        "max_value_string_bytes": 1048576,
        "max_value_array_items": 65536,
        "max_value_object_members": 16384,
        "max_schema_source_bytes": 1048576,
        "max_schema_nodes": 16384,
        "max_schema_depth": 32,
        "max_schema_documents": 64,
        "max_schema_positions": 2048,
        "max_reference_edges": 4096,
        "max_schema_string_bytes": 65536,
        "max_schema_array_items": 4096,
        "max_schema_object_members": 4096,
        "max_schema_integer_bits": 14000,
        "max_numeric_intermediate_bits": 32768,
        "max_call_depth": 32,
        "max_expanded_calls": 131072,
        "max_validation_score": 67108864,
        "max_unique_items": 128,
        "max_repr_characters": 8388608,
        "max_variable_bytes": 268435456,
        "max_auxiliary_entries": 1048576,
    }
)


class SchemaRefused(Exception):
    def __init__(self, reason: Reason = "unsupported_schema") -> None:
        self.reason: Reason = reason
        super().__init__(reason)


def refuse(reason: Reason = "unsupported_schema") -> Never:
    raise SchemaRefused(reason) from None


def require(condition: bool, *, capacity: bool = False) -> None:
    if not condition:
        refuse("evaluation_exhausted" if capacity else "unsupported_schema")


def record(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    require(type(value) is dict)
    mapping = cast("dict[object, object]", value)
    require(len(mapping) == len(keys))
    for actual, expected in zip(mapping, keys, strict=True):
        require(type(actual) is str and actual == expected)
    return cast("dict[str, object]", mapping)


def domain_limits(domain: SchemaDomain) -> dict[str, int]:
    limits = dict(LIMITS)
    if domain is SchemaDomain.PARAMETERS:
        limits.update(
            max_value_raw_bytes=4194304,
            max_value_canonical_bytes=4194304,
            max_value_depth=32,
            max_value_integer_bits=64,
        )
    return limits


def bind_policy(domain: SchemaDomain, value: object) -> Mapping[str, int]:
    if type(domain) is not SchemaDomain:
        refuse("unsupported_policy")
    expected = domain_limits(domain)
    keys = (
        ("policy", "parameters")
        if domain is SchemaDomain.OUTPUT
        else ("profile", "limits")
    )
    try:
        binding = record(value, keys)
        identity = binding[keys[0]]
        require(type(identity) is str and identity == domain.value)
        supplied = record(binding[keys[1]], tuple(expected))
        for key, limit in expected.items():
            require(type(supplied[key]) is int and supplied[key] == limit)
    except SchemaRefused:
        refuse("unsupported_policy")
    return MappingProxyType(expected)


@dataclass(frozen=True, slots=True)
class SourceAccounting:
    source_bytes: int = 0
    source_nodes: int = 0
    schema_positions: int = 0
    reference_edges: int = 0
    schema_documents: int = 0
    preparation_score: int = 0


@dataclass(frozen=True, slots=True)
class SourceDecision:
    outcome: Literal["source_checks_completed", "refused"]
    reason: Reason | Literal["none"] = "none"
    accounting: SourceAccounting = SourceAccounting()


@dataclass(frozen=True, slots=True)
class SchemaAccounting:
    value_nodes: int = 0
    schema_nodes: int = 0
    schema_positions: int = 0
    reference_edges: int = 0
    expanded_calls: int = 0
    validation_score: int = 0
    variable_bytes: int = 0
    auxiliary_entries: int = 0


@dataclass(frozen=True, slots=True)
class SchemaDecision:
    outcome: Literal["valid", "invalid", "refused"]
    reason: Reason | Literal["none"] = "none"
    accounting: SchemaAccounting = SchemaAccounting()
