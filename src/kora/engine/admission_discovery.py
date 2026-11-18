import asyncio
from dataclasses import dataclass

from sqlalchemy import Text, column, literal_column, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.admission_discovery_v1 import CANDIDATE_ROWS, INDEX_NAME
from kora.db.connection import clear_snapshot_error
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import SQLITE
from kora.engine.locks import COMPONENT
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

DISCOVERED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_discovery.selected",
        COMPONENT,
        Level.DEBUG,
        "initial admission candidates selected",
        {"count": FieldKind.INTEGER, "wrapped": FieldKind.BOOLEAN},
    )
)


@dataclass(frozen=True, slots=True)
class AdmissionPage:
    node_ids: tuple[str, ...]
    cursor: str | None
    ceiling: str | None
    wrapped: bool


class AdmissionDiscoveryFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("admission discovery failed")


class AdmissionSweep:
    def __init__(self, page_size: int = 100) -> None:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("an admission page holds one to one hundred IDs")
        self.page_size = page_size
        self._cursor: str | None = None
        self._ceiling: str | None = None

    async def _read(self, connection: AsyncConnection) -> AdmissionPage:
        identity = column("id", Text())
        candidates = (
            select(identity)
            .select_from(
                text(f"main.node_instance INDEXED BY {INDEX_NAME}")
                if connection.dialect.name == SQLITE
                else text("ONLY node_instance")
            )
            .where(literal_column(CANDIDATE_ROWS))
        )
        wrapped = self._ceiling is None
        ceiling, cursor = self._ceiling, self._cursor
        if wrapped:
            with await connection.execute(
                candidates.order_by(identity.desc()).limit(1)
            ) as result:
                ceiling = result.scalar_one_or_none()
            cursor = None
        ids: tuple[str, ...] = ()
        if ceiling is not None:
            statement = candidates.where(identity <= ceiling)
            if cursor is not None:
                statement = statement.where(identity > cursor)
            with await connection.execute(
                statement.order_by(identity).limit(self.page_size)
            ) as result:
                ids = tuple(str(row[0]) for row in result)
        self._cursor = ids[-1] if ids else cursor
        self._ceiling = ceiling if len(ids) == self.page_size else None
        return AdmissionPage(ids, self._cursor, self._ceiling, wrapped)

    async def next_page(self, connection: AsyncConnection) -> AdmissionPage:
        try:
            page = await self._read(connection)
        except asyncio.CancelledError as error:
            clear_snapshot_error(error)
            raise
        except SQLAlchemyError:
            del connection
            raise_private_metadata_error(AdmissionDiscoveryFailed())
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(
                DISCOVERED, count=len(page.node_ids), wrapped=page.wrapped
            )
        return page
