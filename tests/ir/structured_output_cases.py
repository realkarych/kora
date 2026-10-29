import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from kora.ir import StructuredOutput
from kora.ir.structured_json import MAX_JSON_BYTES
from tests.ir.test_structured_output_contract import fixture_bytes

ARTIFACTS: Path = Path(__file__).resolve().parents[2] / "knowledge/specs/artifacts"


@dataclass(frozen=True)
class CanonicalCase:
    name: str
    raw: bytes
    canonical: bytes
    digest: str


def canonical_cases() -> list[CanonicalCase]:
    vectors = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "StructuredOutput.vectors.json").read_bytes()),
    )
    return [
        CanonicalCase(
            name=cast("str", case["name"]),
            raw=fixture_bytes(case["raw"]),
            canonical=fixture_bytes(case["canonical"]),
            digest=cast("str", case["canonical_sha256"]),
        )
        for case in cast("list[dict[str, object]]", vectors["canonical_cases"])
    ]


def raw_refusals() -> list[tuple[str, bytes]]:
    vectors = cast(
        "dict[str, object]",
        json.loads((ARTIFACTS / "StructuredOutput.vectors.json").read_bytes()),
    )
    return [
        (
            cast("str", case["name"]),
            fixture_bytes(case["raw"])
            if "raw" in case
            else bytes.fromhex(cast("str", case["raw_hex"])),
        )
        for case in cast("list[dict[str, object]]", vectors["raw_refusals"])
    ]


def forged(raw: object) -> StructuredOutput:
    value = object.__new__(StructuredOutput)
    object.__setattr__(value, "_canonical", raw)
    return value


def exact_string(size: int) -> str:
    return "a" * (size - 2)


def nested(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


def shared_tree() -> object:
    value: object = "a" * 1024
    for _ in range(12):
        value = [value, value]
    return value


def hostile_inputs() -> dict[str, Callable[[], object]]:
    return {
        "wide_list": lambda: [None] * MAX_JSON_BYTES,
        "wide_dict": lambda: {str(i): None for i in range(MAX_JSON_BYTES // 4)},
        "large_string": lambda: "a" * (8 * MAX_JSON_BYTES),
        "large_integer": lambda: 1 << (8 * MAX_JSON_BYTES),
        "shared_tree": shared_tree,
    }
