import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from kora.db.deadline_discovery import DueAttemptSweep
from kora.db.identifiers import Clock
from kora.db.pending_runs import RunStatusSweep
from kora.db.reads import MarkedRunSweep
from kora.engine.admission_processing import AdmissionProcessor
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.staging_maintenance import StagingMaintenance
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.scheduler_lifecycle import SchedulerFailure
from kora.scheduler.discovery import Discovery

COMPONENT: str = CATALOG.register_component("scheduler")
LIFECYCLE = CATALOG.register(
    EventSpec(
        "scheduler.lifecycle",
        COMPONENT,
        Level.INFO,
        "scheduler lifecycle",
        {"outcome": FieldKind.STRING},
    )
)
TRANSITION = CATALOG.register(
    EventSpec(
        "scheduler.transition",
        COMPONENT,
        Level.DEBUG,
        "scheduler transition committed",
        {"outcome": FieldKind.STRING},
    )
)


def emit(event: EventSpec, outcome: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(event, outcome=outcome)


@dataclass(frozen=True)
class Tick:
    discovery: Discovery
    clock: Clock
    read_seconds: float
    promotion: PendingPromotion
    admission: AdmissionProcessor
    maintenance: StagingMaintenance

    def deadline(self) -> float:
        return asyncio.get_running_loop().time() + self.read_seconds

    async def attempt_page(
        self,
        sweep: DueAttemptSweep,
        stopped: Callable[[], bool],
        *,
        startup: bool = False,
    ) -> tuple[int, bool] | None:
        page = await self.discovery.read(
            partial(sweep.next_page, now=self.clock()), self.deadline()
        )
        if page is None:
            return None
        changed = False
        for attempt_id, run_id in page:
            if stopped():
                break
            changed |= await self.maintenance.attempt(
                attempt_id,
                run_id,
                lease=sweep.kind == "lease",
                startup=startup,
                deadline=self.deadline(),
                stopped=stopped,
            )
        return len(page), changed

    async def recover(self, stopped: Callable[[], bool]) -> None:
        sweep = DueAttemptSweep("lease", self.discovery.size)
        while not stopped():
            page = await self.attempt_page(sweep, stopped, startup=True)
            if page is None:
                raise SchedulerFailure()
            if page[0] < self.discovery.size:
                if not stopped():
                    emit(LIFECYCLE, "recovered")
                return
            await asyncio.sleep(0)

    async def run_page(
        self, sweep: RunStatusSweep | MarkedRunSweep, stopped: Callable[[], bool]
    ) -> bool:
        page = await self.discovery.read(sweep.next_page, self.deadline())
        if page is None:
            return False
        changed = False
        for run_id in page.run_ids:
            if stopped():
                break
            deadline = self.deadline()
            if sweep is self.discovery.pending:
                outcome = await self.promotion.process(
                    run_id, deadline=deadline, stopped=stopped
                )
                if outcome is PromotionOutcome.APPLIED:
                    emit(TRANSITION, "promoted")
                    changed = True
            elif sweep is self.discovery.running:
                changed |= await self.maintenance.expire_running(
                    run_id, deadline=deadline, stopped=stopped
                )
            else:
                changed |= await self.maintenance.settle_marked(
                    run_id, deadline=deadline, stopped=stopped
                )
        return changed

    async def admit_page(self, stopped: Callable[[], bool]) -> bool:
        page = await self.discovery.read(
            self.discovery.admission.next_page, self.deadline()
        )
        if page is None:
            return False
        changed = False
        for node_id in page.node_ids:
            if stopped():
                break
            outcome = await self.admission.process(
                node_id,
                periodic_recheck=True,
                deadline=self.deadline(),
                stopped=stopped,
            )
            if outcome is AttemptAdmissionState.QUEUED:
                emit(TRANSITION, "admitted")
                changed = True
            changed |= outcome is AttemptAdmissionState.EXPIRED
        return changed

    async def __call__(self, stopped: Callable[[], bool]) -> bool:
        changed = False
        for sweep in (self.discovery.leases, self.discovery.deadlines):
            if stopped():
                return changed
            page = await self.attempt_page(sweep, stopped)
            if page is not None:
                changed |= page[1]
            await asyncio.sleep(0)
        for sweep in (
            self.discovery.running,
            self.discovery.marked,
            self.discovery.pending,
        ):
            if stopped():
                return changed
            changed |= await self.run_page(sweep, stopped)
            await asyncio.sleep(0)
        if stopped():
            return changed
        return await self.admit_page(stopped) or changed
