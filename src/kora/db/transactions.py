import asyncio
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

WRITER_LEASE: str = "kora_writer_transaction"


class WriterTransactionError(RuntimeError):
    pass


class WriterTransaction:
    def __init__(self, connection: AsyncConnection) -> None:
        transaction = connection.get_transaction()
        if transaction is None or connection.in_nested_transaction():
            raise WriterTransactionError("a root writer transaction is required")
        self.connection = connection
        self._transaction: AsyncTransaction = transaction
        self._task: asyncio.Task[object] | None = asyncio.current_task()
        self._active: bool = True
        self._locks_claimed: bool = False

    def require_active(self) -> None:
        connection = self.connection
        if (
            not self._active
            or self._task is not asyncio.current_task()
            or connection.closed
            or connection.invalidated
            or not self._transaction.is_active
            or connection.get_transaction() is not self._transaction
            or connection.in_nested_transaction()
            or connection.info.get(WRITER_LEASE) is not self
        ):
            raise WriterTransactionError("the writer transaction is not current")

    def claim_lock_context(self) -> None:
        self.require_active()
        if self._locks_claimed:
            raise WriterTransactionError("the transaction already has a lock context")
        self._locks_claimed = True

    def end(self) -> None:
        self._active = False


@contextmanager
def managed_writer(connection: AsyncConnection) -> Generator[None, None, None]:
    storage = connection.info
    if WRITER_LEASE in storage:
        raise WriterTransactionError("the writer transaction is already managed")
    lease = WriterTransaction(connection)
    storage[WRITER_LEASE] = lease
    try:
        yield
    finally:
        lease.end()
        storage.pop(WRITER_LEASE, None)


def writer_transaction(connection: AsyncConnection) -> WriterTransaction:
    if connection.closed or connection.invalidated:
        raise WriterTransactionError("a managed writer transaction is required")
    lease: object = connection.info.get(WRITER_LEASE)
    if not isinstance(lease, WriterTransaction) or lease.connection is not connection:
        raise WriterTransactionError("a managed writer transaction is required")
    lease.require_active()
    return lease
