import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

from kora.ir import ContractViolation, ToolPolicy
from kora.ir.globs import Fuel, star_match
from kora.ir.json import JsonObject
from kora.ir.tool_patterns import (
    EDIT_PATH_NAMES,
    MAX_ARRAY_BYTES,
    MAX_LAYER_NON_ASCII,
    MAX_LAYER_PIECES,
    MAX_PATTERN_CHARACTERS,
    MAX_PATTERNS,
    MCP_PREFIX,
    MCP_SEPARATOR,
    NAME_CHARACTERS,
    ArrayName,
    Refusal,
    ToolPattern,
    array_refusals,
    array_too_large,
    compiled,
    piece_count,
    policy_refusals,
    server_pattern,
    split_pattern,
    tail_free_of,
    too_many_pieces,
    too_much_folding,
)

__all__ = [
    "ArrayName",
    "EDIT_PATH_NAMES",
    "MAX_ARRAY_BYTES",
    "MAX_LAYER_NON_ASCII",
    "MAX_LAYER_PIECES",
    "MAX_PATTERNS",
    "MAX_PATTERN_CHARACTERS",
    "MCP_PREFIX",
    "MCP_SEPARATOR",
    "NAME_CHARACTERS",
    "Refusal",
    "ToolPattern",
    "array_refusals",
    "array_too_large",
    "compiled",
    "piece_count",
    "policy_refusals",
    "server_pattern",
    "split_pattern",
    "tail_free_of",
    "too_many_pieces",
    "too_much_folding",
]

MAX_DIAGNOSTICS: Final[int] = 1000
MAX_DIAGNOSTIC_BYTES: Final[int] = 1_048_576
POLICY_CODE: Final[str] = "IR013"
FAMILY_NAMES: Final[tuple[str, ...]] = ("Edit", "Write")
FILE_EDIT_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "claude": ("Edit", "MultiEdit", "NotebookEdit", "Write"),
    "codex": ("apply_patch",),
}
LayerName = Literal["namespace", "node"]


class DiagnosticsLimit(Exception):
    def __init__(self) -> None:
        super().__init__("the policy has too many violations")


class LayerInvalid(Exception):
    def __init__(self) -> None:
        super().__init__("a policy layer is invalid")


@dataclass(frozen=True)
class Layer:
    name: LayerName
    allow: tuple[ToolPattern, ...]
    deny: tuple[ToolPattern, ...]


def document_refusals(
    document: JsonObject,
) -> Iterator[tuple[tuple[str | int, ...], Refusal]]:
    try:
        policy = ToolPolicy.model_validate(document)
    except ValidationError:
        policy = None
    if policy is None:
        yield ((), Refusal.DOCUMENT)
        return
    yield from policy_refusals(policy)


def violation(path: tuple[str | int, ...], refusal: Refusal) -> ContractViolation:
    return ContractViolation(
        code=POLICY_CODE, message=refusal.value, path=path if path else None
    )


def encoded_size(item: ContractViolation) -> int:
    return len(
        json.dumps(
            item.model_dump(mode="json", exclude_none=True), separators=(",", ":")
        )
    )


def validated(document: JsonObject) -> list[ContractViolation]:
    found: list[ContractViolation] = []
    size = 2
    for path, refusal in document_refusals(document):
        item = violation(path, refusal)
        size += encoded_size(item) + 1
        if len(found) == MAX_DIAGNOSTICS or size > MAX_DIAGNOSTIC_BYTES:
            raise DiagnosticsLimit
        found.append(item)
    return found


def parsed_array(
    array: ArrayName, patterns: tuple[str, ...]
) -> tuple[ToolPattern, ...]:
    if array_too_large(patterns):
        raise LayerInvalid
    parsed: list[ToolPattern] = []
    for index, text in enumerate(patterns):
        result = compiled(index, text, array)
        if isinstance(result, Refusal):
            raise LayerInvalid
        parsed.append(result)
    return tuple(parsed)


def parsed_layer(name: LayerName, policy: ToolPolicy) -> Layer:
    if too_many_pieces(policy) or too_much_folding(policy):
        raise LayerInvalid
    return Layer(
        name=name,
        allow=parsed_array("allow", policy.allow),
        deny=parsed_array("deny", policy.deny),
    )


def name_matches(pattern: ToolPattern, tool: str, fuel: Fuel) -> bool:
    if star_match(pattern.name, tool, fuel):
        return True
    return pattern.server is not None and star_match(pattern.server, tool, fuel)


def is_edit_tool(harness: str, tool: str) -> bool:
    return tool in FILE_EDIT_TOOLS.get(harness, ())


def deny_governs(pattern: ToolPattern, harness: str, tool: str, fuel: Fuel) -> bool:
    if name_matches(pattern, tool, fuel):
        return True
    return is_edit_tool(harness, tool) and any(
        name_matches(pattern, family, fuel) for family in FAMILY_NAMES
    )


def allow_governs(pattern: ToolPattern, harness: str, tool: str, fuel: Fuel) -> bool:
    if name_matches(pattern, tool, fuel):
        return True
    return is_edit_tool(harness, tool) and pattern.name_text in FAMILY_NAMES


def deny_governs_edits(pattern: ToolPattern, harness: str, fuel: Fuel) -> bool:
    return any(
        deny_governs(pattern, harness, tool, fuel)
        for tool in FILE_EDIT_TOOLS.get(harness, ())
    )


def allow_governs_edits(pattern: ToolPattern, harness: str, fuel: Fuel) -> bool:
    return any(
        allow_governs(pattern, harness, tool, fuel)
        for tool in FILE_EDIT_TOOLS.get(harness, ())
    )
