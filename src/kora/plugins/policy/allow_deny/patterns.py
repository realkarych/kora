import enum
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

from kora.ir import ContractViolation, ToolPolicy
from kora.ir.globs import (
    Fuel,
    StarPattern,
    folded,
    has_control_character,
    needle_weight,
    non_ascii_count,
    relative_path_pattern_valid,
    searchable,
    star_match,
    star_pattern,
    touches_mark,
    utf8_size,
)
from kora.ir.json import JsonObject

MAX_PATTERNS: Final[int] = 128
MAX_ARRAY_BYTES: Final[int] = 65_536
MAX_PATTERN_CHARACTERS: Final[int] = 4096
MAX_LAYER_PIECES: Final[int] = 8192
MAX_LAYER_NON_ASCII: Final[int] = 16_384
MAX_DIAGNOSTICS: Final[int] = 1000
MAX_DIAGNOSTIC_BYTES: Final[int] = 1_048_576
POLICY_CODE: Final[str] = "IR013"
NAME_CHARACTERS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.*-"
)
FAMILY_NAMES: Final[tuple[str, ...]] = ("Edit", "Write")
FILE_EDIT_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "claude": ("Edit", "MultiEdit", "NotebookEdit", "Write"),
    "codex": ("apply_patch",),
}
EDIT_PATH_NAMES: Final[frozenset[str]] = frozenset(
    {*FAMILY_NAMES, *(tool for tools in FILE_EDIT_TOOLS.values() for tool in tools)}
)
MCP_PREFIX: Final[str] = "mcp__"
MCP_SEPARATOR: Final[str] = "__"
LayerName = Literal["namespace", "node"]
ArrayName = Literal["allow", "deny"]


class Refusal(enum.Enum):
    DOCUMENT = "the document is not a tool policy"
    ARRAY = "the pattern array is too large"
    MALFORMED = "the tool pattern is malformed"
    TOO_LONG = "the tool pattern is too long"
    PIECE = "a wildcard piece of the tool pattern is too long"
    UNSUPPORTED = "the tool pattern uses a harness form kora does not support"
    EDIT_PATH = "the file-edit pattern argument is not a relative path pattern"
    PIECES = "the tool policy has too many wildcard pieces"
    FOLDING = "the tool policy has too many characters outside ASCII"
    MARK = "a wildcard of the deny pattern touches a combining mark"


class DiagnosticsLimit(Exception):
    def __init__(self) -> None:
        super().__init__("the policy has too many violations")


class LayerInvalid(Exception):
    def __init__(self) -> None:
        super().__init__("a policy layer is invalid")


@dataclass(frozen=True)
class ToolPattern:
    index: int
    name: StarPattern
    name_text: str
    argument: StarPattern | None
    folded_argument: StarPattern | None
    tail_free: StarPattern | None
    server: StarPattern | None


@dataclass(frozen=True)
class Layer:
    name: LayerName
    allow: tuple[ToolPattern, ...]
    deny: tuple[ToolPattern, ...]


def split_pattern(text: str) -> tuple[str, str | None] | Refusal:
    if not text:
        return Refusal.MALFORMED
    if len(text) > MAX_PATTERN_CHARACTERS:
        return Refusal.TOO_LONG
    opening = text.find("(")
    if opening < 0:
        return (text, None)
    if not text.endswith(")") or opening >= len(text) - 2:
        return Refusal.MALFORMED
    argument = text[opening + 1 : -1]
    if "(" in argument or ")" in argument or has_control_character(argument):
        return Refusal.MALFORMED
    return (text[:opening], argument)


def compiled(index: int, text: str, array: ArrayName) -> ToolPattern | Refusal:
    split = split_pattern(text)
    if isinstance(split, Refusal):
        return split
    name, argument = split
    if not name or not NAME_CHARACTERS.issuperset(name):
        return Refusal.MALFORMED
    name_pattern = star_pattern(name)
    if not searchable(name_pattern):
        return Refusal.PIECE
    if ":*" in text:
        return Refusal.UNSUPPORTED
    server = server_pattern(name)
    if server is not None and not searchable(server):
        return Refusal.PIECE
    if argument is None:
        return ToolPattern(index, name_pattern, name, None, None, None, server)
    if argument.startswith(("//", "~/")):
        return Refusal.UNSUPPORTED
    if name in EDIT_PATH_NAMES and not relative_path_pattern_valid(argument):
        return Refusal.EDIT_PATH
    exact = star_pattern(argument)
    folded_text = folded(argument)
    folded_pattern = exact if folded_text == argument else star_pattern(folded_text)
    if not searchable(exact) or not searchable(folded_pattern):
        return Refusal.PIECE
    if array == "allow":
        return ToolPattern(index, name_pattern, name, exact, None, None, server)
    if touches_mark(folded_pattern):
        return Refusal.MARK
    return ToolPattern(
        index=index,
        name=name_pattern,
        name_text=name,
        argument=None,
        folded_argument=folded_pattern,
        tail_free=tail_free_of(folded_pattern),
        server=server,
    )


def array_too_large(patterns: tuple[str, ...]) -> bool:
    return len(patterns) > MAX_PATTERNS or utf8_size(patterns) > MAX_ARRAY_BYTES


def array_refusals(
    array: ArrayName, patterns: tuple[str, ...]
) -> Iterator[tuple[tuple[str | int, ...], Refusal]]:
    if array_too_large(patterns):
        yield ((array,), Refusal.ARRAY)
        return
    for index, pattern in enumerate(patterns):
        result = compiled(index, pattern, array)
        if isinstance(result, Refusal):
            yield ((array, index), result)


def piece_count(patterns: tuple[str, ...]) -> int:
    return sum(text.count("*") + 1 for text in patterns)


def too_many_pieces(policy: ToolPolicy) -> bool:
    return piece_count(policy.allow) + piece_count(policy.deny) > MAX_LAYER_PIECES


def too_much_folding(policy: ToolPolicy) -> bool:
    return non_ascii_count(policy.allow + policy.deny) > MAX_LAYER_NON_ASCII


def policy_refusals(
    policy: ToolPolicy,
) -> Iterator[tuple[tuple[str | int, ...], Refusal]]:
    yield from array_refusals("allow", policy.allow)
    yield from array_refusals("deny", policy.deny)
    if too_many_pieces(policy):
        yield ((), Refusal.PIECES)
    if too_much_folding(policy):
        yield ((), Refusal.FOLDING)


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


def tail_free_of(pattern: StarPattern) -> StarPattern | None:
    pieces = pattern.pieces
    if len(pieces) < 2 or pieces[-1] or not pieces[-2].endswith(" "):
        return None
    kept = (*pieces[:-2], pieces[-2][:-1])
    return StarPattern(kept, needle_weight(max(map(len, kept[1:-1]), default=0)))


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


def server_pattern(name: str) -> StarPattern | None:
    if not name.startswith(MCP_PREFIX) or MCP_SEPARATOR in name[len(MCP_PREFIX) :]:
        return None
    return star_pattern(name + MCP_SEPARATOR + "*")


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
