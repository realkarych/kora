import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from kora.cel_eval.errors import raise_safe
from kora.db.claim_sources import ClaimSourceBinding, ClaimSourceReadLimits
from kora.db.connection import (
    WRITER_ENTRY_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
    clear_snapshot_error,
    require_snapshot_time,
)
from kora.db.identifiers import Clock, IdentifierGenerator
from kora.db.run_input_parent import verify_run_input_parent
from kora.engine.admission_preparation import (
    AdmissionPreparationLimits,
    AdmissionPreparationUnavailable,
    PreparedAdmissionSnapshot,
    prepare_admission_snapshot,
)
from kora.engine.attempt_admission import (
    AttemptAdmissionFailed,
    AttemptAdmissionStale,
    AttemptAdmissionState,
    PreparedAttemptAdmission,
    admit_attempt,
)
from kora.engine.attempt_preparation import prepare_attempt_admission
from kora.engine.attempt_preparation_budget import AttemptPreparationLimits
from kora.engine.claim_material import ClaimRuntimeSelection
from kora.engine.claim_timing import first_claim_deadline
from kora.engine.deadline_records import DeadlineRecordRejected
from kora.engine.expiry_context import ExpiryContext
from kora.engine.expiry_reads import read_expiry_run
from kora.engine.locks import (
    COMPONENT,
    LockError,
    Scope,
    ScopeDeadlineExpired,
    ScopeKind,
    TransitionLocks,
)
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.engine.staging_maintenance import admit_staging_inventory
from kora.ir.deployment_checks import DeploymentSelection
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.worker_claim_wire import ClaimWireRejected

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
    initial_root_only: bool = False

    async def _root_source_ready(
        self, context: ExpiryContext, prepared: PreparedAttemptAdmission
    ) -> bool:
        run = prepared.binding.run
        if not await verify_run_input_parent(
            context.connection,
            run.namespace_id,
            run.graph_id,
            run.version_id,
            run.run_id,
            max_parent_bytes=self.limits.preparation.snapshot.inputs.parent,
        ) or not await admit_staging_inventory(context.connection, run.run_id):
            return False
        saved = await read_expiry_run(context)
        if saved is None or saved.started_at is None:
            return False
        try:
            first_claim_deadline(
                run.run_id,
                prepared.binding.attempt_id,
                saved.started_at,
                self.clock(),
                prepared.run_timeout,
                prepared.attempt_timeout,
                timedelta(0),
            )
        except DeadlineRecordRejected:
            return False
        return True

    async def _process(
        self,
        node_id: str,
        periodic_recheck: bool,
        deadline: float,
        stopped: Callable[[], bool] | None,
    ) -> AttemptAdmissionState:
        source: PreparedAdmissionSnapshot | None = None
        binding: ClaimSourceBinding | None = None
        prepared: PreparedAttemptAdmission | None = None
        timer = asyncio.timeout_at(deadline)
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
                agent_only=self.initial_root_only,
            )
            if self.initial_root_only and not any(
                node.path == source.acquired.candidate.node_path
                and node.kind == "agent"
                and not node.predecessors
                and not node.inputs
                for node in source.readiness.nodes
            ):
                return AttemptAdmissionState.REFUSED
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
            if stopped is not None and stopped():
                return AttemptAdmissionState.REFUSED
            require_snapshot_time(deadline)
            async with timer:
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
                    if stopped is not None and stopped():
                        return AttemptAdmissionState.REFUSED
                    context = ExpiryContext(
                        connection,
                        locks,
                        source.checked.scope,
                        binding.run.run_id,
                        self.clock,
                        self.identifiers,
                        limits.promotion,
                        limits.sources.attempt_row,
                    )
                    if self.initial_root_only and not await self._root_source_ready(
                        context, prepared
                    ):
                        return AttemptAdmissionState.REFUSED
                    outcome = await admit_attempt(
                        context,
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
        except TimeoutError:
            if self.initial_root_only and timer.expired():
                return AttemptAdmissionState.REFUSED
            raise
        finally:
            del source, prepared, binding

    async def process(
        self,
        node_id: str,
        *,
        periodic_recheck: bool,
        deadline: float,
        stopped: Callable[[], bool] | None = None,
    ) -> AttemptAdmissionState:
        outcome = "failed"
        try:
            result = await self._process(node_id, periodic_recheck, deadline, stopped)
            outcome = result.value
            return result
        except asyncio.CancelledError as error:
            outcome = "cancelled"
            clear_snapshot_error(error)
            raise
        except (
            AdmissionPreparationUnavailable,
            AttemptAdmissionStale,
            ClaimWireRejected,
            ScopeDeadlineExpired,
        ):
            if self.initial_root_only:
                outcome = AttemptAdmissionState.REFUSED.value
                return AttemptAdmissionState.REFUSED
            raise_safe(AdmissionProcessingFailed())
        except (DatabaseSnapshotRejected, DatabaseWriterRejected) as error:
            if self.initial_root_only and error.reason in (
                DatabaseSnapshotFailure.DEADLINE,
                DatabaseWriterFailure.DEADLINE,
            ):
                outcome = AttemptAdmissionState.REFUSED.value
                return AttemptAdmissionState.REFUSED
            raise_safe(AdmissionProcessingFailed())
        except (
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
            TimeoutError,
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
