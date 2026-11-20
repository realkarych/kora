import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Protocol

from pydantic import Field, StrictInt, StrictStr, StringConstraints, ValidationError

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
from kora.protocols.policy import Policy, PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.runtime import (
    SessionFailedObservation,
    SessionObservation,
    SessionResultObservation,
    Workload,
)
from kora.protocols.time import Clock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.calls import TRACKING_CEILING
from kora.runner.filesystem import DirectoryAnchor, FilesystemError
from kora.runner.journal import SYNC_DEADLINE_NS, JournalAppendOutcome
from kora.runner.liveness import watch_liveness
from kora.runner.session_result import (
    ResultFile,
    ResultTooLarge,
    digest_of,
    discard_observation,
    write_observation,
)
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


@dataclass(frozen=True)
class HostServices:
    resolver: HarnessResolver
    policy: Policy
    ctx: ProtocolContext
    registry: SecretRegistry
    clock: Clock
    log: ComponentLogger
    launches: Callable[[RunnerSink], LaunchContext] = refusing_launches


@dataclass(frozen=True)
class Session:
    sink: RunnerSink
    hosted: HostedHarness
    run: "asyncio.Task[SessionResult]"
    supervised: "asyncio.Task[None]"
    timer: "asyncio.Task[None]"
    deadline: LocalThresholds | None


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
        self._observation: SessionObservation | None = None

    def start(self, workload: Workload, workspace: WorkspaceRef) -> None:
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
        run = asyncio.create_task(hosted.adapter.run(spec, policy, sink, launches))
        timer = asyncio.create_task(
            watch_liveness(
                sink, services.clock, admitted.stall_after_ns, deadline, run.done
            )
        )
        supervised = asyncio.create_task(self._supervise(sink, hosted, run, timer))
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
            self._seal(
                SessionFailedObservation(kind="session_failed", code="kill_unconfirmed")
            )
            return
        try:
            result = run.result()
        except KoraProtocolError as error:
            self._seal(SessionFailedObservation(kind="session_failed", code=error.code))
            return
        self._seal(
            SessionResultObservation(
                kind="result",
                result=result,
                cause=sink.cause(),
                journal_complete=complete,
            )
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

    def _seal(self, observation: SessionObservation) -> None:
        log = self._services.log
        with self._lock:
            if self._fenced.is_set():
                log.emit(SESSION_FAILED, code="fenced")
                return
            try:
                sealed, written = self._written(observation)
            except (OSError, FilesystemError):
                log.emit(SESSION_FAILED, code="result_unwritten")
                return
            outcome = self._journal.append_outcome(*journal_record(sealed, written))
            if outcome is not JournalAppendOutcome.DURABLE:
                self._discard()
                log.emit(SESSION_FAILED, code="result_unsealed")
                return
            self._sealed = written
            self._observation = sealed
        if isinstance(sealed, SessionFailedObservation):
            log.emit(SESSION_FAILED, code=sealed.code)
            return
        log.emit(
            SESSION_FINISHED,
            cause=sealed.cause or "none",
            stop_reason=sealed.result.stop_reason,
            journal_complete=sealed.journal_complete,
        )

    def _written(
        self, observation: SessionObservation
    ) -> tuple[SessionObservation, ResultFile]:
        try:
            return observation, write_observation(self._control, observation)
        except ResultTooLarge:
            failed = SessionFailedObservation(
                kind="session_failed", code="result_too_large"
            )
            return failed, write_observation(self._control, failed)

    def _discard(self) -> None:
        try:
            discard_observation(self._control)
        except (OSError, FilesystemError):
            self._services.log.emit(SESSION_FAILED, code="result_undiscarded")

    async def cancel(self) -> None:
        session = self._started()
        session.sink.terminate("cancel")
        await asyncio.wait({session.supervised})

    def fence(self) -> None:
        with self._lock:
            self._fenced.set()
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

    def observation(self) -> SessionObservation | None:
        with self._lock:
            return self._observation

    def _started(self) -> Session:
        if self._session is None:
            raise refused("session_not_started")
        return self._session


def journal_record(
    observation: SessionObservation, written: ResultFile
) -> tuple[str, JsonObject]:
    data: JsonObject = {"digest": written.digest, "size": written.size}
    if isinstance(observation, SessionFailedObservation):
        return "runner.session_failed", {**data, "code": observation.code}
    return "runner.session_result", data
