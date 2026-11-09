import asyncio
import enum
import hashlib
import json
import math
import re
from dataclasses import dataclass, replace

from sqlalchemy import (
    Boolean,
    Integer,
    String,
    and_,
    bindparam,
    case,
    column,
    func,
    literal,
    literal_column,
    select,
    text,
    union_all,
)
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    GraphPreparationRejected,
    execute_graph_preparation_settlement,
    graph_preparation_parameters,
    graph_preparation_settlement_statement,
    require_graph_preparation_binding,
)
from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate
from kora.db.graph_publication_digest_v1 import (
    SQLITE_INVALID,
    SQLITE_VALID,
)
from kora.db.graph_publication_ordinal_v1 import (
    ORDINAL_INVALID_INDEX,
    ORDINAL_VALID_INDEX,
    SQLITE_ORDINAL_INVALID,
    SQLITE_ORDINAL_VALID,
)
from kora.db.identifiers import IDENTIFIER_LENGTH, is_identifier
from kora.db.tables import (
    graph,
    graph_deployment_preparation_capacity,
    storage_address_fence,
)
from kora.db.transactions import raise_private_metadata_error, writer_transaction
from kora.db.types import POSTGRESQL, SQLITE
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.identity_records import MAX_COUNT

COMPONENT: str = CATALOG.register_component("engine")
ACQUIRED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.locks.acquired",
        COMPONENT,
        Level.DEBUG,
        "transaction locks acquired",
        {"run_locked": FieldKind.BOOLEAN, "scope_keys": FieldKind.INTEGER},
    )
)
CONTENDED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.locks.contended",
        COMPONENT,
        Level.DEBUG,
        "candidate run lock is contended",
        {},
    )
)
RUN_CLASS: int = 1
STORE_ID_PATTERN: re.Pattern[str] = re.compile(r"[!-~]{1,256}")
STORAGE_DIGEST_PATTERN: re.Pattern[str] = re.compile(r"sha256:[0-9a-f]{64}")
SCOPE_TIMEOUT_SETTING = text(
    "SELECT setting::pg_catalog.int4, unit, vartype, max_val::pg_catalog.int4 "
    "FROM pg_catalog.pg_settings WHERE name = 'statement_timeout'"
)
SET_SCOPE_TIMEOUT = text(
    "SELECT pg_catalog.set_config('statement_timeout', :milliseconds, true)"
).bindparams(bindparam("milliseconds", type_=String()))
TIMED_SCOPE_LOCK = text(
    "SELECT pg_catalog.pg_advisory_xact_lock(:kind, :identity)"
).bindparams(
    bindparam("kind", type_=Integer()),
    bindparam("identity", type_=Integer()),
)
DIGEST_MATCH_KEY: ColumnElement[bool] = and_(
    column("graph_id", String()) == bindparam("graph_id"),
    column("deployment_digest", String()) == bindparam("deployment_digest"),
)
VERSION_ID = column("id", String())
SQLITE_DIGEST_MATCH = union_all(
    select(literal_column("NULL", String()))
    .select_from(
        text("graph_version INDEXED BY ix_graph_version_deployment_digest_invalid_id")
    )
    .where(DIGEST_MATCH_KEY, literal_column(SQLITE_INVALID)),
    select(VERSION_ID)
    .select_from(text("graph_version INDEXED BY ix_graph_version_deployment_digest"))
    .where(DIGEST_MATCH_KEY, literal_column(SQLITE_VALID)),
).limit(2)
POSTGRESQL_VERSION_ID: str = ascii_predicate(
    POSTGRESQL, "id", ULID_ALPHABET, f"= {IDENTIFIER_LENGTH}", "0-7"
)
POSTGRESQL_DIGEST_MATCH = (
    select(case((literal_column(POSTGRESQL_VERSION_ID, Boolean()), VERSION_ID)))
    .select_from(text("graph_version"))
    .where(DIGEST_MATCH_KEY)
    .limit(2)
)


ORDINAL_GRAPH_KEY = column("graph_id", String()) == bindparam("graph_id")
ORDINAL_NUMBER = column("number", Integer())
SQLITE_INVALID_ORDINAL = (
    select(literal_column("1", Integer()))
    .select_from(text(f"main.graph_version INDEXED BY {ORDINAL_INVALID_INDEX}"))
    .where(ORDINAL_GRAPH_KEY, literal_column(SQLITE_ORDINAL_INVALID))
    .limit(1)
)
SQLITE_HIGHEST_ORDINAL = (
    select(ORDINAL_NUMBER)
    .select_from(text(f"main.graph_version INDEXED BY {ORDINAL_VALID_INDEX}"))
    .where(ORDINAL_GRAPH_KEY, literal_column(SQLITE_ORDINAL_VALID))
    .order_by(ORDINAL_NUMBER.desc())
    .limit(1)
)
SQLITE_ORDINAL_OBSERVATION = select(
    case(
        (SQLITE_INVALID_ORDINAL.exists(), literal_column("'refused'", String())),
        else_=SQLITE_HIGHEST_ORDINAL.scalar_subquery(),
    )
)
POSTGRESQL_ORDINAL_OBSERVATION = text(
    "SELECT "
    "(SELECT number FROM graph_version WHERE graph_id = :graph_id "
    "ORDER BY number ASC LIMIT 1), "
    "(SELECT number FROM graph_version WHERE graph_id = :graph_id "
    "ORDER BY number DESC LIMIT 1)"
)


class LockError(RuntimeError):
    pass


class ScopeDeadlineExpired(LockError):
    def __init__(self) -> None:
        super().__init__("the scope lock deadline expired")


class TimedScopeLockFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the timed scope lock could not be acquired")


class StorageAddressMissing(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence is missing")


class StorageAddressContended(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence is contended")


class StorageAddressLockFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence lock failed")


class StorageAddressStageUnavailable(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence is unavailable for staging")


class StorageAddressStageReadFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address staging check failed")


class StorageAddressPublicationFenceUnavailable(LockError):
    def __init__(self) -> None:
        super().__init__("the graph publication stage fence could not be verified")


class StorageAddressPublicationFenceReadFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the graph publication stage fence read failed")


class GraphPreparationLockRejected(LockError):
    def __init__(self) -> None:
        super().__init__("the open graph preparation lock was rejected")


class GraphPreparationLockContended(LockError):
    def __init__(self) -> None:
        super().__init__("the open graph preparation lock is contended")


class GraphPreparationLockFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the open graph preparation lock failed")


class PreparationCapacityUnavailable(LockError):
    def __init__(self) -> None:
        super().__init__("the graph preparation capacity count is unavailable")


class PreparationCapacityReadFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the graph preparation capacity read failed")


class GraphPublicationTargetUnavailable(LockError):
    def __init__(self) -> None:
        super().__init__("the graph publication target is unavailable")


class GraphPublicationTargetReadFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the graph publication target read failed")


class GraphPublicationDigestUnavailable(LockError):
    def __init__(self) -> None:
        super().__init__("the graph deployment digest observation is unavailable")


class GraphPublicationDigestReadFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the graph deployment digest observation failed")


class GraphPublicationOrdinalUnavailable(LockError):
    def __init__(self) -> None:
        super().__init__("the graph version number observation is unavailable")


class GraphPublicationOrdinalReadFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the graph version number observation failed")


def _postgres_lock_contended(dialect: str, error: SQLAlchemyError) -> bool:
    return (
        dialect == POSTGRESQL
        and isinstance(error, DBAPIError)
        and getattr(error.orig, "sqlstate", None) == "55P03"
    )


def _storage_key(store_id: str, payload_digest: str) -> tuple[str, str]:
    if (
        type(store_id) is not str
        or STORE_ID_PATTERN.fullmatch(store_id) is None
        or type(payload_digest) is not str
        or STORAGE_DIGEST_PATTERN.fullmatch(payload_digest) is None
    ):
        raise_private_metadata_error(LockError("invalid storage address lock request"))
    return store_id, payload_digest


def _bounded_storage_keys(
    addresses: tuple[tuple[str, str], ...], max_addresses: int
) -> frozenset[tuple[str, str]]:
    if (
        type(max_addresses) is not int
        or max_addresses < 0
        or type(addresses) is not tuple
        or len(addresses) > max_addresses
    ):
        raise_private_metadata_error(LockError("invalid storage address set"))
    keys: set[tuple[str, str]] = set()
    for address in addresses:
        if type(address) is not tuple or len(address) != 2:
            raise_private_metadata_error(LockError("invalid storage address set"))
        keys.add(_storage_key(*address))
    return frozenset(keys)


async def _lock_existing_address(
    connection: AsyncConnection,
    dialect: str,
    address: tuple[str, str],
    *,
    guard_table: bool,
) -> bool:
    store_id, payload_digest = address
    fields = storage_address_fence.c
    statement = (
        select(literal(1))
        .select_from(storage_address_fence)
        .where(
            fields.store_id == bindparam("store_id"),
            fields.payload_digest == bindparam("payload_digest"),
        )
        .limit(2)
    )
    if dialect == POSTGRESQL:
        statement = statement.with_for_update(nowait=True)
    try:
        if dialect == POSTGRESQL and guard_table:
            table_lock = await connection.execute(
                text("LOCK TABLE storage_address_fence IN ROW SHARE MODE NOWAIT")
            )
            table_lock.close()
        result = await connection.execute(
            statement, {"store_id": store_id, "payload_digest": payload_digest}
        )
        try:
            return result.scalar_one_or_none() == 1
        finally:
            result.close()
    except SQLAlchemyError as error:
        failure = (
            StorageAddressContended()
            if _postgres_lock_contended(dialect, error)
            else StorageAddressLockFailed()
        )
    raise_private_metadata_error(failure)


async def _lock_open_preparation(
    connection: AsyncConnection, dialect: str, binding: GraphPreparationBinding
) -> GraphPreparationBinding:
    failure: LockError
    try:
        if type(binding) is not GraphPreparationBinding:
            raise GraphPreparationRejected()
        snapshot = replace(binding)
        parameters = graph_preparation_parameters(snapshot)
        statement = graph_preparation_settlement_statement(dialect)
        if dialect == POSTGRESQL:
            table_lock = await connection.execute(
                text("LOCK TABLE graph_deployment_preparation IN ROW SHARE MODE NOWAIT")
            )
            table_lock.close()
            statement = statement.with_for_update(nowait=True)
        settlement = await execute_graph_preparation_settlement(
            connection, statement, parameters
        )
        if settlement is not None and settlement.state == "open":
            return snapshot
        failure = GraphPreparationLockRejected()
    except GraphPreparationRejected:
        failure = GraphPreparationLockRejected()
    except SQLAlchemyError as error:
        failure = (
            GraphPreparationLockContended()
            if _postgres_lock_contended(dialect, error)
            else GraphPreparationLockFailed()
        )
    raise_private_metadata_error(failure)


async def _read_storage_fence_incarnation(
    connection: AsyncConnection,
    dialect: str,
    store_id: str,
    payload_digest: str,
    *,
    max_last_generation: int,
    read_failed: type[LockError],
) -> tuple[int | None] | None:
    fields = storage_address_fence.c
    eligible = and_(
        fields.fence_incarnation > 0,
        fields.fence_incarnation <= MAX_COUNT,
        fields.last_deletion_generation >= 0,
        fields.last_deletion_generation <= max_last_generation,
        fields.pending_deletion_generation.is_(None),
    )
    if dialect == SQLITE:
        eligible = and_(
            func.typeof(fields.fence_incarnation) == "integer",
            func.typeof(fields.last_deletion_generation) == "integer",
            eligible,
        )
    statement = select(case((eligible, fields.fence_incarnation), else_=None)).where(
        fields.store_id == bindparam("store_id"),
        fields.payload_digest == bindparam("payload_digest"),
    )
    try:
        result = await connection.execute(
            statement, {"store_id": store_id, "payload_digest": payload_digest}
        )
        row = result.one_or_none()
    except SQLAlchemyError:
        pass
    else:
        if row is None:
            return None
        incarnation: object = row[0]
        if incarnation is None or (
            type(incarnation) is int and 0 < incarnation <= MAX_COUNT
        ):
            return (incarnation,)
    raise_private_metadata_error(read_failed())


async def _read_preparation_capacity(connection: AsyncConnection, dialect: str) -> int:
    fields = graph_deployment_preparation_capacity.c
    valid = and_(fields.occupied >= 0, fields.occupied <= MAX_COUNT)
    if dialect == SQLITE:
        valid = and_(
            func.typeof(fields.id) == "integer",
            func.typeof(fields.occupied) == "integer",
            valid,
        )
    statement = (
        select(case((valid, fields.occupied), else_=None))
        .where(fields.id == 1)
        .limit(2)
    )
    try:
        occupied: object = (await connection.execute(statement)).scalar_one_or_none()
    except SQLAlchemyError:
        pass
    else:
        if type(occupied) is int and 0 <= occupied <= MAX_COUNT:
            return occupied
        raise_private_metadata_error(PreparationCapacityUnavailable())
    raise_private_metadata_error(PreparationCapacityReadFailed())


async def _read_graph_publication_target(
    connection: AsyncConnection, dialect: str, binding: GraphPreparationBinding
) -> str | None:
    fields = graph.c
    selected: ColumnElement[str] = fields.id
    if dialect == SQLITE:
        valid = and_(
            func.typeof(fields.id) == "text",
            func.octet_length(fields.id).in_(
                (IDENTIFIER_LENGTH, 2 * IDENTIFIER_LENGTH)
            ),
        )
        selected = case((valid, fields.id))
    statement = (
        select(selected)
        .where(
            fields.namespace_id == binding.namespace_id,
            fields.name == binding.graph_name,
        )
        .limit(2)
    )
    try:
        row = (await connection.execute(statement)).one_or_none()
    except (SQLAlchemyError, UnicodeError):
        pass
    else:
        if row is None:
            return None
        graph_id: object = row[0]
        if type(graph_id) is str and is_identifier(graph_id):
            return graph_id
        raise_private_metadata_error(GraphPublicationTargetUnavailable())
    raise_private_metadata_error(GraphPublicationTargetReadFailed())


async def _read_graph_publication_digest_match(
    connection: AsyncConnection, dialect: str, graph_id: str, deployment_digest: str
) -> str | None:
    statement = SQLITE_DIGEST_MATCH if dialect == SQLITE else POSTGRESQL_DIGEST_MATCH
    try:
        result = await connection.execute(
            statement, {"graph_id": graph_id, "deployment_digest": deployment_digest}
        )
        try:
            rows = result.all()
        finally:
            result.close()
    except (SQLAlchemyError, UnicodeError):
        pass
    else:
        if not rows:
            return None
        version_id: object = rows[0][0]
        if len(rows) == 1 and type(version_id) is str and is_identifier(version_id):
            return version_id
        raise_private_metadata_error(GraphPublicationDigestUnavailable())
    raise_private_metadata_error(GraphPublicationDigestReadFailed())


async def _read_graph_publication_ordinal(
    connection: AsyncConnection, dialect: str, graph_id: str
) -> int | None:
    statement = (
        SQLITE_ORDINAL_OBSERVATION
        if dialect == SQLITE
        else POSTGRESQL_ORDINAL_OBSERVATION
    )
    try:
        result = await connection.execute(statement, {"graph_id": graph_id})
        try:
            row = result.one()
        finally:
            result.close()
    except (SQLAlchemyError, UnicodeError):
        pass
    else:
        lowest: object = row[0]
        highest: object = lowest if dialect == SQLITE else row[1]
        if lowest is None and highest is None:
            return None
        if type(lowest) is int and type(highest) is int and 0 < lowest <= highest:
            return highest
        del row, lowest, highest
        raise_private_metadata_error(GraphPublicationOrdinalUnavailable())
    raise_private_metadata_error(GraphPublicationOrdinalReadFailed())


class ScopeKind(enum.IntEnum):
    PREPARATION_CAPACITY = 0
    GLOBAL = 2
    NAMESPACE = 3
    GRAPH = 4
    RUNTIME = 5
    FAN_OUT = 6
    TARGET = 7


def _scope_remaining_seconds(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise_private_metadata_error(ScopeDeadlineExpired())
    return remaining


async def _read_scope_timeout(
    connection: AsyncConnection, deadline: float
) -> tuple[int, int]:
    _scope_remaining_seconds(deadline)
    row: tuple[object, ...] = tuple(
        (await connection.execute(SCOPE_TIMEOUT_SETTING)).one()
    )
    current, unit, value_kind, maximum = row
    if (
        type(current) is not int
        or type(maximum) is not int
        or not 0 <= current <= maximum
        or maximum <= 0
        or unit != "ms"
        or value_kind != "integer"
    ):
        raise_private_metadata_error(TimedScopeLockFailed())
    return current, maximum


async def _set_scope_timeout(
    connection: AsyncConnection, deadline: float, milliseconds: int
) -> None:
    _scope_remaining_seconds(deadline)
    result: object = await connection.scalar(
        SET_SCOPE_TIMEOUT, {"milliseconds": str(milliseconds)}
    )
    if type(result) is not str:
        raise_private_metadata_error(TimedScopeLockFailed())


async def _acquire_postgresql_scopes_before(
    connection: AsyncConnection, keys: list[tuple[int, int]], deadline: float
) -> None:
    timer = asyncio.timeout_at(deadline)
    try:
        async with timer:
            original, maximum = await _read_scope_timeout(connection, deadline)
            cap = original or maximum
            for kind, identity in keys:
                remaining = _scope_remaining_seconds(deadline)
                milliseconds = (
                    math.floor(remaining * 1000) if remaining < cap / 1000 else cap
                )
                if milliseconds <= 0:
                    raise_private_metadata_error(ScopeDeadlineExpired())
                await _set_scope_timeout(connection, deadline, milliseconds)
                _scope_remaining_seconds(deadline)
                result = await connection.execute(
                    TIMED_SCOPE_LOCK, {"kind": kind, "identity": identity}
                )
                result.close()
            await _set_scope_timeout(connection, deadline, original)
            if await _read_scope_timeout(connection, deadline) != (original, maximum):
                raise_private_metadata_error(TimedScopeLockFailed())
    except TimeoutError:
        failure = ScopeDeadlineExpired() if timer.expired() else TimedScopeLockFailed()
    except (OSError, SQLAlchemyError):
        failure = TimedScopeLockFailed()
    else:
        return
    raise_private_metadata_error(failure)


def identity_hash(parts: tuple[str, ...]) -> int:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(encoded).digest()[:4], "big", signed=True)


@dataclass(frozen=True)
class Scope:
    kind: ScopeKind
    identity: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not ScopeKind or type(self.identity) is not tuple:
            raise LockError("invalid lock scope")
        sizes = {
            ScopeKind.PREPARATION_CAPACITY: 0,
            ScopeKind.GLOBAL: 0,
            ScopeKind.FAN_OUT: 3,
            ScopeKind.TARGET: 2,
        }
        if len(self.identity) != sizes.get(self.kind, 1) or any(
            type(part) is not str or not part for part in self.identity
        ):
            raise LockError("invalid lock scope")

    @property
    def key(self) -> tuple[int, int]:
        return (int(self.kind), identity_hash(self.identity))


def record_acquired(run_locked: bool, scope_keys: int) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            ACQUIRED, run_locked=run_locked, scope_keys=scope_keys
        )


@dataclass(frozen=True, repr=False)
class _GraphPreparationGuard:
    binding: GraphPreparationBinding
    selected_graph_id: str | None


class TransitionLocks:
    def __init__(self, connection: AsyncConnection) -> None:
        self._lease = writer_transaction(connection)
        self._lease.claim_lock_context()
        self._run_id: str | None = None
        self._scopes: frozenset[Scope] | None = None
        self._storage_addresses: frozenset[tuple[str, str]] | None = None
        self._graph_preparation: _GraphPreparationGuard | None = None
        self._observed_graph_target: _GraphPreparationGuard | None = None
        self._usable: bool = True
        self._dialect: str = connection.dialect.name
        if self._dialect not in (POSTGRESQL, SQLITE):
            self._usable = False
            raise LockError("unsupported lock dialect")

    def _require_current(self) -> None:
        self._lease.require_active()
        if not self._usable:
            raise LockError("the lock context cannot be used")

    async def acquire_run(self, run_id: str, *, wait: bool = True) -> bool:
        self._require_current()
        if not is_identifier(run_id) or type(wait) is not bool:
            raise LockError("invalid run lock request")
        if self._run_id == run_id:
            return True
        if self._run_id is not None or self._scopes is not None:
            raise LockError("the run lock must be acquired first and once")
        self._usable = False
        acquired = True
        if self._dialect == POSTGRESQL:
            statement = (
                "SELECT pg_advisory_xact_lock(:kind, :identity)"
                if wait
                else "SELECT pg_try_advisory_xact_lock(:kind, :identity)"
            )
            result = await self._lease.connection.execute(
                text(statement),
                {"kind": RUN_CLASS, "identity": identity_hash((run_id,))},
            )
            try:
                acquired = True if wait else result.scalar_one() is True
            finally:
                result.close()
        self._lease.require_active()
        if not acquired:
            diagnostics = current_diagnostics()
            if diagnostics is not None:
                diagnostics.logger(COMPONENT).emit(CONTENDED)
            return False
        self._run_id = run_id
        self._usable = True
        record_acquired(True, 0)
        return True

    async def acquire_scopes(
        self, scopes: tuple[Scope, ...], *, deadline: float | None = None
    ) -> None:
        self._require_current()
        if self._scopes is not None:
            raise LockError("the scope set is acquired once")
        if deadline is not None:
            self._usable = False
            if type(deadline) is not float or not math.isfinite(deadline):
                raise_private_metadata_error(TimedScopeLockFailed())
            _scope_remaining_seconds(deadline)
        selected = frozenset(scopes)
        keys = sorted({scope.key for scope in selected})
        self._usable = False
        if deadline is not None:
            if self._dialect == POSTGRESQL and keys:
                await _acquire_postgresql_scopes_before(
                    self._lease.connection, keys, deadline
                )
        elif self._dialect == POSTGRESQL:
            for kind, identity in keys:
                result = await self._lease.connection.execute(
                    text("SELECT pg_advisory_xact_lock(:kind, :identity)"),
                    {"kind": kind, "identity": identity},
                )
                result.close()
        self._lease.require_active()
        if deadline is not None:
            _scope_remaining_seconds(deadline)
        self._scopes = selected
        self._usable = True
        record_acquired(self._run_id is not None, len(keys))

    def require(self, run_id: str | None, scopes: tuple[Scope, ...] = ()) -> None:
        self._require_current()
        if self._run_id != run_id or not frozenset(scopes).issubset(self._scopes or ()):
            raise LockError("the required transaction locks are not held")

    def require_connection(self, connection: AsyncConnection) -> None:
        self._require_current()
        if self._lease.connection is not connection:
            raise LockError("the write connection does not own the locks")

    async def read_preparation_capacity(self, connection: AsyncConnection) -> int:
        self.require_connection(connection)
        self.require(self._run_id, (Scope(ScopeKind.PREPARATION_CAPACITY),))
        self._usable = False
        occupied = await _read_preparation_capacity(connection, self._dialect)
        self._lease.require_active()
        self._usable = True
        return occupied

    def _require_selected_graph_scopes(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> None:
        self.require_connection(connection)
        require_graph_preparation_binding(binding)
        if type(selected_graph_id) is not str or not is_identifier(selected_graph_id):
            raise LockError("invalid selected graph identity")
        if (
            binding.initial_graph_id is not None
            and binding.initial_graph_id != selected_graph_id
        ):
            raise LockError("the selected graph differs from the bound graph")
        self.require(
            None,
            (
                Scope(ScopeKind.NAMESPACE, (binding.namespace_id,)),
                Scope(ScopeKind.GRAPH, (selected_graph_id,)),
            ),
        )

    def require_graph_publication_scope(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> None:
        self._require_selected_graph_scopes(connection, binding, selected_graph_id)
        if self._storage_addresses is not None:
            raise LockError("the graph publication target precedes storage addresses")

    async def read_graph_publication_target(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> str | None:
        self.require_graph_publication_scope(connection, binding, selected_graph_id)
        self._usable = False
        self._observed_graph_target = None
        snapshot = replace(binding)
        graph_id = await _read_graph_publication_target(
            connection, self._dialect, snapshot
        )
        self._lease.require_active()
        if graph_id == selected_graph_id:
            self._observed_graph_target = _GraphPreparationGuard(
                snapshot, selected_graph_id
            )
        self._usable = True
        return graph_id

    def _require_observed_publication_target(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> None:
        self.require_graph_publication_scope(connection, binding, selected_graph_id)
        if self._observed_graph_target != _GraphPreparationGuard(
            binding, selected_graph_id
        ):
            raise_private_metadata_error(
                LockError("the exact graph publication target was not observed")
            )

    async def read_graph_publication_digest_match(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> str | None:
        self._require_observed_publication_target(
            connection, binding, selected_graph_id
        )
        self._usable = False
        version_id = await _read_graph_publication_digest_match(
            connection, self._dialect, selected_graph_id, binding.deployment_digest
        )
        self._lease.require_active()
        self._usable = True
        return version_id

    async def read_graph_publication_ordinal(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> int | None:
        self._require_observed_publication_target(
            connection, binding, selected_graph_id
        )
        self._usable = False
        number = await _read_graph_publication_ordinal(
            connection, self._dialect, selected_graph_id
        )
        self._lease.require_active()
        self._usable = True
        return number

    async def acquire_existing_storage_address(
        self, store_id: str, payload_digest: str
    ) -> None:
        await self.acquire_existing_storage_addresses(
            ((store_id, payload_digest),), max_addresses=1
        )

    async def acquire_existing_storage_addresses(
        self,
        addresses: tuple[tuple[str, str], ...],
        *,
        max_addresses: int,
    ) -> None:
        self._require_current()
        if self._scopes is None or self._storage_addresses is not None:
            raise LockError("the storage address is acquired once after scopes")
        self._usable = False
        keys = _bounded_storage_keys(addresses, max_addresses)
        for index, address in enumerate(sorted(keys)):
            if not await _lock_existing_address(
                self._lease.connection,
                self._dialect,
                address,
                guard_table=index == 0,
            ):
                raise_private_metadata_error(StorageAddressMissing())
            self._lease.require_active()
        self._storage_addresses = keys
        self._usable = True

    def require_existing_storage_address(
        self, connection: AsyncConnection, store_id: str, payload_digest: str
    ) -> None:
        self.require_connection(connection)
        key = _storage_key(store_id, payload_digest)
        if self._storage_addresses is None or key not in self._storage_addresses:
            raise LockError("the required storage address lock is not held")

    async def acquire_open_graph_preparation(
        self,
        connection: AsyncConnection,
        store_id: str,
        payload_digest: str,
        binding: GraphPreparationBinding,
    ) -> None:
        self.require_existing_storage_address(connection, store_id, payload_digest)
        await self._acquire_graph_preparation(connection, binding, None)

    async def _acquire_graph_preparation(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str | None,
    ) -> None:
        if self._graph_preparation is not None:
            raise LockError("the graph preparation is acquired once")
        self._usable = False
        snapshot = await _lock_open_preparation(connection, self._dialect, binding)
        self._lease.require_active()
        self._graph_preparation = _GraphPreparationGuard(snapshot, selected_graph_id)
        self._usable = True

    def _require_publication_preparation_scope(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> None:
        self._require_selected_graph_scopes(connection, binding, selected_graph_id)
        if self._storage_addresses is None:
            raise LockError("the graph preparation follows the completed address phase")

    async def acquire_publication_graph_preparation(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> None:
        self._require_publication_preparation_scope(
            connection, binding, selected_graph_id
        )
        await self._acquire_graph_preparation(connection, binding, selected_graph_id)

    def require_publication_graph_preparation(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
    ) -> None:
        self._require_publication_preparation_scope(
            connection, binding, selected_graph_id
        )
        guard = self._graph_preparation
        if (
            guard is None
            or guard.binding != binding
            or guard.selected_graph_id != selected_graph_id
        ):
            raise LockError("the required publication preparation lock is not held")

    def require_open_graph_preparation(
        self,
        connection: AsyncConnection,
        store_id: str,
        payload_digest: str,
        binding: GraphPreparationBinding,
    ) -> None:
        self.require_existing_storage_address(connection, store_id, payload_digest)
        require_graph_preparation_binding(binding)
        guard = self._graph_preparation
        if guard is None or guard.binding != binding:
            raise LockError("the required open graph preparation lock is not held")

    async def read_stage_fence_incarnation(
        self,
        connection: AsyncConnection,
        store_id: str,
        payload_digest: str,
        binding: GraphPreparationBinding,
    ) -> int:
        self.require_open_graph_preparation(
            connection, store_id, payload_digest, binding
        )
        self._usable = False
        row = await _read_storage_fence_incarnation(
            connection,
            self._dialect,
            store_id,
            payload_digest,
            max_last_generation=MAX_COUNT - 1,
            read_failed=StorageAddressStageReadFailed,
        )
        self._lease.require_active()
        if row is None:
            raise_private_metadata_error(StorageAddressStageUnavailable())
        self._usable = True
        (incarnation,) = row
        if incarnation is None:
            raise_private_metadata_error(StorageAddressStageUnavailable())
        return incarnation

    async def require_publication_stage_fence(
        self,
        connection: AsyncConnection,
        binding: GraphPreparationBinding,
        selected_graph_id: str,
        store_id: str,
        payload_digest: str,
        expected_incarnation: int,
    ) -> None:
        self.require_publication_graph_preparation(
            connection, binding, selected_graph_id
        )
        self.require_existing_storage_address(connection, store_id, payload_digest)
        if (
            type(expected_incarnation) is not int
            or not 0 < expected_incarnation <= MAX_COUNT
        ):
            raise_private_metadata_error(StorageAddressPublicationFenceUnavailable())
        self._usable = False
        row = await _read_storage_fence_incarnation(
            connection,
            self._dialect,
            store_id,
            payload_digest,
            max_last_generation=MAX_COUNT,
            read_failed=StorageAddressPublicationFenceReadFailed,
        )
        self._lease.require_active()
        if row is None:
            raise_private_metadata_error(StorageAddressPublicationFenceUnavailable())
        self._usable = True
        if row[0] != expected_incarnation:
            raise_private_metadata_error(StorageAddressPublicationFenceUnavailable())
