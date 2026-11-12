from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.reads import SweepPage, highest_run_id
from kora.db.tables import run


class PendingRunSweep:
    def __init__(self, page_size: int = 100) -> None:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError(
                "a pending run page holds between one and one hundred rows"
            )
        self.page_size = page_size
        self._cursor: str | None = None
        self._ceiling: str | None = None

    async def next_page(self, connection: AsyncConnection) -> SweepPage:
        wrapped = self._ceiling is None
        if wrapped:
            self._ceiling = await highest_run_id(connection)
            self._cursor = None
        if self._ceiling is None:
            return SweepPage((), None, None, wrapped)
        statement = select(run.c.id).where(
            run.c.status == "pending", run.c.id <= self._ceiling
        )
        if self._cursor is not None:
            statement = statement.where(run.c.id > self._cursor)
        with await connection.execute(
            statement.order_by(run.c.id).limit(self.page_size)
        ) as result:
            ids = [str(row[0]) for row in result]
        if len(ids) < self.page_size:
            self._ceiling = None
        elif ids:
            self._cursor = ids[-1]
        return SweepPage(ids, self._cursor, self._ceiling, wrapped)
