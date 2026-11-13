from collections.abc import Mapping
from dataclasses import dataclass

from kora.db.claim_sources_v1 import MAX_SOURCE_COUNT, SOURCE_FIELDS
from kora.ir.graph_json import ObjectReader, TextBudget, render_object, scalar_text
from kora.ir.json import JsonValue


class ClaimSourcesRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("claim sources are unavailable")


@dataclass(frozen=True, slots=True)
class SourceAllowance:
    native: int
    transport: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 < value <= MAX_SOURCE_COUNT
            for value in (self.native, self.transport)
        ):
            raise ClaimSourcesRejected()


@dataclass(frozen=True, slots=True, repr=False)
class PreparedSource:
    name: str
    text: str | None
    native_bytes: int
    transport_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class PreparedClaimSources:
    fields: tuple[PreparedSource, ...]


def _source_bound(value: JsonValue, allowance: int) -> int:
    pending = [value]
    total = 0
    while pending:
        member = pending.pop()
        if isinstance(member, dict):
            total += 64 + 32 * len(member)
            for key, child in member.items():
                pending.extend((key, child))
        elif isinstance(member, list):
            total += 64 + 32 * len(member)
            pending.extend(member)
        elif type(member) is str:
            if "\0" in member:
                raise ClaimSourcesRejected()
            total += 64 + 12 * len(member.encode("utf-8"))
        elif type(member) is bool:
            total += 32
        elif type(member) is int:
            total += 64 + 2 * len(scalar_text(member, TextBudget(allowance)))
        elif type(member) is float:
            total += 1024
        else:
            total += 64
        if total > allowance:
            raise ClaimSourcesRejected()
    return total


def _prepare_claim_sources(
    values: Mapping[str, JsonValue], allowances: Mapping[str, SourceAllowance]
) -> PreparedClaimSources:
    if (
        not values
        or not values.keys() <= set(SOURCE_FIELDS)
        or values.keys() != allowances.keys()
    ):
        raise ClaimSourcesRejected()
    fields: list[PreparedSource] = []
    for name in SOURCE_FIELDS:
        if name not in values:
            continue
        value = values[name]
        if value is None:
            if name == "input_bindings":
                raise ClaimSourcesRejected()
            fields.append(PreparedSource(name, None, 0, 0))
            continue
        limit = allowances[name]
        encoded = render_object({"v": value}, TextBudget(limit.transport + 6))
        detached = ObjectReader(encoded).object()["v"]
        bound = _source_bound(detached, min(limit.native, limit.transport))
        fields.append(PreparedSource(name, encoded[5:-1], bound, bound))
    return PreparedClaimSources(tuple(fields))


def decode_claim_source(payload: bytes, encoding: str) -> JsonValue:
    text = payload.decode(encoding)
    fragments: list[str] = []
    quoted = False
    escaped = False
    for character in text:
        if quoted or character not in " \t\r\n":
            fragments.append(character)
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
    compact = "".join(fragments)
    return ObjectReader('{"v":' + compact + "}").object()["v"]


def prepare_claim_sources(
    values: Mapping[str, JsonValue], allowances: Mapping[str, SourceAllowance]
) -> PreparedClaimSources:
    try:
        return _prepare_claim_sources(values, allowances)
    except (ValueError, TypeError):
        del values, allowances
        error = ClaimSourcesRejected()
        try:
            raise error from None
        finally:
            error.__cause__ = None
            error.__context__ = None
