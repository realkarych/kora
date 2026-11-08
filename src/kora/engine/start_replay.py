import enum
from dataclasses import dataclass

from sqlalchemy import Executable, and_, case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.cel_eval.errors import raise_safe
from kora.db.identifiers import is_identifier
from kora.db.tables import graph, graph_version, inbound_event
from kora.db.types import IDENTIFIER_LENGTH, NAME_LENGTH, SQLITE
from kora.engine.locks import COMPONENT
from kora.engine.start_records import (
    ManualStartIdentity,
    PreparedManualStartRequest,
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
CANDIDATE_CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.start_candidate.checked",
        COMPONENT,
        Level.DEBUG,
        "manual start version candidate checked",
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


@dataclass(frozen=True, slots=True)
class ManualStartCandidate:
    replay: StartReplay
    version_id: str | None = None


class StartReadFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the manual start metadata read failed")


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
    try:
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
    except (ValueError, RecursionError):
        del key
        raise_safe(StartRecordRejected())
    except SQLAlchemyError:
        del key
        raise_safe(StartReadFailed())
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


def _candidate_statement(
    connection: AsyncConnection,
    identity: ManualStartIdentity,
    version_id: str | None,
) -> Executable:
    fields = graph_version.c
    valid: ColumnElement[bool] = fields.number > 0
    if connection.dialect.name == SQLITE:
        valid = and_(
            func.typeof(fields.number) == "integer",
            valid,
            func.typeof(fields.id) == "text",
            func.octet_length(fields.id).in_(
                (IDENTIFIER_LENGTH, 2 * IDENTIFIER_LENGTH)
            ),
        )
    scoped_graph = (
        select(graph.c.id)
        .where(
            graph.c.id == identity.graph_id,
            graph.c.namespace_id == identity.namespace_id,
        )
        .scalar_subquery()
    )
    statement = select(case((valid, fields.id))).where(fields.graph_id == scoped_graph)
    if version_id is not None:
        return statement.where(fields.id == version_id).limit(1)
    return statement.order_by(fields.number.desc()).limit(1)


async def _read_candidate_version(
    connection: AsyncConnection,
    identity: ManualStartIdentity,
    version_id: str | None,
) -> str | None:
    statement = _candidate_statement(connection, identity, version_id)
    try:
        result = await connection.execute(statement)
        try:
            row = result.one_or_none()
        finally:
            result.close()
    except SQLAlchemyError:
        raise_safe(StartReadFailed())
    except UnicodeError:
        raise_safe(StartRecordRejected())
    if row is None:
        return None
    candidate: object = row[0]
    if type(candidate) is str and is_identifier(candidate):
        return candidate
    del row, candidate
    raise_safe(StartRecordRejected())


async def read_manual_start_candidate(
    connection: AsyncConnection,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
) -> ManualStartCandidate:
    identity, version_id = request.identity, request.graph_version_id
    del request
    replay = await replay_manual_start(connection, principal, identity, key)
    if replay.outcome is not ReplayOutcome.ABSENT:
        return ManualStartCandidate(replay)
    del key, principal
    candidate = await _read_candidate_version(connection, identity, version_id)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            CANDIDATE_CHECKED, outcome="missing" if candidate is None else "selected"
        )
    return ManualStartCandidate(replay, candidate)
