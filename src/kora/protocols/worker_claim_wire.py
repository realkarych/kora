from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from kora.ir.graph_json import (
    GraphValueRejected,
    TextBudget,
    decoded_string,
    quoted,
    render_object,
)

MAX_CLAIM_BYTES: Final[int] = 16_777_216
MAX_IMMUTABLE_BYTES: Final[int] = 12_582_912
MAX_POLICY_BYTES: Final[int] = 1_048_576
MAX_SYSTEM_BYTES: Final[int] = 3_145_728
MAX_NODE_POLICY_BYTES: Final[int] = 262_144
MAX_NAMESPACE_POLICY_BYTES: Final[int] = 524_288
MAX_POLICY_COMPOSITION_BYTES: Final[int] = 262_144
MAX_REQUEST_DESCRIPTOR_BYTES: Final[int] = 16_384
CLAIM_PART_JOIN_BYTES: Final[int] = 8


class ClaimPartition(StrEnum):
    IMMUTABLE = "immutable"
    POLICY = "policy"
    SYSTEM = "system"

    @property
    def limit(self) -> int:
        return {
            ClaimPartition.IMMUTABLE: MAX_IMMUTABLE_BYTES,
            ClaimPartition.POLICY: MAX_POLICY_BYTES,
            ClaimPartition.SYSTEM: MAX_SYSTEM_BYTES - CLAIM_PART_JOIN_BYTES,
        }[self]


class ClaimWireRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("the Claim representation is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedClaimPart:
    partition: ClaimPartition
    payload: bytes
    members: frozenset[str]


def prepare_claim_part(partition: ClaimPartition, value: object) -> PreparedClaimPart:
    try:
        payload = render_object(value, TextBudget(partition.limit, ascii_only=False))
        member_budget = TextBudget(partition.limit, ascii_only=False)
        members = frozenset(
            decoded_string(quoted(name, member_budget), 0)[0]
            for name in cast(dict[str, object], value)
        )
    except GraphValueRejected:
        raise ClaimWireRejected() from None
    return PreparedClaimPart(partition, payload.encode("utf-8"), members)


def claim_response(
    immutable: PreparedClaimPart,
    policy: PreparedClaimPart,
    system: PreparedClaimPart,
) -> bytes:
    parts = (immutable, policy, system)
    names: set[str] = set()
    for expected, part in zip(ClaimPartition, parts, strict=True):
        if (
            part.partition is not expected
            or len(part.payload) > expected.limit
            or not part.payload.startswith(b"{")
            or not part.payload.endswith(b"}")
            or names.intersection(part.members)
        ):
            raise ClaimWireRejected()
        names.update(part.members)
    maximum = sum(len(part.payload) for part in parts) + CLAIM_PART_JOIN_BYTES
    if maximum > MAX_CLAIM_BYTES:
        raise ClaimWireRejected()
    fields = [part.payload[1:-1] for part in parts if part.members]
    return b'{"claim":{' + b",".join(fields) + b"}}"
