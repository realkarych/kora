import asyncio
import hashlib
import os
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Protocol

from pydantic import MISSING, ValidationError

from kora.ir.config import AgentConfig
from kora.ir.json import JsonObject
from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    ProcessDiagnostics,
    SecretAdmissionError,
)
from kora.plugins.runtime.local.capture import OpenedLog
from kora.plugins.runtime.local.channel import LogName, delivery_error
from kora.plugins.runtime.local.command import TRANSFER, attempt_token
from kora.plugins.runtime.local.launch import COMPONENT as LAUNCH_COMPONENT
from kora.plugins.runtime.local.launch import ExpectedBinary, LocalLaunches
from kora.plugins.runtime.local.placements import HarnessPlacements
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.workload_session import (
    Completion,
    Incoming,
    OpenedOutput,
    PhaseName,
    StateName,
)
from kora.plugins.runtime.local.workspace import (
    CONTEXT_NAMESPACE,
    CONTROL,
    Admitted,
    WorkspacePreparation,
    WorkspaceState,
    checkout_admission,
)
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import AgentExecution
from kora.protocols.policy import Policy
from kora.protocols.runtime import (
    CommandObservation,
    SessionResultObservation,
    TransferItem,
    Workload,
)
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import COMPONENT as HOST_COMPONENT
from kora.runner.agent import (
    AgentHost,
    HarnessResolver,
    HostedHarness,
    HostServices,
    SessionLimits,
)
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    ReleaseState,
    open_private_directory,
    release_tree,
    rename_no_replace,
)
from kora.runner.identity import NativeIdentityReader
from kora.runner.journal import RunningJournal
from kora.runner.native_io import observed
from kora.runner.registry import (
    ControlFileRef,
    Submitter,
    synchronize,
    unlink_if_present,
)
from kora.runner.session_result import bounded_read

COMPONENT: Final[str] = CATALOG.register_component("runtime.local.runner")
AGENT: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.agent",
        COMPONENT,
        Level.INFO,
        "the attempt runner handled an agent session start",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.INTEGER},
    )
)
QUIESCENCE: Final[str] = "runner.quiescence"
RESERVED_HARNESS: Final[str] = "fake"
EXIT_RESULT: Final[int] = 0
EXIT_UNSEALED: Final[int] = 1
HONOURED: Final[frozenset[str]] = frozenset({"model", "system_prompt_append"})
UNHONOURED: Final[tuple[str, ...]] = tuple(
    name
    for name, field in AgentConfig.model_fields.items()
    if not field.is_required() and name not in HONOURED | {"extensions"}
)
SCRIPT_LIMIT: Final[int] = 80 * 1_048_576

UNAVAILABLE: Final[str] = "workload_kind_unavailable"


class ScriptIntake(Protocol):
    def receive(self, buffer: bytes) -> None: ...

    def verified(self, buffer: bytes) -> None: ...


class PolicySource(Protocol):
    def policy(self, execution: AgentExecution) -> Policy: ...


class RefusingPolicies:
    def policy(self, execution: AgentExecution) -> Policy:
        raise delivery_error(UNAVAILABLE)


class ProductionResolver:
    def __init__(self, hosted: Mapping[str, HarnessResolver] | None = None) -> None:
        self.hosted: Mapping[str, HarnessResolver] = MappingProxyType(
            {} if hosted is None else dict(hosted)
        )

    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        name = execution.harness
        resolver = None if name == RESERVED_HARNESS else self.hosted.get(name)
        if resolver is None:
            raise delivery_error(UNAVAILABLE)
        return resolver.resolve(execution, workspace)


@dataclass(frozen=True)
class AgentSources:
    resolver: HarnessResolver
    policies: PolicySource
    scripts: ScriptIntake | None = None
    binary: ExpectedBinary | None = None


def agent_admission(workload: Workload) -> Admitted | str:
    admitted = checkout_admission(workload)
    if isinstance(admitted, str):
        return admitted
    if (
        admitted is None
        or workload.kind != "agent"
        or workload.plan_executor != "runner"
        or workload.publish is not None
        or workload.session_spec is None
        or workload.env
        or workload.secret_refs
        or workload.harness_credentials
        or workload.artifact_inputs
        or workload.resources
    ):
        return UNAVAILABLE
    try:
        AgentExecution.model_validate(workload.execution_request)
        SessionLimits.model_validate(workload.limits)
        config = AgentConfig.model_validate(dict(workload.resolved_config))
    except ValidationError:
        return UNAVAILABLE
    if config.extensions or any(
        getattr(config, name) is not MISSING for name in UNHONOURED
    ):
        return UNAVAILABLE
    return admitted


@dataclass(frozen=True)
class _Admitted:
    workload: Workload
    execution: AgentExecution


class _Stage(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"


class AgentSession:
    def __init__(
        self,
        attempt: DirectoryAnchor,
        candidate: str,
        diagnostics: ProcessDiagnostics,
        clock: Clock,
        reaper: AdoptedReaper,
        sources: AgentSources,
        identities: NativeIdentityReader,
    ) -> None:
        self._launch_cleanup: asyncio.Task[None] | None = None
        self._launches: LocalLaunches | None = None
        self._placements: HarnessPlacements | None = None
        self._launch_socket: Path | None = None
        self._identities = identities
        self._reaper = reaper
        self._scope = SecretScope(diagnostics)
        self._attempt = attempt
        self._candidate = candidate
        self._diagnostics = diagnostics
        self._clock = clock
        self._sources = sources
        self._loop = asyncio.get_running_loop()
        self._log = diagnostics.logger(COMPONENT)
        self._journal = RunningJournal(attempt, clock)
        self._workspace = WorkspacePreparation(
            attempt, candidate, self._journal, reaper, clock, self._log
        )
        self._lock = threading.Lock()
        self._starting = threading.Lock()
        self._execution: Submitter | None = None
        self._stage = _Stage.IDLE
        self._refusal = UNAVAILABLE
        self._fenced = False
        self._claimed = False
        self._transferring = False
        self._transfer_ns = 0
        self._script: TransferItem | None = None
        self._script_directory: DirectoryAnchor | None = None
        self._control: DirectoryAnchor | None = None
        self._host: AgentHost | None = None
        self._finished: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None

    def capture_prelaunch(
        self, baseline: dict[int, int], workload: Workload, deadline: int
    ) -> None:
        return None

    def accept(
        self,
        submitter: Submitter,
        prelaunch: dict[int, int] | None,
        workload: Workload,
    ) -> None:
        with self._lock:
            if self._execution is None:
                self._execution = submitter
        admitted = agent_admission(workload)
        if isinstance(admitted, str):
            self._fail(admitted)
            return
        self._workspace.begin(admitted, workload.attempt_id)

    def supervise(
        self,
        submitter: Submitter,
        observational: bool,
        workload: Workload | None,
        authorize: Callable[[], object],
    ) -> None:
        started = self._clock.monotonic_ns()
        with self._starting:
            try:
                claimed = self._claim(submitter, observational, workload)
                if claimed is not None:
                    admitted, workspace = claimed
                    authorize()
                    policy = self._policy(admitted.execution)
                    asyncio.run_coroutine_threadsafe(
                        self._begin(admitted.workload, workspace, policy), self._loop
                    ).result()
            except KoraProtocolError as error:
                journal = error.code == "journal_unavailable"
                self._emit("journal_unavailable" if journal else "refused", started)
                raise
        self._emit("started", started)

    def _claim(
        self, submitter: Submitter, observational: bool, workload: Workload | None
    ) -> tuple[_Admitted, WorkspaceRef] | None:
        with self._lock:
            if self._fenced or self._journal.closed:
                raise delivery_error("journal_unavailable")
            execution = self._execution
            if observational or (execution is not None and submitter != execution):
                raise delivery_error("execution_unauthorized")
            if self._stage is _Stage.RUNNING:
                return None
            if self._stage is _Stage.FAILED:
                raise delivery_error(self._refusal)
        admitted = UNAVAILABLE if workload is None else agent_admission(workload)
        if isinstance(admitted, str) or workload is None:
            code = admitted if isinstance(admitted, str) else UNAVAILABLE
            self._fail(code)
            raise delivery_error(code)
        try:
            self._workspace.gate()
        except KoraProtocolError as error:
            if not error.retryable:
                self._fail(error.code)
            raise
        prepared = self._workspace.prepared
        if prepared is None:
            self._fail("workspace_failed")
            raise delivery_error("workspace_failed")
        with self._lock:
            self._check_claim()
            if self._transferring:
                raise delivery_error("workload_busy")
            self._claimed = True
            if self._execution is None:
                self._execution = submitter
        self._verify_script()
        workspace = WorkspaceRef(
            attempt_id=workload.attempt_id,
            root=self._candidate,
            repo=prepared.repo,
            commit=prepared.commit,
            base_commit=prepared.base_commit,
            branch=prepared.branch,
        )
        execution = AgentExecution.model_validate(workload.execution_request)
        return _Admitted(workload, execution), workspace

    def _check_claim(self) -> None:
        if self._fenced or self._journal.closed:
            raise delivery_error("journal_unavailable")
        if self._stage is _Stage.FAILED:
            raise delivery_error(self._refusal)

    def _policy(self, execution: AgentExecution) -> Policy:
        try:
            return self._sources.policies.policy(execution)
        except KoraProtocolError as error:
            self._fail(error.code)
            raise

    def _fail(self, code: str) -> None:
        with self._lock:
            self._stage = _Stage.FAILED
            self._refusal = code

    async def _begin(
        self, workload: Workload, workspace: WorkspaceRef, policy: Policy
    ) -> None:
        if self._fenced:
            raise delivery_error("journal_unavailable")
        try:
            control = self._control_anchor()
            launches = self._prepare_launches(control)
        except (OSError, FilesystemError, KoraProtocolError, SecretAdmissionError):
            self._fail("launch_rejected")
            try:
                await self._close_launches()
            except (OSError, FilesystemError, KoraProtocolError) as cleanup:
                raise delivery_error("launch_rejected") from cleanup
            raise delivery_error("launch_rejected") from None
        services = HostServices(
            launches=launches.bind,
            resolver=self._sources.resolver,
            policy=policy,
            ctx=self._context(workload.attempt_id),
            registry=self._diagnostics.registry,
            clock=self._clock,
            log=self._diagnostics.logger(HOST_COMPONENT),
        )
        host = AgentHost(services, self._journal, control, self._candidate)
        self._host = host
        try:
            host.start(workload, workspace)
        except KoraProtocolError as error:
            self._fail(error.code)
            try:
                await self._close_launches()
            except (OSError, FilesystemError, KoraProtocolError) as cleanup:
                raise error from cleanup
            raise
        with self._lock:
            self._stage = _Stage.RUNNING
        self._finished = self._loop.create_task(self._session_finished(host))

    async def _session_finished(self, host: AgentHost) -> None:
        try:
            await host.finished()
        finally:
            await self._close_launches()

    async def _close_launches(self) -> None:
        if self._launch_cleanup is None:
            self._launch_cleanup = self._loop.create_task(self._release_launches())
        cancelled = await observed(self._launch_cleanup)
        self._launch_cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _release_launches(self) -> None:
        try:
            if self._launches is not None:
                try:
                    await self._launches.close()
                finally:
                    control = self._control
                    if control is not None:
                        progress = release_tree(control, "launch")
                        if progress.state is not ReleaseState.COMPLETE:
                            raise FilesystemError("launch_cleanup_incomplete")
        finally:
            try:
                if self._placements is not None:
                    self._placements.close()
            finally:
                self._scope.release()

    def bind_launch_socket(self, path: Path) -> None:
        self._launch_socket = path

    def launches(self) -> LocalLaunches | None:
        return self._launches

    def _prepare_launches(self, control: DirectoryAnchor) -> LocalLaunches:
        socket_path = self._launch_socket
        if socket_path is None:
            raise delivery_error("launch_rejected")
        candidate = Path(self._candidate)
        placements = HarnessPlacements(self._attempt, candidate)
        self._placements = placements
        token = attempt_token(self._attempt, self._scope, "launch_rejected")
        self._launches = LocalLaunches(
            placements.paths,
            control,
            candidate.parent / "control",
            socket_path,
            self._sources.binary,
            token,
            self._clock,
            self._identities,
            self._diagnostics.logger(LAUNCH_COMPONENT),
            self._reaper,
        )
        return self._launches

    def _context(self, attempt_id: str) -> ProtocolContext:
        return ProtocolContext(
            namespace_id=CONTEXT_NAMESPACE,
            run_id=None,
            attempt_id=attempt_id,
            logger=self._log,
            secrets=None,
            secret_admission=None,
            request_digest=None,
            deadline=None,
            clock=self._clock,
            activity=NoActivity(),
        )

    def _emit(self, outcome: str, started: int) -> None:
        waited = max(0, (self._clock.monotonic_ns() - started) // 1_000_000)
        level = Level.INFO if outcome == "started" else Level.WARNING
        self._log.emit_at(level, AGENT, outcome=outcome, waited_ms=waited)

    def attach_drains(self) -> None:
        return None

    def status(self) -> tuple[StateName, PhaseName, int | None]:
        finished = self._finished
        host = self._host
        if finished is None or host is None:
            return "starting", "preparation", None
        if not finished.done():
            return "running", "execution", None
        if isinstance(host.observation(), SessionResultObservation):
            return "exited", "complete", EXIT_RESULT
        return "exited", "failed", EXIT_UNSEALED

    def _owned(self, submitter: Submitter) -> None:
        with self._lock:
            if self._execution != submitter:
                raise delivery_error("execution_unauthorized")

    def _control_anchor(self) -> DirectoryAnchor:
        with self._lock:
            if self._control is None:
                try:
                    os.mkdir(CONTROL, mode=0o700, dir_fd=self._attempt.descriptor)
                except FileExistsError:
                    pass
                self._control = open_private_directory(self._attempt, CONTROL)
            return self._control

    def _verify_script(self) -> None:
        scripts = self._sources.scripts
        if scripts is None:
            return
        item = self._script
        directory = self._script_directory
        if item is None or directory is None:
            self._fail(UNAVAILABLE)
            raise delivery_error(UNAVAILABLE)
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            descriptor = os.open(item.name, flags, dir_fd=directory.descriptor)
            try:
                entry = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_nlink != 1
                    or entry.st_uid != os.getuid()
                    or stat.S_IMODE(entry.st_mode) != 0o400
                    or entry.st_size != item.size
                ):
                    raise delivery_error("integrity_error")
                buffer = bounded_read(descriptor, item.size)
            finally:
                os.close(descriptor)
            if (
                len(buffer) != item.size
                or "sha256:" + hashlib.sha256(buffer).hexdigest() != item.sha256
            ):
                raise delivery_error("integrity_error")
            scripts.verified(buffer)
        except (OSError, FilesystemError, ValidationError, KoraProtocolError):
            self._fail("integrity_error")
            raise delivery_error("integrity_error") from None

    def decide_transfer(
        self, submitter: Submitter, observational: bool, item: TransferItem
    ) -> Literal["write", "repeat", "conflict"]:
        self._owned(submitter)
        if self._sources.scripts is None:
            raise delivery_error(UNAVAILABLE)
        started = self._clock.monotonic_ns()
        try:
            with self._lock:
                if self._fenced or self._journal.closed:
                    raise delivery_error("journal_unavailable")
                if (
                    self._claimed
                    or self._stage is _Stage.FAILED
                    or self._transferring
                    or item.kind != "script"
                    or item.name != "script.json"
                    or item.size > SCRIPT_LIMIT
                ):
                    raise delivery_error("transfer_refused")
                if self._script is not None and self._script != item:
                    raise delivery_error("transfer_refused")
                self._transferring = True
                self._transfer_ns = started
                return "repeat" if self._script is not None else "write"
        except KoraProtocolError as error:
            self._emit_transfer(
                "journal_unavailable"
                if error.code == "journal_unavailable"
                else "refused",
                started,
            )
            raise

    def open_incoming(self) -> Incoming:
        control = self._control_anchor()
        try:
            os.mkdir("script", mode=0o700, dir_fd=control.descriptor)
        except FileExistsError:
            pass
        directory = open_private_directory(control, "script")
        try:
            unlink_if_present(directory, ".incoming")
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(".incoming", flags, 0o600, dir_fd=directory.descriptor)
        except OSError:
            directory.close()
            raise
        return Incoming(directory, descriptor)

    def discard_incoming(self, incoming: Incoming) -> None:
        try:
            try:
                os.close(incoming.descriptor)
            finally:
                unlink_if_present(incoming.directory, ".incoming")
        finally:
            incoming.directory.close()

    def _append_transfer(self, kind: str, item: TransferItem) -> None:
        try:
            if self._journal.append(
                kind, {"name": item.name, "size": item.size, "sha256": item.sha256}
            ):
                return
        except FilesystemError:
            self.fence_transfer()
            self._emit_transfer("journal_unavailable")
            raise delivery_error("journal_unavailable") from None
        self.fence_transfer()
        self._emit_transfer("journal_unavailable")
        raise delivery_error("journal_unavailable")

    def _reject_script(self, item: TransferItem, code: str) -> None:
        self._fail(code)
        self._append_transfer("transfer.rejected", item)
        self._emit_transfer("rejected")

    def finish_incoming(
        self,
        incoming: Incoming,
        item: TransferItem,
        digest: str,
        authorize: Callable[[], object],
    ) -> None:
        authorize()
        scripts = self._sources.scripts
        if scripts is None:
            raise delivery_error(UNAVAILABLE)
        try:
            if digest != item.sha256:
                raise delivery_error("integrity_error")
            buffer = os.pread(incoming.descriptor, item.size + 1, 0)
            if len(buffer) != item.size:
                raise delivery_error("integrity_error")
            scripts.receive(buffer)
        except KoraProtocolError as error:
            unlink_if_present(incoming.directory, ".incoming")
            self._reject_script(item, error.code)
            raise
        directory = incoming.directory
        anchor = DirectoryAnchor(
            os.dup(directory.descriptor), directory.identity, directory.ancestors
        )
        created = False
        committed = False
        try:
            os.fchmod(incoming.descriptor, 0o400)
            synchronize(incoming.descriptor)
            authorize()
            rename_no_replace(directory, ".incoming", item.name)
            created = True
            synchronize(directory.descriptor)
            authorize()
            self._append_transfer("transfer.materialised", item)
            committed = True
        finally:
            if not committed:
                anchor.close()
                if created:
                    self._discard_placed(directory, item.name)
        self._script_directory = anchor
        self._script = item
        self._emit_transfer("materialised")

    def _discard_placed(self, directory: DirectoryAnchor, name: str) -> None:
        try:
            unlink_if_present(directory, name)
            synchronize(directory.descriptor)
        except (OSError, FilesystemError):
            self.fence_transfer()

    def complete_seen(
        self,
        decision: Literal["repeat", "conflict"],
        item: TransferItem,
        digest: str,
    ) -> None:
        if digest != item.sha256:
            self._reject_script(item, "integrity_error")
            raise delivery_error("integrity_error")

    def _emit_transfer(self, outcome: str, started: int | None = None) -> None:
        beginning = self._transfer_ns if started is None else started
        waited = max(0, (self._clock.monotonic_ns() - beginning) // 1_000_000)
        level = Level.INFO if outcome == "materialised" else Level.WARNING
        self._log.emit_at(level, TRANSFER, outcome=outcome, waited_ms=waited)

    def end_transfer(self) -> None:
        with self._lock:
            self._transferring = False

    def fence_transfer(self) -> None:
        self._journal.close()

    def open_output(self, submitter: Submitter, name: str) -> OpenedOutput:
        self._owned(submitter)
        raise delivery_error(UNAVAILABLE)

    def open_log(self, submitter: Submitter, which: LogName, offset: int) -> OpenedLog:
        self._owned(submitter)
        raise delivery_error(UNAVAILABLE)

    def confirm_output(
        self, opened: OpenedOutput, active: Callable[[], None] | None = None
    ) -> None:
        raise delivery_error(UNAVAILABLE)

    def read_output(self, opened: OpenedOutput, offset: int, length: int) -> bytes:
        raise delivery_error(UNAVAILABLE)

    def note_output(self, outcome: str, started: int) -> None:
        return None

    def command_observation(self, submitter: Submitter) -> CommandObservation:
        self._owned(submitter)
        raise delivery_error(UNAVAILABLE)

    def session_observation(self, submitter: Submitter) -> ControlFileRef:
        self._owned(submitter)
        finished = self._finished
        if finished is None or not finished.done():
            with self._lock:
                settled = (
                    self._fenced
                    or self._stage is _Stage.FAILED
                    or self._journal.closed
                    or self._workspace.state is WorkspaceState.FAILED
                )
            raise delivery_error("session_unobserved" if settled else "workload_busy")
        reference = self.result_reference()
        if reference is None:
            raise delivery_error("session_unobserved")
        return reference

    def completion(self) -> Completion:
        finished = self._finished
        if finished is None or not finished.done():
            return Completion.PENDING
        return Completion.COMPLETE

    def result_reference(self) -> ControlFileRef | None:
        host = self._host
        sealed = None if host is None else host.result_reference()
        if sealed is None:
            return None
        return ControlFileRef(name=sealed.name, size=sealed.size, digest=sealed.digest)

    def record_completion(self, data: JsonObject) -> bool:
        return self._journal.append(QUIESCENCE, data)

    def fence(self) -> None:
        with self._lock:
            self._fenced = True
        if self._host is not None:
            self._host.fence()
        self._workspace.cancel()

    def close_journal(self) -> None:
        self._journal.close()

    async def stop(self) -> None:
        if self._closing is None:
            self._closing = self._loop.create_task(self._release())
        cancelled = await observed(self._closing)
        self._closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _release(self) -> None:
        self.fence()
        self.close_journal()
        try:
            try:
                try:
                    if self._launches is not None:
                        await self._launches.close()
                finally:
                    finished = self._finished
                    if self._host is not None:
                        released = await self._host.stop()
                        if finished is not None and released.session:
                            await asyncio.wait({finished})
            finally:
                try:
                    await self._close_launches()
                finally:
                    await self._workspace.settle()
        finally:
            if self._script_directory is not None:
                self._script_directory.close()
            if self._control is not None:
                self._control.close()
        finished = self._finished
        if finished is not None and finished.done() and not finished.cancelled():
            finished.result()
