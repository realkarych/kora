import asyncio
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError

from kora.cel_eval.errors import raise_safe
from kora.db.connection import Database
from kora.db.identifiers import Clock, IdentifierGenerator
from kora.engine.admission_processing import AdmissionProcessor
from kora.engine.claim_material import ClaimRuntimeSelection
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.staging_maintenance import StagingMaintenance
from kora.engine.worker_claim_poll import ClaimPollLimits
from kora.ir.deployment_checks import DeploymentSelection
from kora.protocols.runtime import RuntimeCapabilities
from kora.protocols.scheduler_lifecycle import SchedulerFailure
from kora.scheduler.discovery import Discovery
from kora.scheduler.tick import LIFECYCLE, Tick, emit, initial_admission_limits


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
    database: Database,
    worker: ClaimPollLimits,
    clock: Clock,
    identifiers: IdentifierGenerator,
    selection: DeploymentSelection,
    capabilities: RuntimeCapabilities,
    *,
    page_size: int = 100,
) -> Scheduler:
    claim = worker.snapshot
    graph = GraphSnapshotLimits(
        claim.graph, claim.graph_native_bytes, claim.owner_text_bytes
    )
    admission = AdmissionProcessor(
        database,
        initial_admission_limits(worker),
        ClaimRuntimeSelection("local", "git", capabilities.default_stall_after),
        selection,
        clock,
        identifiers,
        1,
        capabilities.max_concurrent,
        timedelta(seconds=1),
        initial_root_only=True,
    )
    return Scheduler(
        Tick(
            Discovery(database, page_size),
            clock,
            worker.read_seconds,
            PendingPromotion(database, graph, worker.expiry, clock, identifiers),
            admission,
            StagingMaintenance(
                database,
                worker.expiry,
                claim.sources.attempt_row,
                clock,
                identifiers,
                "local",
            ),
        )
    )
