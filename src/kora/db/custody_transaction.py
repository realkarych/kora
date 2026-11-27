from dataclasses import dataclass
from typing import Final, Self

import aiosqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

from kora.db.completion import complete
from kora.db.custody_models import (
    CustodyNativeOutcome,
    CustodyPendingCommit,
    CustodyTransactionBinding,
    CustodyUnavailable,
)
from kora.db.custody_sqlite import SQLiteWorkerOriginal
from kora.db.types import SQLITE

_FRONTIER_APPLIED: Final[object] = object()


async def native_writer_driver(connection: AsyncConnection) -> object:
    proxied = await connection.get_raw_connection()
    return proxied.driver_connection


@dataclass(frozen=True, init=False)
class AppliedCustodyFrontier:
    pending: CustodyPendingCommit
    _owner: object

    def __init__(
        self, authority: object, pending: CustodyPendingCommit, owner: object
    ) -> None:
        if authority is not _FRONTIER_APPLIED:
            raise CustodyUnavailable
        object.__setattr__(self, "pending", pending)
        object.__setattr__(self, "_owner", owner)

    def require_owner(self, owner: object) -> None:
        if self._owner is not owner:
            raise CustodyUnavailable


class OriginalCustodyTransaction:
    _connection: AsyncConnection
    _transaction: AsyncTransaction
    _binding: CustodyTransactionBinding
    _driver: object
    _custody_owner: object | None
    _started: bool
    _outcome: CustodyNativeOutcome | None
    _sqlite: SQLiteWorkerOriginal
    _sqlite_released: bool

    def __init__(
        self,
        connection: AsyncConnection,
        transaction: AsyncTransaction,
        binding: CustodyTransactionBinding,
        driver: object,
        sqlite: SQLiteWorkerOriginal,
    ) -> None:
        self._connection = connection
        self._transaction = transaction
        self._binding = binding
        self._driver = driver
        self._custody_owner = None
        self._started = False
        self._outcome = None
        self._sqlite = sqlite
        self._sqlite_released = False
        self._require_original()

    @classmethod
    async def capture(
        cls,
        connection: AsyncConnection,
        *,
        metadata_id: bytes,
        issuer_sequence: int,
        connection_sequence: int,
        transaction_sequence: int,
        sqlite: SQLiteWorkerOriginal,
    ) -> Self:
        transaction = connection.get_transaction()
        if (
            connection.dialect.name != SQLITE
            or transaction is None
            or not transaction.is_active
        ):
            raise CustodyUnavailable
        driver = await native_writer_driver(connection)
        if driver is not sqlite.driver or not isinstance(driver, aiosqlite.Connection):
            raise CustodyUnavailable
        original = cls(
            connection,
            transaction,
            CustodyTransactionBinding(
                metadata_id,
                issuer_sequence,
                connection_sequence,
                transaction_sequence,
                sqlite.settings_digest,
                None,
            ),
            driver,
            sqlite,
        )
        await original._require_native_original()
        return original

    def binding_for_preparation(self) -> CustodyTransactionBinding:
        if self._started:
            raise CustodyUnavailable
        self._require_original()
        return self._binding

    def bind_custody_owner(self, owner: object) -> CustodyTransactionBinding:
        if self._custody_owner is not None or owner is None:
            raise CustodyUnavailable
        binding = self.binding_for_preparation()
        self._custody_owner = owner
        return binding

    async def verify_native_custody_owner(self, owner: object) -> None:
        if owner is None or owner is not self._custody_owner:
            raise CustodyUnavailable
        await self._require_native_original()

    async def advance_frontier_before_dispatch(
        self, pending: CustodyPendingCommit, owner: object
    ) -> AppliedCustodyFrontier:
        if pending.transaction != self.binding_for_preparation():
            raise CustodyUnavailable
        await self.verify_native_custody_owner(owner)
        await self._sqlite.advance_frontier(pending)
        await self.verify_native_custody_owner(owner)
        return AppliedCustodyFrontier(_FRONTIER_APPLIED, pending, owner)

    def outcome_after_release(
        self, pending: CustodyPendingCommit, owner: object
    ) -> CustodyNativeOutcome:
        outcome = self._outcome
        if (
            owner is None
            or owner is not self._custody_owner
            or pending.transaction != self._binding
            or not self.resources_released()
            or outcome is None
            or outcome is CustodyNativeOutcome.UNKNOWN
        ):
            raise CustodyUnavailable
        return outcome

    def resources_released(self) -> bool:
        return self._sqlite_released

    async def close_sqlite(self) -> None:
        await self._sqlite.require_native_closed()
        await self._connection.close()

    def confirm_sqlite_release(self) -> None:
        self._sqlite_released = True

    @property
    def outcome(self) -> CustodyNativeOutcome | None:
        return self._outcome

    async def commit(self, *, custody_owner: object) -> None:
        if custody_owner is not self._custody_owner:
            raise CustodyUnavailable
        self._start()
        await complete(self._finish(commit=True))

    async def rollback(self) -> None:
        self._start()
        await complete(self._finish(commit=False))

    def _start(self) -> None:
        if self._started:
            raise CustodyUnavailable
        self._started = True

    def _require_original(self) -> None:
        if (
            self._connection.closed
            or self._connection.invalidated
            or self._connection.get_transaction() is not self._transaction
            or not self._transaction.is_active
        ):
            raise CustodyUnavailable

    async def _require_native_original(self) -> None:
        self._require_original()
        driver = await native_writer_driver(self._connection)
        if driver is not self._driver:
            raise CustodyUnavailable
        await self._sqlite.require_original()

    async def _finish(self, *, commit: bool) -> None:
        try:
            if commit:
                try:
                    await self._sqlite.commit()
                except CustodyUnavailable:
                    await self._sqlite.rollback()
                    raise
            else:
                await self._sqlite.rollback()
        finally:
            self._outcome = self._sqlite.outcome or CustodyNativeOutcome.UNKNOWN


@dataclass(frozen=True, init=False)
class ReleasedCustodyCommit:
    pending: CustodyPendingCommit
    outcome: CustodyNativeOutcome
    _owner: object

    def __init__(
        self,
        original: OriginalCustodyTransaction,
        pending: CustodyPendingCommit,
        owner: object,
    ) -> None:
        object.__setattr__(self, "pending", pending)
        object.__setattr__(
            self, "outcome", original.outcome_after_release(pending, owner)
        )
        object.__setattr__(self, "_owner", owner)

    def require_owner(self, owner: object) -> None:
        if self._owner is not owner:
            raise CustodyUnavailable
