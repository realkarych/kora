from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import (
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
)
from kora.db.deadline_discovery import DueAttemptSweep, RunningRunSweep
from kora.db.pending_runs import PendingRunSweep
from kora.db.reads import MarkedRunSweep
from kora.engine.admission_discovery import AdmissionSweep

Page = TypeVar("Page")


@dataclass
class Discovery:
    database: Database
    size: int

    def __post_init__(self) -> None:
        self.pending = PendingRunSweep(self.size)
        self.marked = MarkedRunSweep(self.size)
        self.running = RunningRunSweep(self.size)
        self.admission = AdmissionSweep(self.size)
        self.leases = DueAttemptSweep("lease", self.size)
        self.deadlines = DueAttemptSweep("deadline", self.size)

    async def read(
        self, read: Callable[[AsyncConnection], Awaitable[Page]], deadline: float
    ) -> Page | None:
        try:
            async with self.database.read_snapshot(deadline=deadline) as connection:
                return await read(connection)
        except DatabaseSnapshotRejected as error:
            if error.reason is not DatabaseSnapshotFailure.DEADLINE:
                raise
            return None
