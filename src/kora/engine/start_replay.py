import enum
import hashlib
import hmac
from dataclasses import dataclass
from typing import Never, Protocol, Self, runtime_checkable

from sqlalchemy import Executable, and_, case, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.cel_eval.errors import raise_safe
from kora.db.identifiers import is_identifier
from kora.db.tables import graph, graph_version, inbound_event
from kora.db.types import IDENTIFIER_LENGTH, NAME_LENGTH, POSTGRESQL, SQLITE
from kora.engine.locks import COMPONENT, TransitionLocks
from kora.engine.start_capacity import manual_start_scopes
from kora.engine.start_records import (
    ManualStartIdentity,
    PreparedManualStartRequest,
    StartOutcome,
    StartRecordRejected,
    decode_start_receipt,
)
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal

_MANUAL_START_CANDIDATE = object()

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
VERSION_RECHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.start_candidate.rechecked",
        COMPONENT,
        Level.DEBUG,
        "manual start version candidate rechecked",
        {"matches": FieldKind.BOOLEAN},
    )
)


START_WRITER_MODE = text(
    "SELECT pg_catalog.current_setting('transaction_isolation'::pg_catalog.text) "
    "OPERATOR(pg_catalog.=) 'read committed'::pg_catalog.text AND "
    "pg_catalog.current_setting('transaction_read_only'::pg_catalog.text) "
    "OPERATOR(pg_catalog.=) 'off'::pg_catalog.text"
)


@runtime_checkable
class _NativePostgresTransaction(Protocol):
    def is_in_transaction(self) -> bool: ...


class ReplayOutcome(enum.StrEnum):
    ABSENT = "absent"
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    REFUSED = "refused"


@dataclass(frozen=True)
class StartReplay:
    outcome: ReplayOutcome
    response: StartOutcome | None = None


class ManualStartCandidate:
    __slots__ = (
        "_outcome",
        "_response",
        "_request_digest",
        "_key_digest",
        "version_id",
    )
    _outcome: ReplayOutcome
    _response: StartOutcome | None
    _request_digest: str | None
    _key_digest: bytes | None
    version_id: str | None

    def __new__(
        cls,
        issuer: object,
        replay: StartReplay,
        version_id: str | None = None,
        request_digest: str | None = None,
        key_digest: bytes | None = None,
    ) -> Self:
        if issuer is not _MANUAL_START_CANDIDATE:
            del issuer, replay, version_id, request_digest, key_digest
            raise TypeError("manual start candidate requires a checked replay read")
        candidate = super().__new__(cls)
        response = None if replay.response is None else replay.response.model_copy()
        object.__setattr__(candidate, "_outcome", replay.outcome)
        object.__setattr__(candidate, "_response", response)
        object.__setattr__(candidate, "version_id", version_id)
        object.__setattr__(candidate, "_request_digest", request_digest)
        object.__setattr__(candidate, "_key_digest", key_digest)
        return candidate

    @property
    def replay(self) -> StartReplay:
        response = self._response
        return StartReplay(
            self._outcome, None if response is None else response.model_copy()
        )

    def is_new_for(self, request_digest: str, key: str) -> bool:
        if (
            self._outcome is not ReplayOutcome.ABSENT
            or self._response is not None
            or self._request_digest != request_digest
            or self._key_digest is None
        ):
            return False
        require_key(key)
        return hmac.compare_digest(self._key_digest, _receipt_key_digest(key))

    def __setattr__(self, name: str, value: object) -> Never:
        del self, value
        raise AttributeError("manual start candidate is immutable")

    def __delattr__(self, name: str) -> Never:
        del self
        raise AttributeError("manual start candidate is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("manual start candidate requires explicit access")


@dataclass(frozen=True, slots=True)
class ManualStartVersionRecheck:
    replay: StartReplay
    version_matches: bool


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


def _receipt_key_digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def _may_replay(principal: Principal, identity: ManualStartIdentity) -> bool:
    return principal.kind == "user" and any(
        grant.namespace_id == identity.namespace_id for grant in principal.namespaces
    )


def _checked_identity(identity: ManualStartIdentity, key: str) -> ManualStartIdentity:
    try:
        require_key(key)
        return ManualStartIdentity.model_validate(identity)
    except ValueError:
        del identity, key
    raise_safe(StartRecordRejected())


async def _read_receipt(
    connection: AsyncConnection,
    identity: ManualStartIdentity,
    key: str,
) -> StartReplay:
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
        raise_safe(StartRecordRejected())
    except SQLAlchemyError:
        raise_safe(StartReadFailed())
    finally:
        del key
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


async def replay_manual_start(
    connection: AsyncConnection,
    principal: Principal,
    identity: ManualStartIdentity,
    key: str,
) -> StartReplay:
    if not _may_replay(principal, identity):
        return replay_result(ReplayOutcome.REFUSED)
    try:
        return await _read_receipt(connection, _checked_identity(identity, key), key)
    finally:
        del principal, identity, key


async def _writable_postgres_transaction(connection: AsyncConnection) -> bool:
    if connection.dialect.driver != "asyncpg":
        return False
    try:
        raw = await connection.get_raw_connection()
        driver: object = raw.driver_connection
        if not isinstance(driver, _NativePostgresTransaction):
            return False
        if driver.is_in_transaction() is not True:
            return False
        return (await connection.scalar(START_WRITER_MODE)) is True
    except SQLAlchemyError:
        return False


async def recheck_locked_manual_start_receipt(
    connection: AsyncConnection,
    locks: TransitionLocks,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
    run_id: str,
) -> StartReplay:
    identity: ManualStartIdentity | None = None
    try:
        locks.require_connection(connection)
        if type(request) is not PreparedManualStartRequest:
            raise StartRecordRejected()
        identity = request.identity
        if not _may_replay(principal, identity):
            return replay_result(ReplayOutcome.REFUSED)
        if type(run_id) is not str or not is_identifier(run_id):
            raise StartRecordRejected()
        locks.require(
            run_id, manual_start_scopes(identity.namespace_id, identity.graph_id)
        )
        identity = _checked_identity(identity, key)
        if (
            connection.dialect.name == POSTGRESQL
            and not await _writable_postgres_transaction(connection)
        ):
            raise_safe(StartReadFailed())
        locks.require_connection(connection)
        result = await _read_receipt(connection, identity, key)
        locks.require_connection(connection)
        return result
    finally:
        del connection, locks, principal, request, key, run_id, identity


async def recheck_locked_manual_start_version(
    connection: AsyncConnection,
    locks: TransitionLocks,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
    run_id: str,
    expected_version_id: str,
) -> ManualStartVersionRecheck:
    selected: str | None = None
    try:
        replay = await recheck_locked_manual_start_receipt(
            connection, locks, principal, request, key, run_id
        )
        if replay.outcome is not ReplayOutcome.ABSENT:
            return ManualStartVersionRecheck(replay, False)
        if type(expected_version_id) is not str or not is_identifier(
            expected_version_id
        ):
            raise_safe(StartRecordRejected())
        locks.require_connection(connection)
        selected = await _read_candidate_version(
            connection, request.identity, request.graph_version_id
        )
        locks.require_connection(connection)
        matches = selected == expected_version_id
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(VERSION_RECHECKED, matches=matches)
        return ManualStartVersionRecheck(replay, matches)
    finally:
        del connection, locks, principal, request, key, run_id, expected_version_id
        del selected


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
    result = None
    row = None
    try:
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
        del candidate
        raise_safe(StartRecordRejected())
    finally:
        del connection, identity, version_id, statement, result, row


async def read_manual_start_candidate(
    connection: AsyncConnection,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
) -> ManualStartCandidate:
    if type(request) is not PreparedManualStartRequest:
        del request, key, principal
        raise_safe(StartRecordRejected())
    identity, version_id = request.identity, request.graph_version_id
    del request
    try:
        replay = await replay_manual_start(connection, principal, identity, key)
    except StartRecordRejected:
        del key, principal
        raise_safe(StartRecordRejected())
    except StartReadFailed:
        del key, principal
        raise_safe(StartReadFailed())
    if replay.outcome is not ReplayOutcome.ABSENT:
        return ManualStartCandidate(_MANUAL_START_CANDIDATE, replay)
    key_digest = _receipt_key_digest(key)
    del key, principal
    try:
        candidate = await _read_candidate_version(connection, identity, version_id)
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(
                CANDIDATE_CHECKED,
                outcome="missing" if candidate is None else "selected",
            )
        return ManualStartCandidate(
            _MANUAL_START_CANDIDATE,
            replay,
            candidate,
            identity.request_digest,
            key_digest,
        )
    finally:
        del key_digest
