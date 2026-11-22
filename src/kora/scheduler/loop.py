import asyncio

from sqlalchemy.exc import SQLAlchemyError

from kora.cel_eval.errors import raise_safe
from kora.engine.admission_processing import AdmissionProcessor
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.staging_maintenance import StagingMaintenance
from kora.protocols.scheduler_lifecycle import SchedulerFailure
from kora.scheduler.discovery import Discovery
from kora.scheduler.tick import LIFECYCLE, Tick, emit


class Scheduler:
    def __init__(self, tick: Tick) -> None:
        self._tick = tick
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def recover(self) -> None:
        try:
            await self._tick.recover(self._stopped.is_set)
        except (RuntimeError, SQLAlchemyError):
            raise_safe(SchedulerFailure())

    async def run(self) -> None:
        if self._stopped.is_set():
            return
        emit(LIFECYCLE, "started")
        try:
            while not self._stopped.is_set():
                if not await self._tick(self._stopped.is_set):
                    try:
                        async with asyncio.timeout(1.0):
                            await self._stopped.wait()
                    except TimeoutError:
                        continue
        except (RuntimeError, SQLAlchemyError):
            raise_safe(SchedulerFailure())
        finally:
            emit(LIFECYCLE, "stopped")


def first_claim_scheduler(
    admission: AdmissionProcessor, *, read_seconds: float = 10.0, page_size: int = 100
) -> Scheduler:
    if not admission.initial_root_only or admission.global_limit != 1:
        raise ValueError("first-Claim scheduling requires single-root admission")
    database, limits = admission.database, admission.limits
    clock, identifiers = admission.clock, admission.identifiers
    source = limits.preparation.snapshot
    source.admit()
    graph = GraphSnapshotLimits(
        source.graph, source.graph_native_bytes, source.owner_text_bytes
    )
    return Scheduler(
        Tick(
            Discovery(database, page_size),
            clock,
            read_seconds,
            PendingPromotion(database, graph, limits.promotion, clock, identifiers),
            admission,
            StagingMaintenance(
                database,
                limits.promotion,
                limits.sources.attempt_row,
                clock,
                identifiers,
                admission.selection.runtime,
            ),
        )
    )
