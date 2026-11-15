from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

from kora.db.completion import complete
from kora.db.custody_models import (
    CustodyNativeOutcome,
    CustodyTransactionBinding,
    CustodyUnavailable,
)


class OriginalCustodyTransaction:
    _connection: AsyncConnection
    _transaction: AsyncTransaction
    _binding: CustodyTransactionBinding
    _started: bool
    _outcome: CustodyNativeOutcome | None

    def __init__(
        self, connection: AsyncConnection, binding: CustodyTransactionBinding
    ) -> None:
        transaction = connection.get_transaction()
        if transaction is None or not transaction.is_active:
            raise CustodyUnavailable
        self._connection = connection
        self._transaction = transaction
        self._binding = binding
        self._started = False
        self._outcome = None

    @property
    def binding(self) -> CustodyTransactionBinding:
        return self._binding

    @property
    def outcome(self) -> CustodyNativeOutcome | None:
        return self._outcome

    async def commit(self) -> None:
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

    async def _finish(self, *, commit: bool) -> None:
        outcome = CustodyNativeOutcome.UNKNOWN
        try:
            self._require_original()
            if commit:
                await self._transaction.commit()
                outcome = CustodyNativeOutcome.COMMITTED
            else:
                await self._transaction.rollback()
                outcome = CustodyNativeOutcome.ABORTED
        finally:
            self._outcome = outcome
