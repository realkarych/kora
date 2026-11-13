from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import event, insert, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import decode_claim_source
from kora.db.connection import (
    SNAPSHOT_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotRejected,
    require_snapshot_time,
)
from kora.db.grant_history import (
    GrantHistoryRejected,
    HistoryPosition,
    HistoryReadLimits,
    HistoryScope,
    RawGrant,
    read_history_rows,
    verify_history_scope,
)
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import attempt, attempt_dispatcher
from kora.db.types import SQLITE
from kora.engine.locks import COMPONENT, TransitionLocks
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.history import (
    GRANT_HISTORY_LABEL,
    MAX_PAGE_ROWS,
    GrantHistorySnapshot,
    chain_seed,
    chain_step,
    record_bytes,
)
from kora.protocols.identity import GrantBinding
from kora.protocols.identity_records import MAX_COUNT
from kora.protocols.worker_claim_system import GRANT_BYTES, PROCESS_BYTES

ACQUIRED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.grant_history.acquired",
        COMPONENT,
        Level.DEBUG,
        "grant history acquisition completed",
        {"acquired": FieldKind.BOOLEAN},
    )
)
PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.grant_history.prepared",
        COMPONENT,
        Level.DEBUG,
        "grant history changes prepared for commit",
        {"complete": FieldKind.BOOLEAN},
    )
)


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredHistory:
    scope: HistoryScope
    start: HistoryPosition | None
    rows: tuple[RawGrant, ...]
    encoding: str


@dataclass(frozen=True, slots=True, repr=False)
class PreparedHistory:
    scope: HistoryScope
    start: HistoryPosition | None
    grants: tuple[tuple[GrantBinding, HistoryPosition], ...]


def _emit(spec: EventSpec, name: str, value: bool) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(spec, **{name: value})


def _seed(scope: HistoryScope) -> str:
    return chain_seed(
        GRANT_HISTORY_LABEL,
        scope.namespace_id,
        scope.attempt_id,
        scope.execution_domain_id,
    )


def _snapshot(
    scope: HistoryScope, position: HistoryPosition | None
) -> GrantHistorySnapshot:
    return GrantHistorySnapshot(
        version=1,
        namespace_id=scope.namespace_id,
        attempt_id=scope.attempt_id,
        execution_domain_id=scope.execution_domain_id,
        upper_epoch=None if position is None else position.epoch,
        count=0 if position is None else position.count,
        digest=_seed(scope) if position is None else position.digest,
    )


async def _acquire(
    database: Database, scope: HistoryScope, limits: HistoryReadLimits, deadline: float
) -> AcquiredHistory:
    def before_statement(connection: Connection, *arguments: object) -> None:
        require_snapshot_time(deadline)

    limits.admit(MAX_PAGE_ROWS + 3, GRANT_BYTES, PROCESS_BYTES)
    async with database.read_snapshot(deadline=deadline) as connection:
        event.listen(
            connection.sync_connection, "before_cursor_execute", before_statement
        )
        try:
            if not await verify_history_scope(connection, scope, limits):
                raise GrantHistoryRejected()
            prior = await read_history_rows(
                connection,
                scope,
                limits,
                PROCESS_BYTES,
                maximum=1,
                verified=True,
                newest=True,
            )
            start = prior[0].prefix if prior else None
            rows = await read_history_rows(
                connection,
                scope,
                limits,
                PROCESS_BYTES,
                maximum=MAX_PAGE_ROWS + 1,
                after=None if start is None else start.epoch,
            )
            encoding = "UTF-8"
            if connection.dialect.name == SQLITE:
                value = await connection.scalar(text("PRAGMA encoding"))
                if value not in ("UTF-8", "UTF-16le", "UTF-16be"):
                    raise GrantHistoryRejected()
                encoding = cast(str, value)
            result = AcquiredHistory(scope, start, rows, encoding)
        finally:
            event.remove(
                connection.sync_connection, "before_cursor_execute", before_statement
            )
    return result


async def acquire_history(
    database: Database,
    scope: HistoryScope,
    limits: HistoryReadLimits,
    *,
    deadline: float,
) -> AcquiredHistory:
    acquired = False
    try:
        result = await _acquire(database, scope, limits, deadline)
        acquired = True
        return result
    except (ValueError, DatabaseSnapshotRejected, *SNAPSHOT_NATIVE_ERRORS):
        raise_safe(GrantHistoryRejected())
    finally:
        _emit(ACQUIRED, "acquired", acquired)


def _prepare(acquired: AcquiredHistory) -> PreparedHistory:
    prior = acquired.start
    digest = _seed(acquired.scope) if prior is None else prior.digest
    count = 0 if prior is None else prior.count
    epoch = -1 if prior is None else prior.epoch
    grants: list[tuple[GrantBinding, HistoryPosition]] = []
    for raw in acquired.rows[:MAX_PAGE_ROWS]:
        if raw.epoch <= epoch or count == MAX_COUNT:
            raise GrantHistoryRejected()
        grant = GrantBinding.model_validate(
            {
                "dispatcher_id": raw.dispatcher_id,
                "worker_id": raw.worker_id,
                "incarnation_id": raw.incarnation_id,
                "lease_epoch": raw.epoch,
                "execution_domain_id": raw.execution_domain_id,
                "process": None
                if raw.process is None
                else decode_claim_source(raw.process, acquired.encoding),
            }
        )
        if grant.execution_domain_id != acquired.scope.execution_domain_id:
            raise GrantHistoryRejected()
        digest = chain_step(digest, record_bytes(grant))
        count += 1
        epoch = grant.lease_epoch
        grants.append((grant, HistoryPosition(epoch, count, digest)))
    return PreparedHistory(acquired.scope, acquired.start, tuple(grants))


def prepare_history(acquired: AcquiredHistory) -> PreparedHistory:
    try:
        return _prepare(acquired)
    except ValueError:
        del acquired
        raise_safe(GrantHistoryRejected())


async def _require_owner(
    connection: AsyncConnection,
    locks: TransitionLocks,
    scope: HistoryScope,
    limits: HistoryReadLimits,
) -> None:
    locks.require_connection(connection)
    locks.require(scope.run_id)
    limits.admit(MAX_PAGE_ROWS + 3, GRANT_BYTES, PROCESS_BYTES)
    if not await verify_history_scope(connection, scope, limits, writing=True):
        raise GrantHistoryRejected()


async def _tip(
    connection: AsyncConnection,
    scope: HistoryScope,
    snapshot: GrantHistorySnapshot | None,
    *,
    reset: bool,
) -> None:
    values: dict[str, object] = dict(
        zip(
            TIP_FIELDS,
            (None, None, None)
            if snapshot is None
            else (snapshot.upper_epoch, snapshot.count, snapshot.digest),
            strict=True,
        )
    )
    if reset:
        values.update({name: None for name in ACK_FIELDS})
    await connection.execute(
        update(attempt).where(attempt.c.id == scope.attempt_id).values(**values)
    )


async def _save_history_batch(
    connection: AsyncConnection,
    locks: TransitionLocks,
    prepared: PreparedHistory,
    limits: HistoryReadLimits,
) -> GrantHistorySnapshot | None:
    scope = prepared.scope
    await _require_owner(connection, locks, scope, limits)
    prior = await read_history_rows(
        connection, scope, limits, PROCESS_BYTES, maximum=1, verified=True, newest=True
    )
    if (prior[0].prefix if prior else None) != prepared.start:
        return None
    if prepared.grants:
        current = await read_history_rows(
            connection,
            scope,
            limits,
            PROCESS_BYTES,
            maximum=len(prepared.grants),
            after=None if prepared.start is None else prepared.start.epoch,
            reserve_prefix=True,
        )
        if tuple((row.dispatcher_id, row.epoch) for row in current) != tuple(
            (grant.dispatcher_id, grant.lease_epoch) for grant, _ in prepared.grants
        ):
            raise GrantHistoryRejected()
    for grant, position in prepared.grants:
        result = await connection.execute(
            update(attempt_dispatcher)
            .where(
                attempt_dispatcher.c.id == grant.dispatcher_id,
                attempt_dispatcher.c.attempt_id == scope.attempt_id,
                attempt_dispatcher.c.lease_epoch == grant.lease_epoch,
                attempt_dispatcher.c.prefix_count.is_(None),
                attempt_dispatcher.c.prefix_digest.is_(None),
            )
            .values(prefix_count=position.count, prefix_digest=position.digest)
        )
        if result.rowcount != 1:
            raise GrantHistoryRejected()
    position = prepared.grants[-1][1] if prepared.grants else prepared.start
    latest = await read_history_rows(
        connection, scope, limits, PROCESS_BYTES, maximum=1, newest=True
    )
    complete = (not latest and position is None) or (
        bool(latest)
        and position is not None
        and latest[0].epoch == position.epoch
        and latest[0].prefix == position
    )
    snapshot = _snapshot(scope, position) if complete else None
    await _tip(connection, scope, snapshot, reset=bool(prepared.grants) or not complete)
    _emit(PREPARED, "complete", complete)
    return snapshot


async def _append_grant(
    connection: AsyncConnection,
    locks: TransitionLocks,
    scope: HistoryScope,
    grant: GrantBinding,
    mode: Literal["execute", "recover"],
    now: datetime,
    limits: HistoryReadLimits,
) -> GrantHistorySnapshot | None:
    await _require_owner(connection, locks, scope, limits)
    encoded = record_bytes(grant)
    if (
        grant.execution_domain_id != scope.execution_domain_id
        or 2 * len(encoded) + 1024 > limits.dispatcher_row
    ):
        raise GrantHistoryRejected()
    latest = await read_history_rows(
        connection, scope, limits, PROCESS_BYTES, maximum=1, newest=True
    )
    if latest and grant.lease_epoch <= latest[0].epoch:
        raise GrantHistoryRejected()
    previous = latest[0].prefix if latest else None
    complete = not latest or previous is not None
    position: HistoryPosition | None = None
    if complete:
        count = 0 if previous is None else previous.count
        if count == MAX_COUNT:
            raise GrantHistoryRejected()
        digest = _seed(scope) if previous is None else previous.digest
        position = HistoryPosition(
            grant.lease_epoch, count + 1, chain_step(digest, encoded)
        )
    await connection.execute(
        insert(attempt_dispatcher).values(
            id=grant.dispatcher_id,
            attempt_id=scope.attempt_id,
            worker_id=grant.worker_id,
            incarnation_id=grant.incarnation_id,
            execution_domain_id=grant.execution_domain_id,
            lease_epoch=grant.lease_epoch,
            process=None
            if grant.process is None
            else grant.process.model_dump(mode="json"),
            mode=mode,
            granted_at=now,
            created_at=now,
            updated_at=now,
            prefix_count=None if position is None else position.count,
            prefix_digest=None if position is None else position.digest,
        )
    )
    snapshot = _snapshot(scope, position) if position is not None else None
    await _tip(connection, scope, snapshot, reset=True)
    _emit(PREPARED, "complete", complete)
    return snapshot


async def save_history_batch(
    connection: AsyncConnection,
    locks: TransitionLocks,
    prepared: PreparedHistory,
    limits: HistoryReadLimits,
) -> GrantHistorySnapshot | None:
    try:
        return await _save_history_batch(connection, locks, prepared, limits)
    except (SQLAlchemyError, OSError, ValueError):
        del prepared
        raise_safe(GrantHistoryRejected())


async def append_grant(
    connection: AsyncConnection,
    locks: TransitionLocks,
    scope: HistoryScope,
    grant: GrantBinding,
    mode: Literal["execute", "recover"],
    now: datetime,
    limits: HistoryReadLimits,
) -> GrantHistorySnapshot | None:
    try:
        return await _append_grant(connection, locks, scope, grant, mode, now, limits)
    except (SQLAlchemyError, OSError, ValueError):
        del grant
        raise_safe(GrantHistoryRejected())
