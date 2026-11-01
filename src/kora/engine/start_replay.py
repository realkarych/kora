import enum
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import inbound_event
from kora.db.types import NAME_LENGTH
from kora.engine.locks import COMPONENT
from kora.engine.start_records import (
    ManualStartIdentity,
    StartOutcome,
    StartRecordRejected,
    decode_start_receipt,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal

CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.start_replay.checked",
        COMPONENT,
        Level.DEBUG,
        "manual start replay checked",
        {"outcome": FieldKind.STRING},
    )
)


class ReplayOutcome(enum.StrEnum):
    ABSENT = "absent"
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    REFUSED = "refused"


@dataclass(frozen=True)
class StartReplay:
    outcome: ReplayOutcome
    response: StartOutcome | None = None


def replay_result(
    outcome: ReplayOutcome, response: StartOutcome | None = None
) -> StartReplay:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(CHECKED, outcome=outcome.value)
    return StartReplay(outcome, response)


def require_key(key: str) -> None:
    if (
        type(key) is not str
        or not 1 <= len(key) <= NAME_LENGTH
        or any(char == "\u0000" or 0xD800 <= ord(char) <= 0xDFFF for char in key)
    ):
        raise StartRecordRejected()


async def replay_manual_start(
    connection: AsyncConnection,
    principal: Principal,
    identity: ManualStartIdentity,
    key: str,
) -> StartReplay:
    if principal.kind != "user" or not any(
        grant.namespace_id == identity.namespace_id for grant in principal.namespaces
    ):
        return replay_result(ReplayOutcome.REFUSED)
    require_key(key)
    try:
        identity = ManualStartIdentity.model_validate(identity)
    except ValueError:
        raise StartRecordRejected() from None
    result = await connection.execute(
        select(inbound_event.c.start_outcome).where(
            inbound_event.c.namespace_id == identity.namespace_id,
            inbound_event.c.name == f"kora.start:{identity.graph_id}",
            inbound_event.c.idempotency_key == key,
        )
    )
    try:
        row = result.one_or_none()
    finally:
        result.close()
    if row is None:
        return replay_result(ReplayOutcome.ABSENT)
    receipt = decode_start_receipt(row[0])
    if (receipt.namespace_id, receipt.graph_id) != (
        identity.namespace_id,
        identity.graph_id,
    ):
        raise StartRecordRejected()
    if receipt.request_digest != identity.request_digest:
        return replay_result(ReplayOutcome.MISMATCHED)
    return replay_result(ReplayOutcome.MATCHED, receipt.response)
