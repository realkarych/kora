from typing import Final, NoReturn

from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError

MESSAGES: Final[dict[str, str]] = {
    "storage_deadline": "the useful storage admission deadline has expired",
    "storage_path": "the private storage path is unsafe or unsupported",
    "storage_identity": "the physical storage identity or configuration does not match",
    "storage_busy": "storage ownership is busy; reconcile within the deadline",
    "storage_capacity": "the finite private storage capacity is unavailable",
    "storage_conflict": "the operation conflicts with its immutable storage binding",
    "storage_issuer_closed": "the receiver cannot admit a new storage effect",
    "storage_unavailable": "the requested immutable storage evidence is unavailable",
    "storage_integrity": "the stored bytes or immutable identity failed verification",
    "storage_io": "the storage operation could not settle; retain its recovery intent",
    "storage_closed": "the storage receiver is closing or closed",
    "storage_range": "the requested immutable range is invalid",
}
RETRYABLE: Final[frozenset[str]] = frozenset(
    {"storage_busy", "storage_capacity", "storage_io"}
)


def refuse(code: str) -> NoReturn:
    raise KoraProtocolError(code, MESSAGES[code], code in RETRYABLE, {}) from None


def require_deadline(ctx: ProtocolContext) -> None:
    if (
        ctx.deadline is not None
        and ctx.clock.now() >= ctx.deadline.at - ctx.deadline.clock_uncertainty
    ):
        refuse("storage_deadline")
