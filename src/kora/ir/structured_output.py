from dataclasses import dataclass, field
from hashlib import sha256
from typing import cast

from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import (
    StructuredOutputValidationError as StructuredOutputValidationError,
)
from kora.ir.structured_json import decode_json, encode_snapshot, refuse, snapshot


@dataclass(frozen=True, slots=True, init=False)
class StructuredOutput:
    _canonical: bytes = field(repr=False)

    def __init__(self, value: object) -> None:
        canonical = encode_snapshot(snapshot(value))
        object.__setattr__(self, "_canonical", canonical)

    @property
    def value(self) -> JsonValue:
        return checked_value(self)

    def canonical_bytes(self) -> bytes:
        checked_value(self)
        return self._canonical


def checked_value(value: StructuredOutput) -> JsonValue:
    if type(value) is not StructuredOutput:
        refuse("SO003")
    raw: object = getattr(value, "_canonical", None)
    if type(raw) is not bytes:
        refuse("SO003")
    result = snapshot(decode_json(raw), "SO002")
    if encode_snapshot(result) != raw:
        refuse("SO003")
    return result


def parse_structured_output(value: object) -> StructuredOutput:
    if type(value) is StructuredOutput:
        return StructuredOutput(checked_value(value))
    return StructuredOutput(value)


def parse_structured_output_json(payload: bytes) -> StructuredOutput:
    return StructuredOutput(decode_json(payload))


def canonical_structured_output(value: StructuredOutput) -> bytes:
    return StructuredOutput.canonical_bytes(value)


def structured_output_digest(value: StructuredOutput) -> str:
    return sha256(canonical_structured_output(value)).hexdigest()


def summarize_structured_output(value: StructuredOutput) -> JsonValue:
    return checked_value(value)


_DESCRIPTOR: JsonObject = {
    "name": "StructuredOutput",
    "builtin_revision": "1",
    "schema": "urn:kora:artifact:StructuredOutput",
    "parent": None,
    "behavior": {
        "validation": "kora.structured-output-validation.v1",
        "digest_fields": "kora.json-payload.v1",
        "summary": "kora.json-payload.v1",
    },
    "validation": {
        "max_raw_json_bytes": 1048576,
        "max_canonical_json_bytes": 1048576,
        "max_json_depth": 16,
        "max_integer_bits": 14000,
    },
    "canonicalization": "kora.structured-json.v1",
    "compatibility": {
        "execution_semantics": "kora.execution.v1",
        "checker_view": "kora.structured-output-view.v1",
    },
    "digest_selection": [""],
    "schema_sha256": "e924b9a44c0825f0b2a43b0291d1f98c4851425a50e291914dd85ba096ac6eae",
}


def same_descriptor(value: object, expected: JsonValue) -> bool:
    if type(value) is not type(expected):
        return False
    if type(expected) is dict:
        entries = cast("dict[object, object]", value)
        if len(entries) != len(expected):
            return False
        return all(
            type(key) is str
            and key in expected
            and same_descriptor(item, expected[key])
            for key, item in entries.items()
        )
    if type(expected) is list:
        items = cast("list[object]", value)
        return len(items) == len(expected) and all(
            same_descriptor(left, right)
            for left, right in zip(items, expected, strict=True)
        )
    return value == expected


def require_structured_output_revision(descriptor: object) -> None:
    if not same_descriptor(descriptor, _DESCRIPTOR):
        refuse("SO005")
