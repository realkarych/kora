from datetime import datetime
from typing import Literal

from sqlalchemy import literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.deadline_discovery_v1 import DEADLINE_ROWS, LEASE_ROWS
from kora.db.pending_runs import RunStatusSweep
from kora.db.tables import attempt, node_instance
from kora.db.types import normalise_timestamp

DueKind = Literal["deadline", "lease"]


def check_page_size(page_size: int) -> None:
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError("a deadline discovery page holds one to one hundred rows")


class RunningRunSweep(RunStatusSweep):
    status: str = "running"


class DueAttemptSweep:
    def __init__(self, kind: DueKind, page_size: int = 100) -> None:
        check_page_size(page_size)
        if kind not in ("deadline", "lease"):
            raise ValueError("an attempt discovery source is deadline or lease")
        self.kind = kind
        self.page_size = page_size
        self._cursor: tuple[datetime, str] | None = None
        self._ceiling: str | None = None
        self._cutoff: datetime | None = None

    async def next_page(
        self, connection: AsyncConnection, now: datetime
    ) -> tuple[tuple[str, str], ...]:
        if self._ceiling is None:
            self._cursor = None
            self._cutoff = normalise_timestamp(now)
            self._ceiling = await connection.scalar(
                select(attempt.c.id).order_by(attempt.c.id.desc()).limit(1)
            )
        if self._ceiling is None:
            return ()
        due = attempt.c.deadline if self.kind == "deadline" else attempt.c.lease_until
        predicate = DEADLINE_ROWS if self.kind == "deadline" else LEASE_ROWS
        candidates = (
            select(attempt.c.id, attempt.c.node_instance_id, due.label("due"))
            .where(
                literal_column(predicate),
                due <= self._cutoff,
                attempt.c.id <= self._ceiling,
            )
            .order_by(due, attempt.c.id)
            .limit(self.page_size)
        )
        if self._cursor is not None:
            candidates = candidates.where(tuple_(due, attempt.c.id) > self._cursor)
        page = candidates.subquery()
        statement = (
            select(page.c.id, node_instance.c.run_id, page.c.due)
            .join(node_instance, page.c.node_instance_id == node_instance.c.id)
            .order_by(page.c.due, page.c.id)
        )
        with await connection.execute(statement) as result:
            rows = result.all()
        if len(rows) < self.page_size:
            self._ceiling = None
        elif rows:
            self._cursor = (normalise_timestamp(rows[-1][2]), str(rows[-1][0]))
        return tuple((str(row[0]), str(row[1])) for row in rows)
