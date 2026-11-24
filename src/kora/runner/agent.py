import asyncio
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol

from pydantic import Field, StrictInt, StrictStr, StringConstraints, ValidationError

from kora.ir.cost import PriceRevision
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.observability.redaction import SecretRegistry
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.deadlines import (
    NANOSECONDS_PER_SECOND,
    ExecutionDeadline,
    LocalThresholds,
    thresholds,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    AgentExecution,
    HarnessAdapter,
    Launch,
    LaunchContext,
    LaunchInputs,
    Placement,
    SessionResult,
    SessionSpec,
)
from kora.protocols.identity_records import MAX_COUNT
from kora.protocols.policy import Policy, PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.runtime import (
    SessionFailedObservation,
    SessionObservation,
    SessionResultObservation,
    Workload,
)
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.calls import TRACKING_CEILING
from kora.runner.filesystem import DirectoryAnchor, FilesystemError
from kora.runner.journal import SYNC_DEADLINE_NS, JournalAppendOutcome
from kora.runner.liveness import watch_liveness
from kora.runner.native_io import (
    NativeExecutor,
    NativeIOBusy,
    NativeIOClosed,
    NativeIOUnresolved,
)
from kora.runner.session_result import (
    FinalizationFacts,
    FinalizationImplementation,
    FinalizationOrigin,
    PreparedFinalization,
    PreparedObservation,
    ResultFile,
    digest_of,
    discard_observation,
    prepare_finalization,
    prepare_observation,
    read_observation,
    write_finalization,
    write_observation,
)
from kora.runner.session_result_encoding import ResultPreparationStopped, check_running
from kora.runner.sink import JournalWriter, RunnerSink, settled_by

INTERRUPT_GRACE_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND
KILL_SETTLE_NS: Final[int] = 2 * NANOSECONDS_PER_SECOND
CANCEL_SETTLE_NS: Final[int] = NANOSECONDS_PER_SECOND
STOP_DEADLINE_NS: Final[int] = 10 * NANOSECONDS_PER_SECOND
COMPONENT: Final[str] = CATALOG.register_component("runner.agent")
SESSION_STARTED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runner.agent.session_started",
        COMPONENT,
        Level.INFO,
        "the runner started an agent session",
        {},
    )
)
SESSION_FINISHED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runner.agent.session_finished",
        COMPONENT,
        Level.INFO,
        "the runner sealed an agent session result",
        {
            "cause": FieldKind.STRING,
            "stop_reason": FieldKind.STRING,
            "journal_complete": FieldKind.BOOLEAN,
        },
    )
)
SESSION_FAILED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runner.agent.session_failed",
        COMPONENT,
        Level.WARNING,
        "an agent session ended without a sealed result",
        {"code": FieldKind.STRING},
    )
)
JOURNAL_GAP: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runner.agent.journal_gap",
        COMPONENT,
        Level.WARNING,
        "adapter records of an agent session were not journaled",
        {"omitted": FieldKind.INTEGER},
    )
)
STOP_UNSETTLED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runner.agent.stop_unsettled",
        COMPONENT,
        Level.WARNING,
        "an agent session stop reached its bound",
        {"session": FieldKind.BOOLEAN, "writer": FieldKind.BOOLEAN},
    )
)


def refused(code: str) -> KoraProtocolError:
    return KoraProtocolError(code, "the agent workload is refused", False, {})


FINALIZATION: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runner.agent.finalization",
        COMPONENT,
        Level.DEBUG,
        "the runner checked the isolated finalization snapshot",
        {"outcome": FieldKind.STRING},
    )
)


class SessionLimits(BoundaryModel):
    stall_after_us: Annotated[
        StrictStr, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,18})$")
    ]
    max_events: Annotated[StrictInt, Field(ge=1, le=TRACKING_CEILING)] | None = None


@dataclass(frozen=True)
class AdmittedSession:
    execution: AgentExecution
    spec: SessionSpec
    max_events: int | None
    stall_after_ns: int
    deadline: ExecutionDeadline | None


@dataclass(frozen=True)
class HostedHarness:
    adapter: HarnessAdapter
    kill: Callable[[], None]
    no_capture: FinalizationImplementation | None = None


class HarnessResolver(Protocol):
    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness: ...


class BoundPolicy:
    def __init__(self, policy: Policy, ctx: ProtocolContext) -> None:
        self._policy = policy
        self._ctx = ctx

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision:
        return await self._policy.decide(request, layers, protected_paths, self._ctx)


class RefusingLaunches:
    placements: Mapping[Placement, Path] = MappingProxyType({})

    async def allocate(self, inputs: LaunchInputs) -> Launch:
        raise KoraProtocolError(
            "launch_rejected", "the runner allocates no harness launch", False, {}
        )


def refusing_launches(_sink: RunnerSink) -> LaunchContext:
    return RefusingLaunches()


ResultPreparation = Callable[
    [SessionObservation, tuple[str, ...], threading.Event], PreparedObservation
]
PreparationOutcome = PreparedObservation | Literal["stopped"]


def invoke_preparation(
    operation: Callable[[], PreparedObservation],
) -> PreparationOutcome:
    try:
        return operation()
    except ResultPreparationStopped:
        return "stopped"


class NativeResultWork:
    def __init__(self) -> None:
        self._executor: NativeExecutor | None = NativeExecutor(workers=1, admitted=1)
        self.stop = threading.Event()

    @property
    def settled(self) -> bool:
        return self._executor is None

    async def prepare(
        self, operation: Callable[[], PreparedObservation]
    ) -> PreparationOutcome:
        executor = self._executor
        if executor is None:
            raise NativeIOClosed
        return await executor.run_nowait(lambda: invoke_preparation(operation))

    async def close(self) -> None:
        executor = self._executor
        if executor is None:
            return
        try:
            await executor.close()
        except NativeIOUnresolved:
            if not executor.joined:
                raise
        finally:
            if executor.joined:
                self._executor = None


@dataclass(frozen=True)
class HostServices:
    resolver: HarnessResolver
    policy: Policy
    ctx: ProtocolContext
    registry: SecretRegistry
    clock: Clock
    log: ComponentLogger
    launches: Callable[[RunnerSink], LaunchContext] = refusing_launches
    secret_scope: SecretScope | None = None
    prepare_result: ResultPreparation = prepare_observation


@dataclass(frozen=True)
class Session:
    sink: RunnerSink
    hosted: HostedHarness
    run: "asyncio.Task[SessionResult]"
    supervised: "asyncio.Task[None]"
    timer: "asyncio.Task[None]"
    deadline: LocalThresholds | None


@dataclass
class MeasuredSession:
    origin: FinalizationOrigin
    implementation: FinalizationImplementation
    spec: SessionSpec
    spec_digest: str
    duration_ms: int | None = None

    async def run(
        self, clock: Clock, operation: Callable[[], Awaitable[SessionResult]]
    ) -> SessionResult:
        start = clock.monotonic_ns()
        result = await operation()
        elapsed = clock.monotonic_ns() - start
        if 0 <= elapsed < (MAX_COUNT + 1) * 1_000_000:
            self.duration_ms = elapsed // 1_000_000
        return result

    def facts(self, result: SessionResult) -> FinalizationFacts | None:
        if (
            self.duration_ms is None
            or result.harness != self.implementation.name
            or result.installed_version != self.implementation.version
        ):
            return None
        try:
            price = PriceRevision.model_validate(self.spec.price_table)
        except ValidationError:
            return None
        if price.id != self.spec.price_table_id:
            return None
        origin = self.origin
        return FinalizationFacts(
            attempt_id=origin.attempt_id,
            execution_domain_id=origin.execution_domain_id,
            submitter=origin.submitter,
            supervisor=origin.supervisor,
            request_digest=origin.request_digest,
            session_spec_digest=self.spec_digest,
            implementation=self.implementation,
            capture="unavailable",
            duration_ms=self.duration_ms,
            price_table_id=price.id,
        )


@dataclass(frozen=True)
class Released:
    session: bool
    writer: bool


class AgentHost:
    def __init__(
        self,
        services: HostServices,
        journal: JournalWriter,
        control: DirectoryAnchor,
        candidate: str,
    ) -> None:
        self._services = services
        self._journal = journal
        self._control = control
        self._candidate = candidate
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._fenced = asyncio.Event()
        self._sealed: ResultFile | None = None
        self._finalization: ResultFile | None = None
        self._finalization_cancelled = False
        self._has_result = False
        self._result_work: NativeResultWork | None = None

    def start(
        self,
        workload: Workload,
        workspace: WorkspaceRef,
        *,
        origin: FinalizationOrigin | None = None,
    ) -> None:
        if self._session is not None or self._fenced.is_set():
            raise refused("workload_busy")
        admitted = self.admitted(workload, workspace)
        services = self._services
        deadline = (
            thresholds(admitted.deadline, services.clock.sample())
            if admitted.deadline is not None
            else None
        )
        if (
            deadline is not None
            and deadline.interrupt_at_ns <= services.clock.monotonic_ns()
        ):
            raise refused("deadline_expired")
        spec = admitted.spec
        digest = digest_of(canonical_bytes(spec.model_dump(mode="json")))
        recorded = self._journal.append_outcome(
            "runner.session_spec", {"digest": digest}
        )
        if recorded is not JournalAppendOutcome.DURABLE:
            raise KoraProtocolError(
                "journal_unavailable", "the session spec was not recorded", True, {}
            )
        hosted = services.resolver.resolve(admitted.execution, workspace)
        sink = RunnerSink(
            self._journal,
            services.registry,
            services.clock,
            admitted.max_events,
            admitted.stall_after_ns,
        )
        policy = BoundPolicy(services.policy, services.ctx)
        try:
            launches = services.launches(sink)
        except BaseException:
            sink.close()
            raise
        measurement = (
            None
            if origin is None or hosted.no_capture is None
            else MeasuredSession(origin, hosted.no_capture, spec, digest)
        )
        run = asyncio.create_task(
            hosted.adapter.run(spec, policy, sink, launches)
            if measurement is None
            else measurement.run(
                services.clock, lambda: hosted.adapter.run(spec, policy, sink, launches)
            )
        )
        timer = asyncio.create_task(
            watch_liveness(
                sink, services.clock, admitted.stall_after_ns, deadline, run.done
            )
        )
        supervised = asyncio.create_task(
            self._supervise(sink, hosted, run, timer, measurement)
        )
        self._session = Session(sink, hosted, run, supervised, timer, deadline)
        services.log.emit(SESSION_STARTED)

    def admitted(self, workload: Workload, workspace: WorkspaceRef) -> AdmittedSession:
        if workload.kind != "agent":
            raise refused("workload_kind_unavailable")
        try:
            execution = AgentExecution.model_validate(workload.execution_request)
            limits = SessionLimits.model_validate(workload.limits)
        except ValidationError:
            raise refused("agent_execution_invalid") from None
        spec = workload.session_spec
        if spec is None:
            raise refused("session_spec_missing")
        if (
            workspace.root != self._candidate
            or workspace.attempt_id != workload.attempt_id
        ):
            raise refused("workspace_untrusted")
        return AdmittedSession(
            execution,
            spec,
            limits.max_events,
            int(limits.stall_after_us) * 1000,
            workload.deadline,
        )

    async def _supervise(
        self,
        sink: RunnerSink,
        hosted: HostedHarness,
        run: "asyncio.Task[SessionResult]",
        timer: "asyncio.Task[None]",
        measurement: MeasuredSession | None,
    ) -> None:
        stopping = asyncio.ensure_future(sink.termination())
        try:
            await asyncio.wait(
                {run, stopping, timer}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopping.cancel()
            await self._cancel_timer(timer)
        ended = await self._ended(hosted, run)
        clock = self._services.clock
        complete = await sink.settle(clock.monotonic_ns() + SYNC_DEADLINE_NS)
        if sink.omitted():
            self._services.log.emit(JOURNAL_GAP, omitted=sink.omitted())
        if not ended:
            await self._finalize(
                SessionFailedObservation(
                    kind="session_failed", code="kill_unconfirmed"
                ),
                sink,
                fixed=True,
            )
            return
        try:
            result = run.result()
        except KoraProtocolError as error:
            await self._finalize(
                SessionFailedObservation(kind="session_failed", code=error.code), sink
            )
            return
        await self._finalize(
            SessionResultObservation(
                kind="result",
                result=result,
                cause=sink.cause(),
                journal_complete=complete,
            ),
            sink,
            measurement=measurement,
        )

    async def _cancel_timer(self, timer: "asyncio.Task[None]") -> None:
        if not timer.cancelling():
            timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not timer.cancelled() or (current is not None and current.cancelling()):
                raise

    async def _ended(
        self, hosted: HostedHarness, run: "asyncio.Task[SessionResult]"
    ) -> bool:
        clock = self._services.clock
        if not run.done() and not self._fenced.is_set():
            await self._graced(run)
        if run.done():
            return True
        hosted.kill()
        if await settled_by(run, clock, clock.monotonic_ns() + KILL_SETTLE_NS):
            return True
        run.cancel()
        await settled_by(run, clock, clock.monotonic_ns() + CANCEL_SETTLE_NS)
        return False

    async def _graced(self, run: "asyncio.Task[SessionResult]") -> None:
        clock = self._services.clock
        now = clock.monotonic_ns()
        deadline = now + INTERRUPT_GRACE_NS
        thresholds = self._started().deadline
        if thresholds is not None:
            deadline = min(deadline, thresholds.escalate_at_ns)
        if deadline <= now:
            return
        fenced = asyncio.ensure_future(self._fenced.wait())
        grace = asyncio.ensure_future(clock.sleep_until(deadline))
        try:
            await asyncio.wait(
                {run, fenced, grace}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            fenced.cancel()
            grace.cancel()

    def _result_deadline_passed(self) -> bool:
        session = self._session
        deadline = None if session is None else session.deadline
        return (
            deadline is not None
            and self._services.clock.monotonic_ns() >= deadline.interrupt_at_ns
        )

    def _result_deadline_due(self, sink: RunnerSink) -> bool:
        if not self._result_deadline_passed():
            return False
        sink.terminate("deadline")
        return True

    async def _stop_expired_result(
        self, deadline: LocalThresholds, work: NativeResultWork, sink: RunnerSink
    ) -> None:
        await self._services.clock.sleep_until(deadline.interrupt_at_ns)
        sink.terminate("deadline")
        work.stop.set()

    async def _finalize(
        self,
        observation: SessionObservation,
        sink: RunnerSink,
        *,
        fixed: bool = False,
        measurement: MeasuredSession | None = None,
    ) -> None:
        log = self._services.log
        with self._lock:
            if self._fenced.is_set():
                log.emit(SESSION_FAILED, code="fenced")
                return
            if self._result_deadline_due(sink):
                log.emit(SESSION_FAILED, code="deadline_expired")
                return
            if self._result_work is not None:
                raise RuntimeError("the session result already has an owner")
            work = NativeResultWork()
            self._result_work = work
        deadline = self._started().deadline
        timer = (
            None
            if deadline is None
            else asyncio.create_task(self._stop_expired_result(deadline, work, sink))
        )
        try:
            scope = self._services.secret_scope
            forms = () if fixed or scope is None else scope.result_spellings()
            try:
                prepared = await work.prepare(
                    lambda: self._prepare_result(
                        observation, measurement, forms, work.stop
                    )
                )
            finally:
                forms = ()
            if prepared == "stopped":
                code = "fenced" if self._fenced.is_set() else "deadline_expired"
                log.emit(SESSION_FAILED, code=code)
            else:
                try:
                    self._seal(prepared, sink, measure=measurement is not None)
                finally:
                    del prepared
        except (NativeIOBusy, NativeIOClosed, NativeIOUnresolved):
            log.emit(SESSION_FAILED, code="result_unwritten")
        finally:
            try:
                if timer is not None:
                    await self._cancel_timer(timer)
            finally:
                await work.close()

    def _prepare_result(
        self,
        observation: SessionObservation,
        measurement: MeasuredSession | None,
        forms: tuple[str, ...],
        stop: threading.Event,
    ) -> PreparedObservation:
        prepared = self._services.prepare_result(observation, forms, stop)
        if (
            prepared.failure is not None
            or measurement is None
            or not isinstance(observation, SessionResultObservation)
        ):
            return prepared
        check_running(stop)
        facts = measurement.facts(observation.result)
        check_running(stop)
        if facts is None:
            return prepared
        return prepare_finalization(prepared, facts, forms, stop)

    async def drain_result_work(self) -> None:
        work = self._result_work
        if work is not None:
            await work.close()

    def result_work_settled(self) -> bool:
        work = self._result_work
        return work is None or work.settled

    def _seal(
        self, prepared: PreparedObservation, sink: RunnerSink, *, measure: bool
    ) -> None:
        log = self._services.log
        with self._lock:
            if self._fenced.is_set():
                log.emit(SESSION_FAILED, code="fenced")
                return
            if self._result_deadline_due(sink):
                log.emit(SESSION_FAILED, code="deadline_expired")
                return
            try:
                written = write_observation(self._control, prepared)
            except (OSError, FilesystemError):
                log.emit(SESSION_FAILED, code="result_unwritten")
                return
            if self._result_deadline_due(sink):
                log.emit(SESSION_FAILED, code="deadline_expired")
                return
            outcome = self._journal.append_outcome(*journal_record(prepared, written))
            if outcome is not JournalAppendOutcome.DURABLE:
                self._discard()
                log.emit(SESSION_FAILED, code="result_unsealed")
                return
            if self._fenced.is_set():
                log.emit(SESSION_FAILED, code="fenced")
                return
            if self._result_deadline_due(sink):
                log.emit(SESSION_FAILED, code="deadline_expired")
                return
            self._sealed = written
            self._has_result = prepared.failure is None
        if prepared.failure is not None:
            log.emit(SESSION_FAILED, code=prepared.failure)
            return
        log.emit(
            SESSION_FINISHED,
            cause=prepared.cause or "none",
            stop_reason=prepared.stop_reason,
            journal_complete=prepared.journal_complete,
        )

        if measure:
            self._services.log.emit(
                FINALIZATION,
                outcome=self._seal_finalization(prepared.finalization),
            )

    def _seal_finalization(self, prepared: PreparedFinalization | None) -> str:
        with self._lock:
            if (
                prepared is None
                or self._fenced.is_set()
                or self._finalization_cancelled
                or self._result_deadline_passed()
            ):
                return "unobserved"
            try:
                reference = write_finalization(self._control, prepared)
            except (OSError, FilesystemError):
                return "unwritten"
            if (
                self._fenced.is_set()
                or self._finalization_cancelled
                or self._result_deadline_passed()
            ):
                return "unobserved"
            self._finalization = reference
            return "sealed"

    def _discard(self) -> None:
        try:
            discard_observation(self._control)
        except (OSError, FilesystemError):
            self._services.log.emit(SESSION_FAILED, code="result_undiscarded")

    async def cancel(self) -> None:
        session = self._started()
        with self._lock:
            self._finalization_cancelled = True
        session.sink.terminate("cancel")
        await asyncio.wait({session.supervised})

    def fence(self) -> None:
        with self._lock:
            self._fenced.set()
            work = self._result_work
            if work is not None:
                work.stop.set()
            session = self._session
            if session is None:
                return
            session.sink.fence()
            session.sink.terminate("cancel")
            session.hosted.kill()

    async def stop(self) -> Released:
        self.fence()
        session = self._session
        if session is None:
            return Released(True, True)
        await self._cancel_timer(session.timer)
        clock = self._services.clock
        supervised = await settled_by(
            session.supervised, clock, clock.monotonic_ns() + STOP_DEADLINE_NS
        )
        settled = supervised and session.run.done()
        session.sink.close()
        writer = await session.sink.joined(clock.monotonic_ns() + SYNC_DEADLINE_NS)
        released = Released(settled, writer)
        if not (released.session and released.writer):
            self._services.log.emit(
                STOP_UNSETTLED, session=released.session, writer=released.writer
            )
        return released

    async def finished(self) -> None:
        await asyncio.shield(self._started().supervised)

    def result_reference(self) -> ResultFile | None:
        with self._lock:
            return self._sealed

    def finalization_reference(self) -> ResultFile | None:
        with self._lock:
            return self._finalization

    def result_publication_expired(self) -> bool:
        with self._lock:
            return self._sealed is None and self._result_deadline_passed()

    def has_result_observation(self) -> bool:
        with self._lock:
            return self._has_result

    def observation(self) -> SessionObservation | None:
        reference = self.result_reference()
        return None if reference is None else read_observation(self._control, reference)

    def _started(self) -> Session:
        if self._session is None:
            raise refused("session_not_started")
        return self._session


def journal_record(
    prepared: PreparedObservation, written: ResultFile
) -> tuple[str, JsonObject]:
    data: JsonObject = {"digest": written.digest, "size": written.size}
    if prepared.failure is not None:
        return "runner.session_failed", {**data, "code": prepared.failure}
    return "runner.session_result", data
