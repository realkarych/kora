import asyncio
from dataclasses import dataclass
from datetime import timedelta

from kora.cel_eval.errors import raise_safe
from kora.db.claim_sources import ClaimSourceBinding, ClaimSourceReadLimits
from kora.db.connection import (
    WRITER_ENTRY_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotRejected,
    DatabaseWriterRejected,
    clear_snapshot_error,
    require_snapshot_time,
)
from kora.db.identifiers import Clock, IdentifierGenerator
from kora.engine.admission_preparation import (
    AdmissionPreparationLimits,
    PreparedAdmissionSnapshot,
    prepare_admission_snapshot,
)
from kora.engine.attempt_admission import (
    AttemptAdmissionFailed,
    AttemptAdmissionState,
    PreparedAttemptAdmission,
    admit_attempt,
)
from kora.engine.attempt_preparation import prepare_attempt_admission
from kora.engine.attempt_preparation_budget import AttemptPreparationLimits
from kora.engine.claim_material import ClaimRuntimeSelection
from kora.engine.expiry_context import ExpiryContext
from kora.engine.locks import COMPONENT, LockError, Scope, ScopeKind, TransitionLocks
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.ir.deployment_checks import DeploymentSelection
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_processing.completed",
        COMPONENT,
        Level.DEBUG,
        "admission candidate processing completed",
        {"outcome": FieldKind.STRING},
    )
)


class AdmissionProcessingFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("admission candidate processing failed")


@dataclass(frozen=True, slots=True)
class AdmissionProcessingLimits:
    preparation: AdmissionPreparationLimits
    sources: ClaimSourceReadLimits
    promotion: PromotionReadLimits
    claim_work: int


@dataclass(frozen=True, slots=True)
class AdmissionProcessor:
    database: Database
    limits: AdmissionProcessingLimits
    selection: ClaimRuntimeSelection
    installation: DeploymentSelection
    clock: Clock
    identifiers: IdentifierGenerator
    global_limit: int | None
    runtime_limit: int | None
    interval: timedelta

    async def _process(
        self, node_id: str, periodic_recheck: bool, deadline: float
    ) -> AttemptAdmissionState:
        source: PreparedAdmissionSnapshot | None = None
        binding: ClaimSourceBinding | None = None
        prepared: PreparedAttemptAdmission | None = None
        try:
            limits = self.limits
            if type(limits.claim_work) is not int or limits.claim_work <= 0:
                raise AdmissionProcessingFailed()
            source = await prepare_admission_snapshot(
                self.database,
                node_id,
                limits.preparation,
                observed_at=self.clock(),
                periodic_recheck=periodic_recheck,
                deadline=deadline,
            )
            binding = ClaimSourceBinding(
                source.acquired.candidate.run,
                node_id,
                self.identifiers.new(),
                source.acquired.price_table_id,
                source.acquired.candidate.node_path,
            )
            prepared = prepare_attempt_admission(
                source.checked,
                source.readiness,
                source.acquired.params,
                binding,
                source.price,
                source.policy,
                self.selection,
                self.installation,
                sources=limits.sources,
                limits=AttemptPreparationLimits(
                    limits.preparation.snapshot.graph,
                    source.reservation.retained_units,
                    limits.preparation.retained_units,
                    limits.claim_work,
                ),
            )
            await asyncio.sleep(0)
            require_snapshot_time(deadline)
            async with asyncio.timeout_at(deadline):
                async with self.database.transaction(
                    admission_deadline=deadline
                ) as connection:
                    locks = TransitionLocks(connection)
                    if not await locks.acquire_run(binding.run.run_id, wait=False):
                        return AttemptAdmissionState.REFUSED
                    await locks.acquire_scopes(
                        (
                            Scope(ScopeKind.GLOBAL),
                            Scope(ScopeKind.NAMESPACE, (binding.run.namespace_id,)),
                            Scope(ScopeKind.GRAPH, (binding.run.graph_id,)),
                            Scope(ScopeKind.RUNTIME, (self.selection.runtime,)),
                        ),
                        deadline=deadline,
                    )
                    outcome = await admit_attempt(
                        ExpiryContext(
                            connection,
                            locks,
                            source.checked.scope,
                            binding.run.run_id,
                            self.clock,
                            self.identifiers,
                            limits.promotion,
                            limits.sources.attempt_row,
                        ),
                        prepared,
                        limits.sources,
                        source.caps,
                        global_limit=self.global_limit,
                        runtime_limit=self.runtime_limit,
                        interval=self.interval,
                        expected_wait_id=source.acquired.wait_id,
                        periodic_recheck=periodic_recheck,
                    )
            return outcome
        finally:
            del source, prepared, binding

    async def process(
        self, node_id: str, *, periodic_recheck: bool, deadline: float
    ) -> AttemptAdmissionState:
        outcome = "failed"
        try:
            result = await self._process(node_id, periodic_recheck, deadline)
            outcome = result.value
            return result
        except asyncio.CancelledError as error:
            outcome = "cancelled"
            clear_snapshot_error(error)
            raise
        except (
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
            TimeoutError,
            DatabaseSnapshotRejected,
            DatabaseWriterRejected,
            LockError,
            AttemptAdmissionFailed,
            *WRITER_ENTRY_NATIVE_ERRORS,
        ):
            raise_safe(AdmissionProcessingFailed())
        finally:
            del self, node_id
            diagnostics = current_diagnostics()
            if diagnostics is not None:
                diagnostics.logger(COMPONENT).emit(COMPLETED, outcome=outcome)
