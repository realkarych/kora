import enum
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

from kora.ir.globs import (
    StarPattern,
    folded,
    has_control_character,
    needle_weight,
    non_ascii_count,
    relative_path_pattern_valid,
    searchable,
    star_pattern,
    utf8_size,
    wildcard_touches_mark,
)
from kora.ir.node import ToolPolicy

EDIT_PATH_NAMES: Final[frozenset[str]] = frozenset(
    ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")
)

MAX_PATTERNS: Final[int] = 128


MAX_ARRAY_BYTES: Final[int] = 65_536


MAX_PATTERN_CHARACTERS: Final[int] = 4096


MAX_LAYER_PIECES: Final[int] = 8192


MAX_LAYER_NON_ASCII: Final[int] = 16_384


NAME_CHARACTERS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.*-"
)


MCP_PREFIX: Final[str] = "mcp__"


MCP_SEPARATOR: Final[str] = "__"


ArrayName = Literal["allow", "deny"]


class Refusal(enum.Enum):
    DOCUMENT = "the document is not a tool policy"
    OUTPUT_CHANNEL = "the tool pattern names the reserved output channel"
    ARRAY = "the pattern array is too large"
    MALFORMED = "the tool pattern is malformed"
    TOO_LONG = "the tool pattern is too long"
    PIECE = "a wildcard piece of the tool pattern is too long"
    UNSUPPORTED = "the tool pattern uses a harness form kora does not support"
    EDIT_PATH = "the file-edit pattern argument is not a relative path pattern"
    PIECES = "the tool policy has too many wildcard pieces"
    FOLDING = "the tool policy has too many characters outside ASCII"
    MARK = "a wildcard of the deny pattern touches a combining mark"


@dataclass(frozen=True)
class ToolPattern:
    index: int
    name: StarPattern
    name_text: str
    argument: StarPattern | None
    tail_free: StarPattern | None
    server: StarPattern | None


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
        return ToolPattern(index, name_pattern, name, None, None, server)
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
        return ToolPattern(index, name_pattern, name, exact, None, server)
    if wildcard_touches_mark(argument, folded_pattern):
        return Refusal.MARK
    return ToolPattern(
        index=index,
        name=name_pattern,
        name_text=name,
        argument=folded_pattern,
        tail_free=tail_free_of(folded_pattern),
        server=server,
    )


def array_too_large(patterns: tuple[str, ...]) -> bool:
    return len(patterns) > MAX_PATTERNS or utf8_size(patterns) > MAX_ARRAY_BYTES


def array_refusals(
    array: ArrayName, patterns: tuple[str, ...], *, reserve_output_channel: bool = False
) -> Iterator[tuple[tuple[str | int, ...], Refusal]]:
    if array_too_large(patterns):
        yield ((array,), Refusal.ARRAY)
        return
    for index, pattern in enumerate(patterns):
        result = compiled(index, pattern, array)
        if isinstance(result, Refusal):
            yield ((array, index), result)
        elif reserve_output_channel and result.name_text == "StructuredOutput":
            yield ((array, index), Refusal.OUTPUT_CHANNEL)


def piece_count(patterns: tuple[str, ...]) -> int:
    return sum(text.count("*") + 1 for text in patterns)


def too_many_pieces(policy: ToolPolicy) -> bool:
    return piece_count(policy.allow) + piece_count(policy.deny) > MAX_LAYER_PIECES


def too_much_folding(policy: ToolPolicy) -> bool:
    return non_ascii_count(policy.allow + policy.deny) > MAX_LAYER_NON_ASCII


def policy_refusals(
    policy: ToolPolicy, *, reserve_output_channel: bool = False
) -> Iterator[tuple[tuple[str | int, ...], Refusal]]:
    yield from array_refusals(
        "allow", policy.allow, reserve_output_channel=reserve_output_channel
    )
    yield from array_refusals(
        "deny", policy.deny, reserve_output_channel=reserve_output_channel
    )
    if too_many_pieces(policy):
        yield ((), Refusal.PIECES)
    if too_much_folding(policy):
        yield ((), Refusal.FOLDING)


def tail_free_of(pattern: StarPattern) -> StarPattern | None:
    pieces = pattern.pieces
    if len(pieces) < 2 or pieces[-1] or not pieces[-2].endswith(" "):
        return None
    kept = (*pieces[:-2], pieces[-2][:-1])
    return StarPattern(kept, needle_weight(max(map(len, kept[1:-1]), default=0)))


def server_pattern(name: str) -> StarPattern | None:
    if not name.startswith(MCP_PREFIX) or MCP_SEPARATOR in name[len(MCP_PREFIX) :]:
        return None
    return star_pattern(name + MCP_SEPARATOR + "*")


def tool_policy_refusals(
    policy: ToolPolicy,
) -> Iterator[tuple[tuple[str | int, ...], Refusal]]:
    yield from policy_refusals(policy, reserve_output_channel=True)
