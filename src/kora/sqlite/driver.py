import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from functools import cache
from itertools import islice
from pathlib import Path
from sqlite3 import SQLITE_BUSY as SQLITE_BUSY
from sqlite3 import SQLITE_LOCKED as SQLITE_LOCKED
from sqlite3 import Error as Error
from threading import Lock
from typing import TypeAlias
from weakref import WeakSet

Parameters: TypeAlias = Sequence[object] | Mapping[str, object]
Row: TypeAlias = tuple[object, ...]
Admission: TypeAlias = Callable[[int, int], bool]
Authorizer: TypeAlias = Callable[
    [int, str | None, str | None, str | None, str | None], int
]


@cache
def driver_version() -> str:
    try:
        from kora.sqlite import _native
    except ImportError as error:
        raise sqlite3.OperationalError(
            "The installed SQLite driver is unavailable."
        ) from error
    return _native.sqlite_version


class Connection:
    def __init__(self, retire: Callable[["Connection"], None] | None = None) -> None:
        driver_version()
        from kora.sqlite import _native

        self.native = _native
        self.handle: object = _native.allocate()
        self.cursors: WeakSet[Cursor] = WeakSet()
        self.isolation_level: str | None = None
        self._retire = retire

    def open(
        self,
        path: Path,
        readonly: bool,
        timeout_ms: int,
        admit: Admission,
        *,
        nofollow: bool = False,
    ) -> None:
        self.native.open(self.handle, str(path), readonly, timeout_ms, nofollow, admit)

    def close(self) -> None:
        for cursor in tuple(self.cursors):
            cursor.close()
        try:
            self.native.close(self.handle)
        finally:
            retire, self._retire = self._retire, None
            if retire is not None:
                retire(self)

    def settled(self) -> bool:
        return self.native.settled(self.handle)

    @property
    def in_transaction(self) -> bool:
        return self.native.state(self.handle)[0] == 0

    def cursor(self) -> "Cursor":
        cursor = Cursor(self)
        self.cursors.add(cursor)
        return cursor

    def execute(self, query: str, parameters: Parameters = ()) -> "Cursor":
        return self.cursor().execute(query, parameters)

    def executemany(self, query: str, parameters: Iterable[Parameters]) -> "Cursor":
        return self.cursor().executemany(query, parameters)

    def commit(self) -> None:
        if self.in_transaction:
            self.execute("COMMIT").close()

    def rollback(self) -> None:
        if self.in_transaction:
            self.execute("ROLLBACK").close()

    def interrupt(self) -> None:
        self.native.interrupt(self.handle)

    def set_progress_handler(
        self, handler: Callable[[], int] | None, interval: int
    ) -> None:
        self.native.callback(self.handle, 0, handler, interval)

    def set_trace_callback(self, handler: Callable[[str], object] | None) -> None:
        self.native.callback(self.handle, 1, handler, 0)

    def set_authorizer(self, handler: Authorizer | None) -> None:
        self.native.callback(self.handle, 2, handler, 0)

    def create_function(
        self,
        name: str,
        arguments: int,
        handler: Callable[..., object],
        *,
        deterministic: bool = False,
    ) -> None:
        self.native.function(self.handle, name, arguments, handler, deterministic)

    def blobopen(
        self, table: str, column: str, row: int, *, readonly: bool, name: str
    ) -> "Blob":
        if not readonly or name != "main":
            raise sqlite3.NotSupportedError(
                "Only readonly blobs in the main database are supported."
            )
        return Blob(self, self.native.open_blob(self.handle, table, column, row))


class Blob:
    def __init__(self, connection: Connection, handle: object) -> None:
        self.connection = connection
        self.handle = handle

    def __len__(self) -> int:
        return self.connection.native.blob_length(self.handle)

    def close(self) -> None:
        self.connection.native.close_blob(self.handle)

    def __enter__(self) -> "Blob":
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()


class NativeRetirement:
    _lock = Lock()
    _pending: dict[Path, dict[Connection, Callable[[], None]]] = {}
    _after: dict[Path, set[Callable[[], None]]] = {}

    @classmethod
    def after(cls, path: Path, release: Callable[[], None]) -> None:
        with cls._lock:
            if path in cls._pending:
                cls._after.setdefault(path, set()).add(release)
                return
        release()

    @classmethod
    def retain(
        cls, path: Path, connection: Connection, release: Callable[[], None]
    ) -> None:
        with cls._lock:
            cls._pending.setdefault(path, {})[connection] = release
        cls.drain(path)

    @classmethod
    def drain(cls, path: Path) -> bool:
        with cls._lock:
            scope = cls._pending.get(path)
            if not scope:
                return True
            for connection, release in tuple(scope.items()):
                if connection.settled():
                    release()
                    del scope[connection]
            if not scope:
                del cls._pending[path]
                for release in cls._after.pop(path, ()):
                    release()
            return not scope


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.row_factory: Callable[[Cursor, Row], object] | None = None
        self.arraysize: int = 1
        self.description: (
            tuple[tuple[str, None, None, None, None, None, None], ...] | None
        ) = None
        self.rowcount: int = -1
        self.lastrowid: int | None = None
        self._closed: bool = False
        self._statement: object | None = None
        self._row: Row | None = None

    def execute(self, query: str, parameters: Parameters = ()) -> "Cursor":
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        self._clear()
        native = self.connection.native
        operation = query.split(None, 1)[0].upper() if query.strip() else ""
        writes = operation in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
        if (
            writes
            and self.connection.isolation_level is not None
            and not self.connection.in_transaction
        ):
            self.connection.execute(f"BEGIN {self.connection.isolation_level}").close()
        statement = native.prepare(self.connection.handle, query)
        if statement is None:
            self.description = None
            return self
        self._statement = statement
        native.bind(statement, parameters)
        self._row = native.step(statement)
        columns = native.columns(statement)
        self.description = (
            None
            if columns is None
            else tuple(
                (column, None, None, None, None, None, None) for column in columns
            )
        )
        _, changed, self.lastrowid = native.state(self.connection.handle)
        self.rowcount = changed if writes and self._row is None else -1
        return self

    def executemany(self, query: str, parameters: Iterable[Parameters]) -> "Cursor":
        total = 0
        for item in parameters:
            self.execute(query, item)
            if self.description is not None:
                raise sqlite3.ProgrammingError(
                    "SQLite executemany requires a statement without rows."
                )
            total += self.rowcount
        self.rowcount = total
        return self

    def fetchone(self) -> object:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        row = self._row
        if row is None or self._statement is None:
            return None
        self._row = self.connection.native.step(self._statement)
        return row if self.row_factory is None else self.row_factory(self, row)

    def fetchmany(self, size: int | None = None) -> list[object]:
        return list(islice(self, self.arraysize if size is None else size))

    def fetchall(self) -> list[object]:
        return list(self)

    def __iter__(self) -> Iterator[object]:
        while self._row is not None:
            yield self.fetchone()

    def close(self) -> None:
        self._clear()
        self._closed = True

    def _clear(self) -> None:
        if self._statement is not None:
            self.connection.native.finalize(self._statement)
        self._statement = None
        self._row = None
