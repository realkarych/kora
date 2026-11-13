from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from kora.db.identifiers import is_identifier
from kora.db.run_queue_index_v1 import (
    ACTIVE_QUEUE_ROWS,
    POSTGRES_QUEUE_KEY,
    SQLITE_QUEUE_INDEX,
    SQLITE_QUEUE_KEY,
    SQLITE_QUEUE_ROWS,
)
from kora.db.transactions import WriterTransactionError, raise_private_metadata_error
from kora.db.types import POSTGRESQL
from kora.engine.locks import COMPONENT, LockError, Scope, ScopeKind, TransitionLocks
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.run_queue.checked",
        COMPONENT,
        Level.DEBUG,
        "queued run order checked",
        {"outcome": FieldKind.STRING},
    )
)


def _probe(table: str, rows: str, key: str) -> TextClause:
    scope = (
        f"FROM {table} WHERE namespace_id = :namespace_id AND graph_id = :graph_id "
        f"AND {rows}"
    )
    pending = f"{scope} AND status = 'pending'"
    return text(
        f"SELECT EXISTS(SELECT 1 {pending} AND ({key}) = ''), "
        f"EXISTS(SELECT 1 {scope} AND status = 'running'), "
        f"EXISTS(SELECT 1 {pending} AND ({key}) < :run_id)"
    )


SQLITE_QUEUE_PROBE = _probe(
    f"main.run INDEXED BY {SQLITE_QUEUE_INDEX}", SQLITE_QUEUE_ROWS, SQLITE_QUEUE_KEY
)
POSTGRES_QUEUE_PROBE = _probe("run", ACTIVE_QUEUE_ROWS, POSTGRES_QUEUE_KEY)


class QueueState(StrEnum):
    INVALID_ORDER = "invalid_order"
    RUNNING = "running"
    OLDER_PENDING = "older_pending"
    CLEAR = "clear"


class QueueCheckFailure(StrEnum):
    IDENTITY = "identity"
    OWNERSHIP = "ownership"
    LOCKS = "locks"
    MODE = "mode"
    READ = "read"


class QueueCheckFailed(RuntimeError):
    def __init__(self, reason: QueueCheckFailure) -> None:
        super().__init__(reason.value)
        self.reason: QueueCheckFailure = reason


async def _observe(
    connection: AsyncConnection,
    locks: TransitionLocks,
    namespace_id: str,
    graph_id: str,
    run_id: str,
) -> QueueState | QueueCheckFailure:
    if not all(
        type(value) is str and is_identifier(value)
        for value in (namespace_id, graph_id, run_id)
    ):
        return QueueCheckFailure.IDENTITY
    scopes = (
        Scope(ScopeKind.NAMESPACE, (namespace_id,)),
        Scope(ScopeKind.GRAPH, (graph_id,)),
    )
    try:
        locks.require_connection(connection)
    except (LockError, WriterTransactionError):
        return QueueCheckFailure.OWNERSHIP
    try:
        locks.require(run_id, scopes)
    except LockError:
        return QueueCheckFailure.LOCKS
    postgres = connection.dialect.name == POSTGRESQL
    try:
        if postgres and not await writable_postgres_transaction(connection):
            return QueueCheckFailure.MODE
        with await connection.execute(
            POSTGRES_QUEUE_PROBE if postgres else SQLITE_QUEUE_PROBE,
            {"namespace_id": namespace_id, "graph_id": graph_id, "run_id": run_id},
        ) as result:
            invalid, running, older = result.one()
        locks.require_connection(connection)
    except (LockError, WriterTransactionError):
        return QueueCheckFailure.OWNERSHIP
    except SQLAlchemyError:
        return QueueCheckFailure.READ
    values = (invalid, running, older)
    if postgres:
        valid_result = all(type(value) is bool for value in values)
    else:
        valid_result = all(type(value) is int and value in (0, 1) for value in values)
    if not valid_result:
        return QueueCheckFailure.READ
    if invalid:
        return QueueState.INVALID_ORDER
    if running:
        return QueueState.RUNNING
    if older:
        return QueueState.OLDER_PENDING
    return QueueState.CLEAR


async def observe_run_queue(
    connection: AsyncConnection,
    locks: TransitionLocks,
    namespace_id: str,
    graph_id: str,
    run_id: str,
) -> QueueState:
    state = await _observe(connection, locks, namespace_id, graph_id, run_id)
    if isinstance(state, QueueCheckFailure):
        del connection, locks, namespace_id, graph_id, run_id
        raise_private_metadata_error(QueueCheckFailed(state))
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(CHECKED, outcome=state.value)
    return state
