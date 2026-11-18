from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import cast

from kora.plugins.harness.claude.stop import refused
from kora.protocols.harness import MAX_SAMPLE_ITEMS, DenialSample, PermissionDeniedData

PRE_EXECUTION_FORM = "<tool_use_error>"
UNKNOWN_TOOL = "mcp__unobserved__tool"


def bounded_identifier(value: str) -> bool:
    return 1 <= len(value) <= 256 and all(
        33 <= ord(character) <= 126 for character in value
    )


def pre_execution_rejection(tool: str, is_error: bool, content: str | None) -> bool:
    return (
        is_error
        and not tool.startswith("mcp__")
        and content is not None
        and content.startswith(PRE_EXECUTION_FORM)
    )


@dataclass(frozen=True)
class HarnessDenials:
    sample: DenialSample
    matched: frozenset[str]
    unmatched: int


def harness_denials(
    entries: object, observed: Mapping[str, str], callback_denied: Set[str]
) -> HarnessDenials:
    if entries is None:
        return HarnessDenials(DenialSample(items=(), total=0), frozenset(), 0)
    if not isinstance(entries, list):
        refused()
    items: list[PermissionDeniedData] = []
    matched: set[str] = set()
    unmatched = 0
    for entry in cast("list[object]", entries):
        if not isinstance(entry, dict):
            refused()
        fields = cast("dict[object, object]", entry)
        tool = fields.get("tool_name")
        identity = fields.get("tool_use_id")
        if not isinstance(tool, str) or not isinstance(identity, str):
            refused()
        if identity in callback_denied:
            continue
        if identity not in observed:
            unmatched += 1
            continue
        if identity in matched:
            continue
        matched.add(identity)
        if bounded_identifier(tool) and len(items) < MAX_SAMPLE_ITEMS:
            items.append(
                PermissionDeniedData(
                    tool=tool,
                    tool_use_id=identity,
                    source="harness",
                    code="harness_rule",
                )
            )
    sample = DenialSample(items=tuple(items), total=len(matched))
    return HarnessDenials(sample, frozenset(matched), unmatched)
