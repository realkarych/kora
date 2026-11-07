import enum
import hashlib
import json
import re
from dataclasses import dataclass

from sqlalchemy import bindparam, literal, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.identifiers import is_identifier
from kora.db.tables import storage_address_fence
from kora.db.transactions import raise_private_metadata_error, writer_transaction
from kora.db.types import POSTGRESQL, SQLITE
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

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


class LockError(RuntimeError):
    pass


class StorageAddressMissing(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence is missing")


class StorageAddressContended(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence is contended")


class StorageAddressLockFailed(LockError):
    def __init__(self) -> None:
        super().__init__("the storage address fence lock failed")


def _storage_key(store_id: str, payload_digest: str) -> tuple[str, str]:
    if (
        type(store_id) is not str
        or STORE_ID_PATTERN.fullmatch(store_id) is None
        or type(payload_digest) is not str
        or STORAGE_DIGEST_PATTERN.fullmatch(payload_digest) is None
    ):
        raise_private_metadata_error(LockError("invalid storage address lock request"))
    return store_id, payload_digest


async def _lock_existing_address(
    connection: AsyncConnection, dialect: str, address: tuple[str, str]
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
    try:
        if dialect == POSTGRESQL:
            table_lock = await connection.execute(
                text("LOCK TABLE storage_address_fence IN ROW SHARE MODE NOWAIT")
            )
            table_lock.close()
            statement = statement.with_for_update(nowait=True)
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
            if dialect == POSTGRESQL
            and isinstance(error, DBAPIError)
            and getattr(error.orig, "sqlstate", None) == "55P03"
            else StorageAddressLockFailed()
        )
    raise_private_metadata_error(failure)


class ScopeKind(enum.IntEnum):
    GLOBAL = 2
    NAMESPACE = 3
    GRAPH = 4
    RUNTIME = 5
    FAN_OUT = 6
    TARGET = 7


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
        sizes = {ScopeKind.GLOBAL: 0, ScopeKind.FAN_OUT: 3, ScopeKind.TARGET: 2}
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


class TransitionLocks:
    def __init__(self, connection: AsyncConnection) -> None:
        self._lease = writer_transaction(connection)
        self._lease.claim_lock_context()
        self._run_id: str | None = None
        self._scopes: frozenset[Scope] | None = None
        self._storage_address: tuple[str, str] | None = None
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

    async def acquire_scopes(self, scopes: tuple[Scope, ...]) -> None:
        self._require_current()
        if self._scopes is not None:
            raise LockError("the scope set is acquired once")
        selected = frozenset(scopes)
        keys = sorted({scope.key for scope in selected})
        self._usable = False
        if self._dialect == POSTGRESQL:
            for kind, identity in keys:
                result = await self._lease.connection.execute(
                    text("SELECT pg_advisory_xact_lock(:kind, :identity)"),
                    {"kind": kind, "identity": identity},
                )
                result.close()
        self._lease.require_active()
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

    async def acquire_existing_storage_address(
        self, store_id: str, payload_digest: str
    ) -> None:
        self._require_current()
        if self._scopes is None or self._storage_address is not None:
            raise LockError("the storage address is acquired once after scopes")
        self._usable = False
        address = _storage_key(store_id, payload_digest)
        if not await _lock_existing_address(
            self._lease.connection, self._dialect, address
        ):
            raise_private_metadata_error(StorageAddressMissing())
        self._lease.require_active()
        self._storage_address = address
        self._usable = True

    def require_existing_storage_address(
        self, connection: AsyncConnection, store_id: str, payload_digest: str
    ) -> None:
        self.require_connection(connection)
        if self._storage_address != _storage_key(store_id, payload_digest):
            raise LockError("the required storage address lock is not held")
