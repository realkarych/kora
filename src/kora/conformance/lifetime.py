import asyncio
import os
import signal
import sys
import threading
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeAlias, TypeVar, cast

from pydantic import TypeAdapter

from kora.conformance.controllers import (
    HELPER_ORIGIN_BYTES,
    CleanupPermit,
    HelperInventory,
    HelperOrigin,
    MutationOwner,
    duration_ns,
)
from kora.conformance.diagnostics import LifetimeDiagnostics
from kora.conformance.io_helper import (
    CreateTreeMutation,
    HelperError,
    InstalledIOHelperLauncher,
    IOHelperLauncher,
    OpenTreeMutation,
    RemoveTreeMutation,
)
from kora.conformance.journal import JournalEntry, JournalSnapshot
from kora.protocols.common import BoundaryModel
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    Acquisition,
    ActivationAcknowledgement,
    ActivationResult,
    BorrowedServiceRegistration,
    BorrowedServiceRequest,
    CleanupOutcome,
    CleanupReference,
    ClosureReport,
    LifetimeLimits,
    LifetimeReason,
    LocalRuntimeLaunch,
    OperationId,
    OriginalOutcome,
    ProcessLaunch,
    ProcessRegistration,
    Reservation,
    ReservationRequest,
    ReservationToken,
    ResourceId,
    ResourceKind,
    ResourceObservation,
    ResourceState,
    TemporaryTreeRegistration,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import ChannelError
from kora.runner.filesystem import DirectoryAnchor, DirectoryIdentity, FilesystemError
from kora.runner.gate import ExecutionConfig
from kora.runner.identity import IdentityReader, IdentityState, NativeIdentityReader
from kora.runner.processes import (
    GatedProcess,
    ProcessCondition,
    ProcessError,
    ProcessObservation,
    RecoveredProcess,
    complete_identity,
    start_gated,
)

Request: TypeAlias = (
    ProcessLaunch | LocalRuntimeLaunch | TemporaryTreeRequest | BorrowedServiceRequest
)
Result: TypeAlias = Reservation | Acquisition | ActivationAcknowledgement
OPERATION_ID: TypeAdapter[OperationId] = TypeAdapter(OperationId)
CLOSURE_REPORT_BYTES: int = 16384
CLOSURE_CONTROL_BYTES: int = CLOSURE_REPORT_BYTES + 2048
CLEANUP_DRIVER_BYTES: int = 4096
MEMBER_CONTROL_BYTES: int = 2048
RECOVERY_ATTEMPT_BYTES: int = 4096
OwnedResult = TypeVar("OwnedResult")


def request_size(request: BoundaryModel | None) -> tuple[bool, int]:
    if request is None:
        return False, 0
    execution = isinstance(request, (ProcessLaunch, LocalRuntimeLaunch))
    size = (
        len(ExecutionConfig.from_launch(request).encode()[1])
        if isinstance(request, ProcessLaunch)
        else len(request.model_dump_json().encode())
    )
    return execution, size


def consume_closure_failure(future: asyncio.Future[ClosureReport]) -> None:
    if not future.cancelled():
        future.exception()


def closure_future() -> asyncio.Future[ClosureReport]:
    future: asyncio.Future[ClosureReport] = asyncio.get_running_loop().create_future()
    future.add_done_callback(consume_closure_failure)
    return future


@dataclass
class CleanupControl:
    result: asyncio.Future[ClosureReport] = field(default_factory=closure_future)
    start: int | None = None
    cooperative: int = 0
    term: int = 0
    killed: int = 0
    final: int = 0
    selected: set[str] = field(default_factory=set[str])
    published: bool = False
    failed: bool = False
    removing_root: bool = False
    terminal_join: bool = False


@dataclass
class CleanupEffect:
    task: asyncio.Task[None]
    control: CleanupControl
    permit: CleanupPermit


@dataclass(eq=False)
class OrdinarySlot:
    wire: bool
    task: asyncio.Task[Result] | None = None


class LifetimeError(Exception):
    def __init__(self, reason: LifetimeReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


async def owned_result(task: asyncio.Task[OwnedResult]) -> OwnedResult:
    if not task.done():
        await asyncio.wait((task,))
    if task.cancelled():
        raise asyncio.CancelledError
    error = task.exception()
    if isinstance(error, LifetimeError):
        raise LifetimeError(error.reason)
    return task.result()


@dataclass
class ResourceRecord:
    entry: JournalEntry
    token: ReservationToken
    process: GatedProcess | None = None
    recovered: RecoveredProcess | None = None
    directory: DirectoryAnchor | None = None
    acquisition: Acquisition | None = None
    dependency: ResourceId | None = None
    fenced: bool = False
    observed_live_processes: int = 0
    observed_remaining_pipes: int = 0
    cleanup: CleanupControl | None = None
    signalled: int = 0
    member_capacity: int = 0


@dataclass
class RecordedOperation:
    request: BoundaryModel
    task: asyncio.Task[Result]
    resource_id: ResourceId | None


@dataclass(frozen=True)
class LifetimeInventory:
    resources: int
    pending_operations: int
    retained_processes: int
    retained_trees: int
    endpoint_count: int
    control_bytes: int
    execution_bytes: int
    ordinary_owners: int
    cleanup_driver_tasks: int
    cleanup_effect_tasks: int
    closure_results: int
    member_capacity: int
    pending_process_readiness: int
    helpers: HelperInventory


def consume_task_failure(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


class ParentLifetime:
    def __init__(
        self,
        *,
        limits: LifetimeLimits | None = None,
        clock: Clock | None = None,
        identities: IdentityReader | None = None,
        identity_source: Callable[[], str] = new_operation_id,
        launcher: IOHelperLauncher | None = None,
        recovery: CleanupReference | None = None,
    ) -> None:
        self.limits = limits if limits is not None else LifetimeLimits()
        self.clock = clock if clock is not None else SystemClock()
        self.identities = (
            identities
            if identities is not None
            else NativeIdentityReader(clock=self.clock)
        )
        observed = self.identities.read(os.getpid())
        if (
            observed.state is not IdentityState.PRESENT
            or observed.process is None
            or not complete_identity(observed.process.identity)
        ):
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        self.owner = observed.process.identity
        self.identity_source = identity_source
        self.invocation_id = (
            identity_source() if recovery is None else recovery.invocation_id
        )
        self.generation = 1
        self.revision = 1
        self.original_outcome = OriginalOutcome.COMPLETE
        self.admission_open = True
        self.bootstrap_succeeded = False
        self.records: dict[ResourceId, ResourceRecord] = {}
        self.operations: dict[tuple[str, str], RecordedOperation] = {}
        self.ordinary_slots: set[OrdinarySlot] = set()
        self.closures: dict[ResourceId | None, asyncio.Future[ClosureReport]] = {}
        self.control_bytes = (
            CLOSURE_CONTROL_BYTES
            + CLEANUP_DRIVER_BYTES
            + (self.limits.max_outstanding_operations + 1) * HELPER_ORIGIN_BYTES
        )
        if recovery is not None:
            self.control_bytes += RECOVERY_ATTEMPT_BYTES
        if self.control_bytes > self.limits.max_control_recovery_bytes:
            raise LifetimeError(LifetimeReason.CAPACITY_EXCEEDED)
        self.invocation_cleanup = CleanupControl()
        self.cleanup_changed = asyncio.Event()
        self.cleanup_effect: CleanupEffect | None = None
        self.bootstrap_finished: int | None = None
        self.execution_bytes = 0
        self.transport_control_bytes = 0
        self.transport_execution_bytes = 0
        self.direct_control_bytes = 0
        self.direct_execution_bytes = 0
        self.transport_pending = 0
        self.endpoint_count = 0
        self.closure_started = asyncio.Event()
        self.cooperative_deadline_ns: int | None = None
        self.borrowed_scopes: dict[str, ResourceId] = {}
        self.mutations = MutationOwner(
            launcher if launcher is not None else InstalledIOHelperLauncher(),
            self.clock,
            self.limits,
            self.cleanup_changed,
        )
        self.diagnostics = LifetimeDiagnostics(self.invocation_id)
        self._bootstrap = asyncio.create_task(
            self._establish() if recovery is None else self._recover(recovery)
        )
        self._bootstrap.add_done_callback(consume_task_failure)
        self._bootstrap.add_done_callback(self._bootstrap_complete)
        self._cleanup_driver = asyncio.create_task(self._drive_cleanup())
        self._cleanup_driver.add_done_callback(self._driver_complete)

    def _driver_complete(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            for _, control in self._cleanup_controls():
                if control.start is not None and not control.result.done():
                    control.result.set_exception(error)

    def _bootstrap_complete(self, task: asyncio.Task[None]) -> None:
        self.bootstrap_finished = self.clock.monotonic_ns()
        self.cleanup_changed.set()

    def deadline(self) -> int:
        admission = CALL_ADMISSION.get()
        if admission is not None and admission.owner is self:
            return admission.deadline
        return self.clock.monotonic_ns() + duration_ns(self.limits.acquisition_seconds)

    async def _establish(self) -> None:
        await self.mutations.bootstrap(self.snapshot(), self.deadline())
        self.bootstrap_succeeded = True
        self.diagnostics.emit("started", LifetimeReason.COMPLETE, 0)

    async def _recover(self, reference: CleanupReference) -> None:
        self.admission_open = False
        deadline = self.deadline()
        snapshot = await self.mutations.recover(reference, self.owner, deadline)
        self.generation = snapshot.generation
        self.revision = snapshot.revision
        self.original_outcome = snapshot.original_outcome
        for entry in snapshot.entries:
            token = ReservationToken(
                invocation_id=self.invocation_id,
                resource_id=entry.resource_id,
                owner=self.owner,
                generation=self.generation,
            )
            record = ResourceRecord(entry, token, fenced=True)
            self.records[entry.resource_id] = record
        if (
            self.control_bytes
            + self.mutations.inventory().journal_bytes
            + len(self.records) * CLOSURE_CONTROL_BYTES
            > self.limits.max_control_recovery_bytes
        ):
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        for record in self.records.values():
            self._reserve_cleanup_control()
            record.cleanup = CleanupControl()
        for record in self.records.values():
            entry = record.entry
            if entry.state is ResourceState.CLOSED:
                continue
            if isinstance(entry.registration, ProcessRegistration):
                record.member_capacity = self._reserve_process_members()
                record.recovered = RecoveredProcess(
                    entry.registration,
                    self.clock,
                    self.identities,
                    max_members=record.member_capacity,
                )
            elif isinstance(entry.registration, BorrowedServiceRegistration):
                record.dependency = entry.registration.connection_scope
        for record in self.records.values():
            entry = record.entry
            if entry.state is ResourceState.CLOSED or entry.registration is None:
                if (
                    entry.state is not ResourceState.RESERVED
                    and entry.state is not ResourceState.CLOSED
                ):
                    raise LifetimeError(LifetimeReason.EFFECT_UNSETTLED)
                continue
            if isinstance(entry.registration, TemporaryTreeRegistration):
                registration = entry.registration
                parent = (
                    self.mutations.directory
                    if entry.parent_resource_id is None
                    else self.records[entry.parent_resource_id].directory
                )
                if parent is None:
                    raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
                identity = DirectoryIdentity(
                    name=registration.name,
                    device=registration.device,
                    inode=registration.inode,
                    owner_uid=registration.owner_uid,
                )
                result = await self.mutations.perform(
                    OpenTreeMutation(directory=parent.identity, target=identity),
                    parent,
                    deadline,
                )
                record.directory = self.mutations.take_directory(result)
            elif not isinstance(
                entry.registration, (BorrowedServiceRegistration, ProcessRegistration)
            ):
                raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        self.bootstrap_succeeded = True
        self.diagnostics.emit("recovery", LifetimeReason.COMPLETE, len(self.records))

    async def ready(self) -> None:
        try:
            await owned_result(self._bootstrap)
        except (
            HelperError,
            ChannelError,
            ProcessError,
            FilesystemError,
            OSError,
        ) as error:
            self.admission_open = False
            raise LifetimeError(LifetimeReason.JOURNAL_UNAVAILABLE) from error

    async def _ready_deadline(self, deadline: int) -> None:
        if self.clock.monotonic_ns() >= deadline:
            raise self._refuse(LifetimeReason.EFFECT_UNSETTLED)
        if not self._bootstrap.done():
            timeout = asyncio.create_task(self.clock.sleep_until(deadline))
            try:
                await asyncio.wait(
                    (self._bootstrap, timeout), return_when=asyncio.FIRST_COMPLETED
                )
                if not self._bootstrap.done() or self.clock.monotonic_ns() >= deadline:
                    raise self._refuse(LifetimeReason.EFFECT_UNSETTLED)
            finally:
                timeout.cancel()
                try:
                    await timeout
                except asyncio.CancelledError:
                    if not timeout.cancelled():
                        raise
        await self.ready()

    def admit_ordinary(self, *, wire: bool) -> OrdinarySlot:
        if len(self.ordinary_slots) >= self.limits.max_outstanding_operations:
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        slot = OrdinarySlot(wire)
        self.ordinary_slots.add(slot)
        return slot

    def release_wire(self, slot: OrdinarySlot) -> None:
        slot.wire = False
        self._release_slot(slot)

    def _release_slot(self, slot: OrdinarySlot) -> None:
        if not slot.wire and (slot.task is None or slot.task.done()):
            self.ordinary_slots.discard(slot)

    @property
    def input_control_bytes(self) -> int:
        return self.transport_control_bytes + self.direct_control_bytes

    @property
    def input_execution_bytes(self) -> int:
        return self.transport_execution_bytes + self.direct_execution_bytes

    def _reserve_direct_input(self, request: BoundaryModel | None) -> "RequestInput":
        execution, size = request_size(request)
        control = 0 if execution else size
        execution_bytes = size if execution else 0
        if (
            self.control_bytes
            + self.input_control_bytes
            + self.mutations.inventory().journal_bytes
            + control
            > self.limits.max_control_recovery_bytes
            or self.execution_bytes + self.input_execution_bytes + execution_bytes
            > self.limits.max_execution_snapshot_bytes
        ):
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        self.direct_control_bytes += control
        self.direct_execution_bytes += execution_bytes
        return RequestInput(self, None, control, execution_bytes)

    @contextmanager
    def _ordinary_call(
        self, deadline: int, request: BoundaryModel | None = None
    ) -> Generator[OrdinarySlot]:
        admission = CALL_ADMISSION.get()
        inherited = admission is not None and admission.owner is self
        slot = (
            admission.slot
            if inherited and admission is not None
            else self.admit_ordinary(wire=False)
        )
        charge = None
        token = None
        try:
            if not inherited:
                charge = self._reserve_direct_input(request)
                token = CALL_ADMISSION.set(CallAdmission(self, slot, deadline, charge))
            yield slot
        finally:
            if token is not None:
                CALL_ADMISSION.reset(token)
            if charge is not None:
                charge.transfer()
            self._release_slot(slot)

    def snapshot(self) -> JournalSnapshot:
        previous = self.mutations.snapshot
        return JournalSnapshot(
            invocation_id=self.invocation_id,
            owner=self.owner,
            generation=self.generation,
            revision=self.revision,
            admission_open=self.admission_open,
            original_outcome=self.original_outcome,
            claim=None if previous is None else previous.claim,
            root=None if previous is None else previous.root,
            directory=None if previous is None else previous.directory,
            ancestors=() if previous is None else previous.ancestors,
            entries=tuple(record.entry for record in self.records.values()),
            pending_effects=tuple(
                record.entry.acquisition_id
                for record in self.records.values()
                if record.entry.acquisition_id is not None
                and record.entry.registration is None
                and record.entry.state is not ResourceState.CLOSED
            ),
        )

    async def _persist(self, deadline: int) -> None:
        self.revision += 1
        snapshot = self.snapshot()
        if (
            len(snapshot.model_dump_json().encode())
            + self.control_bytes
            + self.input_control_bytes
            > self.limits.max_control_recovery_bytes
        ):
            self.admission_open = False
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        try:
            await self.mutations.publish(snapshot, deadline)
        except (
            HelperError,
            ChannelError,
            ProcessError,
            FilesystemError,
            OSError,
        ) as error:
            self.admission_open = False
            raise LifetimeError(LifetimeReason.JOURNAL_UNAVAILABLE) from error

    def _refuse(self, reason: LifetimeReason) -> LifetimeError:
        self.diagnostics.emit("refused", reason, len(self.records))
        return LifetimeError(reason)

    def _record(self, token: ReservationToken) -> ResourceRecord:
        record = self.records.get(token.resource_id)
        if record is None or record.token != token:
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        return record

    def _admit(self, record: ResourceRecord | None = None) -> None:
        if not self.admission_open or (record is not None and record.fenced):
            raise self._refuse(LifetimeReason.EFFECT_UNSETTLED)

    def _operation(
        self,
        key: tuple[str, str],
        request: BoundaryModel,
        resource_id: ResourceId | None,
        coroutine: Coroutine[object, object, Result],
        slot: OrdinarySlot | None = None,
    ) -> asyncio.Task[Result]:
        admission = CALL_ADMISSION.get()
        if slot is None and admission is not None and admission.owner is self:
            slot = admission.slot
        existing = self.operations.get(key)
        if existing is not None:
            coroutine.close()
            if existing.request != request:
                raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
            return existing.task
        if (
            admission is not None
            and admission.owner is self
            and admission.input_charge is not None
        ):
            admission.input_charge.transfer()
        execution, size = request_size(request)
        if (
            self.execution_bytes + self.input_execution_bytes + size
            > self.limits.max_execution_snapshot_bytes
            if execution
            else self.control_bytes
            + self.input_control_bytes
            + self.mutations.inventory().journal_bytes
            + size
            > self.limits.max_control_recovery_bytes
        ):
            coroutine.close()
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        try:
            admitted = self.admit_ordinary(wire=False) if slot is None else slot
        except LifetimeError:
            coroutine.close()
            raise
        if admitted not in self.ordinary_slots or admitted.task is not None:
            coroutine.close()
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        if execution:
            self.execution_bytes += size
        else:
            self.control_bytes += size
        task = asyncio.create_task(self._run_operation(coroutine))
        admitted.task = task
        task.add_done_callback(consume_task_failure)
        task.add_done_callback(self._operation_complete)
        self.operations[key] = RecordedOperation(request, task, resource_id)
        return task

    def _operation_complete(self, task: asyncio.Task[Result]) -> None:
        for slot in tuple(self.ordinary_slots):
            if slot.task is task:
                self._release_slot(slot)
        self.cleanup_changed.set()

    def _reserve_cleanup_control(self) -> None:
        if (
            self.control_bytes
            + self.input_control_bytes
            + self.mutations.inventory().journal_bytes
            + CLOSURE_CONTROL_BYTES
            > self.limits.max_control_recovery_bytes
        ):
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        self.control_bytes += CLOSURE_CONTROL_BYTES

    def _reserve_process_members(self) -> int:
        available = (
            self.limits.max_control_recovery_bytes
            - self.control_bytes
            - self.input_control_bytes
            - self.mutations.inventory().journal_bytes
            - self.limits.max_control_frame_bytes
            - CLOSURE_CONTROL_BYTES
        )
        capacity = min(
            65536,
            available
            // ((self.limits.max_outstanding_operations + 1) * MEMBER_CONTROL_BYTES),
        )
        if capacity < 1:
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        self.control_bytes += capacity * MEMBER_CONTROL_BYTES
        return capacity

    async def _run_operation(
        self, coroutine: Coroutine[object, object, Result]
    ) -> Result:
        try:
            return await coroutine
        except (
            HelperError,
            ChannelError,
            ProcessError,
            FilesystemError,
            OSError,
        ) as error:
            raise self._refuse(LifetimeReason.ACQUISITION_FAILED) from error

    async def reserve(self, request: ReservationRequest) -> Reservation:
        deadline = self.deadline()
        with self._ordinary_call(deadline, request) as slot:
            return await self._reserve_call(request, deadline, slot)

    async def _reserve_call(
        self,
        request: ReservationRequest,
        deadline: int,
        slot: OrdinarySlot | None = None,
    ) -> Reservation:
        await self._ready_deadline(deadline)
        task = self._operation(
            ("reserve", request.operation_id),
            request,
            request.parent_resource_id,
            self._reserve(request, deadline),
            slot,
        )
        return cast(Reservation, await owned_result(task))

    async def _reserve(self, request: ReservationRequest, deadline: int) -> Result:
        self._admit()
        if request.kind is ResourceKind.LOCAL_RUNTIME_DOMAIN:
            raise self._refuse(LifetimeReason.ACQUISITION_FAILED)
        if len(self.records) >= self.limits.max_resource_records:
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        if request.parent_resource_id is not None:
            parent = self.records.get(request.parent_resource_id)
            if parent is None:
                raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
            self._admit(parent)
        resource_id = self.identity_source()
        if resource_id == self.invocation_id or resource_id in self.records:
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        token = ReservationToken(
            invocation_id=self.invocation_id,
            resource_id=resource_id,
            owner=self.owner,
            generation=self.generation,
        )
        entry = JournalEntry(
            resource_id=resource_id,
            parent_resource_id=request.parent_resource_id,
            kind=request.kind,
            state=ResourceState.RESERVED,
            revision=self.revision + 1,
        )
        self._reserve_cleanup_control()
        record = ResourceRecord(entry, token, cleanup=CleanupControl())
        self.records[resource_id] = record
        await self._persist(deadline)
        return Reservation(
            token=token,
            kind=request.kind,
            revision=entry.revision,
            state=ResourceState.RESERVED,
        )

    async def acquire(
        self, token: ReservationToken, operation_id: OperationId, request: Request
    ) -> Acquisition:
        deadline = self.deadline()
        with self._ordinary_call(deadline, request) as slot:
            return await self._acquire_call(
                token, operation_id, request, deadline, slot
            )

    async def _acquire_call(
        self,
        token: ReservationToken,
        operation_id: OperationId,
        request: Request,
        deadline: int,
        slot: OrdinarySlot | None = None,
    ) -> Acquisition:
        await self._ready_deadline(deadline)
        operation_id = OPERATION_ID.validate_python(operation_id)
        record = self._record(token)
        task = self._operation(
            ("acquire", operation_id),
            request,
            token.resource_id,
            self._acquire(record, operation_id, request, deadline),
            slot,
        )
        existing = self.operations[("acquire", operation_id)]
        if existing.resource_id != token.resource_id:
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        return cast(Acquisition, await owned_result(task))

    async def _acquire(
        self, record: ResourceRecord, operation_id: str, request: Request, deadline: int
    ) -> Result:
        self._admit(record)
        if (
            record.entry.state is not ResourceState.RESERVED
            or request.kind != record.entry.kind
        ):
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        directory: DirectoryAnchor | None = None
        if isinstance(request, ProcessLaunch):
            working = self.records.get(request.working_directory_resource_id)
            if working is None or working.directory is None:
                raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
            self._admit(working)
            record.dependency = working.entry.resource_id
            directory = working.directory
        elif isinstance(request, TemporaryTreeRequest):
            if request.parent_resource_id != record.entry.parent_resource_id:
                raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
            parent = (
                None
                if request.parent_resource_id is None
                else self.records.get(request.parent_resource_id)
            )
            directory = self.mutations.directory if parent is None else parent.directory
            if directory is None:
                raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
        elif isinstance(request, BorrowedServiceRequest):
            scope = self.borrowed_scopes.get(request.service_reference)
            if (
                scope is None
                or scope != record.entry.parent_resource_id
                or self.records[scope].process is None
            ):
                raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
            record.dependency = scope
        else:
            raise self._refuse(LifetimeReason.ACQUISITION_FAILED)
        record.entry = record.entry.model_copy(
            update={
                "state": ResourceState.ACQUIRING,
                "revision": self.revision + 1,
                "acquisition_id": operation_id,
                "tree_name": record.entry.resource_id
                if isinstance(request, TemporaryTreeRequest)
                else None,
            }
        )
        await self._persist(deadline)
        if record.fenced or not self.admission_open:
            record.entry = record.entry.model_copy(
                update={"state": ResourceState.CLOSING}
            )
            raise self._refuse(LifetimeReason.EFFECT_UNSETTLED)
        if isinstance(request, ProcessLaunch) and directory is not None:
            record.member_capacity = self._reserve_process_members()
            record.process = start_gated(
                request,
                operation_id,
                directory.descriptor,
                deadline,
                self.clock,
                self.identities,
                max_members=record.member_capacity,
            )
            registration = await record.process.prepare()
        elif isinstance(request, TemporaryTreeRequest) and directory is not None:
            result = await self.mutations.perform(
                CreateTreeMutation(
                    directory=directory.identity, name=record.entry.resource_id
                ),
                directory,
                deadline,
            )
            if result.directory is None:
                raise self._refuse(LifetimeReason.JOURNAL_UNAVAILABLE)
            record.directory = self.mutations.take_directory(result)
            registration = TemporaryTreeRegistration(
                resource_id=record.entry.resource_id,
                parent_resource_id=request.parent_resource_id or self.invocation_id,
                name=record.entry.resource_id,
                device=result.directory.device,
                inode=result.directory.inode,
                owner_uid=result.directory.owner_uid,
            )
        elif isinstance(request, BorrowedServiceRequest):
            registration = BorrowedServiceRegistration(
                service_reference=request.service_reference,
                connection_scope=self.borrowed_scopes[request.service_reference],
            )
        else:
            raise self._refuse(LifetimeReason.ACQUISITION_FAILED)
        acquisition = Acquisition(
            token=record.token,
            operation_id=operation_id,
            revision=self.revision + 1,
            registration=registration,
        )
        record.acquisition = acquisition
        record.entry = record.entry.model_copy(
            update={
                "state": ResourceState.CLOSING
                if record.fenced
                else ResourceState.GATED,
                "revision": acquisition.revision,
                "registration": registration,
            }
        )
        await self._persist(deadline)
        if record.fenced or not self.admission_open:
            raise self._refuse(LifetimeReason.EFFECT_UNSETTLED)
        return acquisition

    async def acknowledge(
        self,
        token: ReservationToken,
        acquisition_operation_id: OperationId,
        revision: int,
    ) -> ActivationAcknowledgement:
        deadline = self.deadline()
        request = ActivationAcknowledgement(
            token=token,
            acquisition_operation_id=acquisition_operation_id,
            revision=revision,
            result=ActivationResult.ACTIVE,
        )
        with self._ordinary_call(deadline, request) as slot:
            return await self._acknowledge_call(request, deadline, slot)

    async def _acknowledge_call(
        self,
        request: ActivationAcknowledgement,
        deadline: int,
        slot: OrdinarySlot | None = None,
    ) -> ActivationAcknowledgement:
        await self._ready_deadline(deadline)
        record = self._record(request.token)
        task = self._operation(
            ("acknowledge", request.acquisition_operation_id),
            request,
            request.token.resource_id,
            self._acknowledge(record, request, deadline),
            slot,
        )
        return cast(ActivationAcknowledgement, await owned_result(task))

    async def _acknowledge(
        self, record: ResourceRecord, request: ActivationAcknowledgement, deadline: int
    ) -> Result:
        acquisition = record.acquisition
        if (
            acquisition is None
            or acquisition.operation_id != request.acquisition_operation_id
            or acquisition.revision != request.revision
        ):
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        if record.fenced or not self.admission_open:
            return request.model_copy(update={"result": ActivationResult.CLOSING})
        record.entry = record.entry.model_copy(
            update={"state": ResourceState.ACTIVE, "revision": self.revision + 1}
        )
        await self._persist(deadline)
        if record.fenced or not self.admission_open:
            return request.model_copy(update={"result": ActivationResult.CLOSING})
        if record.process is not None:
            await record.process.acknowledge()
        self.diagnostics.emit("activated", LifetimeReason.COMPLETE, len(self.records))
        return request

    def bind_borrowed_service(self, process_resource_id: ResourceId) -> str:
        record = self.records[process_resource_id]
        self._admit(record)
        if record.process is None or record.entry.state is not ResourceState.ACTIVE:
            raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
        if len(self.borrowed_scopes) >= self.limits.max_resource_records:
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        reference = self.identity_source()
        self.borrowed_scopes[reference] = process_resource_id
        return reference

    def _selected(self, resource_id: ResourceId | None) -> set[str]:
        if resource_id is None:
            return set(self.records)
        if resource_id not in self.records:
            raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
        selected = {resource_id}
        while True:
            expanded = selected | {
                key
                for key, record in self.records.items()
                if record.entry.parent_resource_id in selected
                or record.dependency in selected
            }
            if expanded == selected:
                return selected
            selected = expanded

    async def observe(
        self, resource_id: ResourceId | None
    ) -> tuple[ResourceObservation, ...]:
        deadline = self.deadline()
        with self._ordinary_call(deadline):
            return await self._observe_call(resource_id, deadline)

    async def _observe_call(
        self, resource_id: ResourceId | None, deadline: int
    ) -> tuple[ResourceObservation, ...]:
        await self._ready_deadline(deadline)
        return tuple(
            self._observation(self.records[key], deadline)
            for key in self._selected(resource_id)
        )

    def _observation(
        self, record: ResourceRecord, deadline: int
    ) -> ResourceObservation:
        if record.process is not None and record.process.closed:
            record.observed_live_processes = 0
            record.observed_remaining_pipes = 0
        if record.recovered is not None and record.recovered.closed:
            record.observed_live_processes = 0
        if self.clock.monotonic_ns() < deadline:
            if record.process is not None and not record.process.closed:
                observed = record.process.observe(deadline)
                record.observed_live_processes = len(observed.live_members)
                record.observed_remaining_pipes = int(not observed.capture_eof)
            if record.recovered is not None and not record.recovered.closed:
                record.observed_live_processes = len(
                    record.recovered.observe(deadline).live_members
                )
        return ResourceObservation(
            resource_id=record.entry.resource_id,
            kind=record.entry.kind,
            revision=record.entry.revision,
            state=record.entry.state,
            pending_acquisition_count=sum(
                not operation.task.done()
                and operation.resource_id == record.entry.resource_id
                for operation in self.operations.values()
            ),
            live_owned_process_count=record.observed_live_processes,
            remaining_pipe_count=record.observed_remaining_pipes,
            remaining_tree_count=int(
                record.directory is not None and not record.directory.closed
            ),
            reason=record.entry.reason,
        )

    def set_original_outcome(self, outcome: OriginalOutcome) -> None:
        if self.original_outcome is OriginalOutcome.COMPLETE:
            self.original_outcome = outcome
        if outcome is OriginalOutcome.CANCELLED:
            self.diagnostics.emit(
                "cancelled", LifetimeReason.CANCELLED, len(self.records)
            )

    def request_close(
        self, resource_id: ResourceId | None
    ) -> asyncio.Future[ClosureReport]:
        control = (
            self.invocation_cleanup
            if resource_id is None
            else self.records[resource_id].cleanup
            if resource_id in self.records
            else None
        )
        if control is None:
            raise self._refuse(LifetimeReason.IDENTITY_UNKNOWN)
        if control.start is not None:
            return control.result
        start = self.clock.monotonic_ns()
        control.start = start
        control.cooperative = start + duration_ns(self.limits.cooperative_seconds)
        control.term = control.cooperative + duration_ns(self.limits.term_seconds)
        control.killed = control.term + duration_ns(
            self.limits.kill_observation_seconds
        )
        control.final = control.killed + duration_ns(self.limits.drain_seconds)
        control.selected = self._selected(resource_id)
        if resource_id is None:
            self.admission_open = False
            self.cooperative_deadline_ns = control.cooperative
            self.closure_started.set()
        self._fence(control.selected)
        self.closures[resource_id] = control.result
        if resource_id is not None and self.invocation_cleanup.result.done():
            control.final = min(control.final, self.invocation_cleanup.final)
            self._finish_terminal_join(resource_id, control)
            return control.result
        control.terminal_join = (
            resource_id is not None and self.invocation_cleanup.removing_root
        )
        self._tighten_helper_constraints()
        self.cleanup_changed.set()
        return control.result

    def _finish_terminal_join(
        self, resource_id: ResourceId, control: CleanupControl
    ) -> None:
        full = self.invocation_cleanup.result.result()
        self._finish_cleanup(
            resource_id,
            control,
            safe=full.state is CleanupOutcome.CLOSED
            and all(
                self.records[key].entry.state is ResourceState.CLOSED
                for key in control.selected
            ),
        )

    def _fence(self, selected: set[str]) -> None:
        for key in selected:
            record = self.records[key]
            record.fenced = True
            if record.process is not None:
                record.process.close_gate()

    async def close(self, resource_id: ResourceId | None) -> ClosureReport:
        return await asyncio.shield(self.request_close(resource_id))

    def _cleanup_controls(self) -> tuple[tuple[ResourceId | None, CleanupControl], ...]:
        return (
            (None, self.invocation_cleanup),
            *(
                (key, record.cleanup)
                for key, record in self.records.items()
                if record.cleanup is not None
            ),
        )

    def _cleanup_pending(self, control: CleanupControl) -> bool:
        return not self._bootstrap.done() or any(
            not operation.task.done()
            and (
                operation.resource_id is None
                or operation.resource_id in control.selected
            )
            for operation in self.operations.values()
        )

    def _cleanup_done(self, task: asyncio.Task[None]) -> None:
        consume_task_failure(task)
        self.cleanup_changed.set()

    def _start_cleanup_effect(
        self,
        control: CleanupControl,
        resource_id: ResourceId | None,
        effect: Coroutine[object, object, None],
    ) -> None:
        permit = CleanupPermit(resource_id, control.final)
        task = asyncio.create_task(effect)
        self.cleanup_effect = CleanupEffect(task, control, permit)
        self.mutations.cleanup_task = task
        self.mutations.cleanup_permit = permit
        self._tighten_helper_constraints()
        task.add_done_callback(self._cleanup_done)

    def _owns_helper(
        self,
        resource_id: ResourceId | None,
        control: CleanupControl,
        origin: HelperOrigin,
    ) -> bool:
        if resource_id is None:
            return True
        if origin.cleanup is not None:
            return origin.cleanup.resource_id in control.selected
        return any(
            operation.task is origin.task and operation.resource_id in control.selected
            for operation in self.operations.values()
        )

    def _tighten_helper_constraints(self) -> None:
        effect = self.cleanup_effect
        for resource_id, control in self._cleanup_controls():
            if control.start is None:
                continue
            if effect is not None and (
                resource_id is None or effect.permit.resource_id in control.selected
            ):
                effect.permit.final = min(effect.permit.final, control.final)
            for origin in self.mutations.origins.values():
                if self._owns_helper(resource_id, control, origin):
                    origin.tighten(
                        control.cooperative, control.term, control.final, self.limits
                    )

    def _signal_cleanup_helpers(self) -> None:
        for helper, origin in self.mutations.origins.items():
            now = self.clock.monotonic_ns()
            if (
                helper.closed
                or helper.anchor.exited
                or origin.final is None
                or now >= origin.final
            ):
                continue
            stage = (
                2
                if origin.kill is not None and now >= origin.kill
                else 1
                if origin.term is not None and now >= origin.term
                else 0
            )
            if stage > origin.signalled and helper.signal(
                signal.SIGKILL if stage == 2 else signal.SIGTERM,
                origin.final,
            ):
                origin.signalled = stage
                self.diagnostics.emit(
                    "escalated", LifetimeReason.RESOURCE_LIVE, len(self.records)
                )

    async def _remove_cleanup_tree(self, record: ResourceRecord, deadline: int) -> None:
        directory = record.directory
        parent_id = record.entry.parent_resource_id
        parent = (
            self.mutations.directory
            if parent_id is None
            else self.records[parent_id].directory
        )
        if directory is None or parent is None:
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        await self.mutations.perform(
            RemoveTreeMutation(directory=parent.identity, target=directory.identity),
            parent,
            deadline,
        )
        self.mutations.prune()
        directory.close()

    async def _publish_cleanup(self, control: CleanupControl) -> None:
        await self._persist(control.final)
        control.published = True

    async def _remove_cleanup_root(self, control: CleanupControl) -> None:
        root = self.mutations.root
        directory = self.mutations.directory
        if root is None or directory is None:
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        await self.mutations.perform(
            RemoveTreeMutation(directory=root.identity, target=directory.identity),
            root,
            control.final,
        )
        self.mutations.prune()
        self.mutations.release()

    def _cleanup_processes(
        self, records: list[ResourceRecord], control: CleanupControl, *, release: bool
    ) -> bool:
        settled = True
        now = self.clock.monotonic_ns()
        requested = (
            signal.SIGKILL
            if now >= control.term
            else signal.SIGTERM
            if now >= control.cooperative
            else None
        )
        escalation = 2 if requested is signal.SIGKILL else 1
        for record in records:
            process = record.process or record.recovered
            if process is None or process.closed:
                continue
            if self.clock.monotonic_ns() >= control.final:
                return False
            observed = process.observe(control.final)
            record.observed_live_processes = len(observed.live_members)
            captured = (
                not isinstance(observed, ProcessObservation) or observed.capture_eof
            )
            if isinstance(observed, ProcessObservation):
                record.observed_remaining_pipes = int(not observed.capture_eof)
            if observed.settled and captured:
                if release:
                    try:
                        process.release(control.final)
                    except ProcessError:
                        settled = False
                continue
            settled = False
            if (
                requested is not None
                and record.signalled < escalation
                and observed.condition is ProcessCondition.LIVE
            ):
                self.diagnostics.emit(
                    "escalated", LifetimeReason.RESOURCE_LIVE, len(records)
                )
                process.signal(requested, control.final)
                record.signalled = escalation
        return settled

    def _finish_cleanup(
        self, resource_id: ResourceId | None, control: CleanupControl, *, safe: bool
    ) -> None:
        records = [
            record for key, record in self.records.items() if key in control.selected
        ]
        if not safe:
            for record in records:
                if record.entry.state is not ResourceState.CLOSED:
                    record.entry = record.entry.model_copy(
                        update={
                            "state": ResourceState.UNKNOWN,
                            "reason": LifetimeReason.EFFECT_UNSETTLED,
                        }
                    )
        unresolved = [
            record
            for record in records
            if record.entry.state is not ResourceState.CLOSED
        ]
        outcome = CleanupOutcome.CLOSED if safe else CleanupOutcome.UNKNOWN
        report = ClosureReport(
            invocation_id=self.invocation_id,
            resource_id=resource_id,
            revision=self.revision,
            state=outcome,
            original_outcome=self.original_outcome,
            cleanup_outcome=outcome,
            unresolved_count=len(unresolved),
            samples=tuple(
                self._observation(record, control.final)
                for record in unresolved[: self.limits.max_unresolved_samples]
            ),
            cleanup_reference=None
            if safe
            else CleanupReference(invocation_id=self.invocation_id),
        )
        if len(report.model_dump_json().encode()) > CLOSURE_REPORT_BYTES:
            raise LifetimeError(LifetimeReason.CAPACITY_EXCEEDED)
        self.diagnostics.emit(
            "closed" if safe else "unknown",
            LifetimeReason.COMPLETE if safe else LifetimeReason.EFFECT_UNSETTLED,
            len(records),
        )
        control.result.set_result(report)
        self.cleanup_changed.set()

    def _advance_cleanup(
        self, resource_id: ResourceId | None, control: CleanupControl
    ) -> None:
        if resource_id is None:
            control.selected.update(self.records)
            self._fence(control.selected)
        now = self.clock.monotonic_ns()
        if now >= control.final:
            self._finish_cleanup(resource_id, control, safe=False)
            return
        if resource_id is not None and control.terminal_join:
            if self.invocation_cleanup.result.done():
                self._finish_terminal_join(resource_id, control)
            return
        records = [
            record for key, record in self.records.items() if key in control.selected
        ]
        pending = self._cleanup_pending(control)
        helpers_settled = self.mutations.pending == 0 and all(
            helper.closed and not helper.descriptors
            for helper in self.mutations.helpers
        )
        settled = self._cleanup_processes(
            records, control, release=not pending and helpers_settled
        )
        if self.clock.monotonic_ns() >= control.final:
            self._finish_cleanup(resource_id, control, safe=False)
            return
        if pending or not helpers_settled or not settled:
            return
        inventory_ready = (
            self.bootstrap_succeeded
            and self.bootstrap_finished is not None
            and self.bootstrap_finished <= control.cooperative
        )
        if not inventory_ready or self.mutations.failed or control.failed:
            self._finish_cleanup(resource_id, control, safe=False)
            return
        for record in records:
            if record.entry.kind is ResourceKind.BORROWED_SERVICE:
                dependency = self.records.get(record.dependency or "")
                process = (
                    None
                    if dependency is None
                    else dependency.process or dependency.recovered
                )
                if process is None or not process.closed:
                    self._finish_cleanup(resource_id, control, safe=False)
                    return
        if self.cleanup_effect is not None:
            return
        for record in reversed(records):
            if record.directory is not None and not record.directory.closed:
                self._start_cleanup_effect(
                    control,
                    record.entry.resource_id,
                    self._remove_cleanup_tree(record, control.final),
                )
                return
        for record in records:
            if record.entry.state is not ResourceState.CLOSED:
                record.entry = record.entry.model_copy(
                    update={
                        "state": ResourceState.CLOSED,
                        "reason": LifetimeReason.COMPLETE,
                        "revision": self.revision + 1,
                    }
                )
        if not control.published:
            self._start_cleanup_effect(
                control, resource_id, self._publish_cleanup(control)
            )
            return
        if resource_id is None:
            if any(
                scoped.start is not None
                and not scoped.result.done()
                and not scoped.terminal_join
                for key, scoped in self._cleanup_controls()
                if key is not None
            ):
                return
            if self.endpoint_count:
                self._finish_cleanup(resource_id, control, safe=False)
                return
            directory = self.mutations.directory
            if directory is None:
                self._finish_cleanup(resource_id, control, safe=False)
                return
            if not directory.closed:
                control.removing_root = True
                self._start_cleanup_effect(
                    control, None, self._remove_cleanup_root(control)
                )
                return
        self._finish_cleanup(resource_id, control, safe=True)

    async def _drive_cleanup(self) -> None:
        while True:
            self.cleanup_changed.clear()
            effect = self.cleanup_effect
            if effect is not None and effect.task.done():
                if effect.task.cancelled() or effect.task.exception() is not None:
                    effect.control.failed = True
                self.cleanup_effect = None
                self.mutations.cleanup_task = None
                self.mutations.cleanup_permit = None
            for helper in self.mutations.helpers:
                if (
                    not self.mutations.pending
                    and not helper.closed
                    and helper.anchor.exited
                    and not helper.anchor.unknown
                ):
                    helper.release()
            self.mutations.prune()
            self._tighten_helper_constraints()
            self._signal_cleanup_helpers()
            active = [
                (key, control)
                for key, control in self._cleanup_controls()
                if control.start is not None and not control.result.done()
            ]
            for key, control in active:
                self._advance_cleanup(key, control)
            if self.invocation_cleanup.result.done() and all(
                control.result.done() for _, control in active
            ):
                return
            active = [
                (key, control) for key, control in active if not control.result.done()
            ]
            if not active:
                await self.cleanup_changed.wait()
                continue
            now = self.clock.monotonic_ns()
            if any(control.final <= now for _, control in active):
                continue
            control_deadline = min(
                instant
                for _, control in active
                for instant in (
                    control.cooperative,
                    control.term,
                    control.killed,
                    control.final,
                )
                if instant > now
            )
            deadline = min(
                (
                    control_deadline,
                    *(
                        instant
                        for origin in self.mutations.origins.values()
                        if origin.final is not None and now < origin.final
                        for instant in (origin.term, origin.kill, origin.final)
                        if instant is not None and instant > now
                    ),
                )
            )
            changed = asyncio.create_task(self.cleanup_changed.wait())
            timeout = asyncio.create_task(self.clock.sleep_until(deadline))
            pending: set[asyncio.Future[None] | asyncio.Task[bool]] = {changed, timeout}
            selected = set[str]().union(*(control.selected for _, control in active))
            for key in selected:
                process = self.records[key].process or self.records[key].recovered
                if process is not None and not process.closed:
                    pending.update(process.readiness())
            pending.update(
                helper.anchor.readiness
                for helper in self.mutations.helpers
                if not helper.closed and not helper.anchor.readiness.done()
            )
            try:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
                timeout.cancel()
                for waiter in (changed, timeout):
                    try:
                        await waiter
                    except asyncio.CancelledError:
                        if not waiter.cancelled():
                            raise

    def inventory(self) -> LifetimeInventory:
        return LifetimeInventory(
            resources=len(self.records),
            pending_operations=sum(
                not operation.task.done() for operation in self.operations.values()
            ),
            retained_processes=sum(
                (record.process is not None and not record.process.closed)
                or (record.recovered is not None and not record.recovered.closed)
                for record in self.records.values()
            ),
            retained_trees=sum(
                record.directory is not None and not record.directory.closed
                for record in self.records.values()
            ),
            endpoint_count=self.endpoint_count,
            control_bytes=self.control_bytes + self.input_control_bytes,
            execution_bytes=self.execution_bytes + self.input_execution_bytes,
            ordinary_owners=len(self.ordinary_slots),
            cleanup_driver_tasks=int(not self._cleanup_driver.done()),
            cleanup_effect_tasks=int(
                self.cleanup_effect is not None and not self.cleanup_effect.task.done()
            ),
            closure_results=sum(
                control.result.done() for _, control in self._cleanup_controls()
            ),
            member_capacity=sum(
                record.member_capacity for record in self.records.values()
            ),
            pending_process_readiness=sum(
                len(process.readiness())
                for record in self.records.values()
                if (process := record.process or record.recovered) is not None
                and not process.closed
            ),
            helpers=self.mutations.inventory(),
        )

    def dispose_recovery_observers(self) -> bool:
        if (
            not self._bootstrap.done()
            or not self._cleanup_driver.done()
            or (self.cleanup_effect is not None and not self.cleanup_effect.task.done())
            or any(not operation.task.done() for operation in self.operations.values())
            or any(not closure.done() for closure in self.closures.values())
            or any(record.process is not None for record in self.records.values())
        ):
            return False
        if not self.mutations.dispose_settled():
            return False
        for record in self.records.values():
            if record.recovered is not None:
                record.recovered.dispose_observation()
            if record.directory is not None:
                record.directory.close()
        return True

    def recovery_dependencies(self) -> tuple[asyncio.Future[object], ...]:
        dependencies: list[asyncio.Future[object]] = [
            cast(asyncio.Future[object], self._bootstrap),
            cast(asyncio.Future[object], self._cleanup_driver),
        ]
        dependencies.extend(
            cast(asyncio.Future[object], operation.task)
            for operation in self.operations.values()
        )
        if self.cleanup_effect is not None:
            dependencies.append(cast(asyncio.Future[object], self.cleanup_effect.task))
        dependencies.extend(
            cast(asyncio.Future[object], helper.anchor.readiness)
            for helper in self.mutations.helpers
        )
        return tuple(dependency for dependency in dependencies if not dependency.done())


@dataclass(frozen=True)
class CallAdmission:
    owner: ParentLifetime
    slot: OrdinarySlot
    deadline: int
    input_charge: "RequestInput | None"


@dataclass
class TransportBudget:
    control_bytes: int = 0
    execution_bytes: int = 0


@dataclass
class RequestInput:
    owner: ParentLifetime
    budget: TransportBudget | None
    control_bytes: int
    execution_bytes: int

    def transfer(self) -> None:
        if self.budget is None:
            self.owner.direct_control_bytes -= self.control_bytes
            self.owner.direct_execution_bytes -= self.execution_bytes
        else:
            self.owner.transport_control_bytes -= self.control_bytes
            self.owner.transport_execution_bytes -= self.execution_bytes
            self.budget.control_bytes -= self.control_bytes
            self.budget.execution_bytes -= self.execution_bytes
        self.control_bytes = 0
        self.execution_bytes = 0


CALL_ADMISSION: ContextVar[CallAdmission | None] = ContextVar(
    "lifetime_call_admission", default=None
)


@contextmanager
def wire_admission(
    owner: ParentLifetime,
    slot: OrdinarySlot,
    deadline: int,
    input_charge: RequestInput | None = None,
) -> Generator[None]:
    if slot not in owner.ordinary_slots or not slot.wire:
        raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)
    token = CALL_ADMISSION.set(CallAdmission(owner, slot, deadline, input_charge))
    try:
        yield
    finally:
        CALL_ADMISSION.reset(token)


@dataclass(frozen=True)
class RecoveryBinding:
    home: str
    state_home: str
    platform: str
    uid: int
    process: ProcessIdentity
    loop: asyncio.AbstractEventLoop
    launcher_identity: int | None
    clock_identity: int | None


@dataclass
class RecoveryContext:
    binding: RecoveryBinding
    root: DirectoryIdentity | None = None
    attempts: dict[str, "RecoveryAttempt"] = field(
        default_factory=dict[str, "RecoveryAttempt"]
    )


@dataclass(eq=False)
class RecoveryAttempt:
    registry: "RecoveryAttempts"
    context: RecoveryContext
    reference: CleanupReference
    limits: LifetimeLimits
    owner: ParentLifetime | None = None
    task: asyncio.Task[ClosureReport] | None = None
    dependencies: set[asyncio.Future[object]] = field(
        default_factory=set[asyncio.Future[object]]
    )

    def changed(self, future: asyncio.Future[object]) -> None:
        self.dependencies.discard(future)
        if not future.cancelled():
            future.exception()
        self.reconcile()

    def reconcile(self) -> None:
        if self.context.binding.loop.is_closed():
            return
        if asyncio.get_running_loop() is not self.context.binding.loop:
            return
        owner = self.owner
        task = self.task
        if owner is None or task is None:
            return
        root = owner.mutations.root
        if root is not None:
            self.registry.bind_root(self, root.identity)
        if owner.mutations.pending == 0:
            for helper in owner.mutations.helpers:
                if not helper.closed:
                    helper.release()
        if task.done() and owner.dispose_recovery_observers():
            if not task.cancelled():
                task.exception()
            for dependency in self.dependencies:
                dependency.remove_done_callback(self.changed)
            self.dependencies.clear()
            self.registry.remove(self)
            return
        dependencies = (
            *owner.recovery_dependencies(),
            cast(asyncio.Future[object], task),
        )
        for dependency in dependencies:
            if not dependency.done() and dependency not in self.dependencies:
                self.dependencies.add(dependency)
                dependency.add_done_callback(self.changed)


@dataclass(frozen=True)
class RecoveryInventory:
    contexts: int
    attempts: int
    placeholders: int
    active_tasks: int
    completed_tasks: int
    dependency_callbacks: int


class RecoveryAttempts:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.context: RecoveryContext | None = None

    def inventory(self) -> RecoveryInventory:
        with self.guard:
            context = self.context
            attempts = () if context is None else tuple(context.attempts.values())
        return RecoveryInventory(
            contexts=int(context is not None),
            attempts=len(attempts),
            placeholders=sum(attempt.owner is None for attempt in attempts),
            active_tasks=sum(
                attempt.task is not None and not attempt.task.done()
                for attempt in attempts
            ),
            completed_tasks=sum(
                attempt.task is not None and attempt.task.done() for attempt in attempts
            ),
            dependency_callbacks=sum(len(attempt.dependencies) for attempt in attempts),
        )

    def bind_root(self, attempt: RecoveryAttempt, root: DirectoryIdentity) -> None:
        with self.guard:
            context = self.context
            if (
                context is not None
                and context is attempt.context
                and context.attempts.get(attempt.reference.invocation_id) is attempt
            ):
                if context.root is not None and context.root != root:
                    raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)
                context.root = root

    def remove(self, attempt: RecoveryAttempt) -> None:
        with self.guard:
            context = self.context
            if (
                context is not None
                and context is attempt.context
                and context.attempts.get(attempt.reference.invocation_id) is attempt
            ):
                del context.attempts[attempt.reference.invocation_id]
                if not context.attempts:
                    self.context = None

    async def resume(
        self,
        reference: CleanupReference,
        limits: LifetimeLimits,
        *,
        launcher: IOHelperLauncher,
        home: str,
        state_home: str,
        clock: Clock | None = None,
    ) -> ClosureReport:
        observed = NativeIdentityReader().read(os.getpid())
        if (
            observed.state is not IdentityState.PRESENT
            or observed.process is None
            or not complete_identity(observed.process.identity)
        ):
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        binding = RecoveryBinding(
            home,
            state_home,
            sys.platform,
            os.geteuid(),
            observed.process.identity,
            asyncio.get_running_loop(),
            None if isinstance(launcher, InstalledIOHelperLauncher) else id(launcher),
            None if clock is None else id(clock),
        )
        with self.guard:
            previous = self.context
            retained = (
                tuple(previous.attempts.values())
                if previous is not None and previous.binding == binding
                else ()
            )
        for existing in retained:
            existing.reconcile()
        with self.guard:
            context = self.context
            if context is None:
                context = RecoveryContext(binding)
                self.context = context
            elif context.binding != binding:
                raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
            attempt = context.attempts.get(reference.invocation_id)
            if attempt is not None:
                if attempt.reference != reference or attempt.limits != limits:
                    raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)
                created = False
            else:
                if context.attempts and context.root is None:
                    raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
                if len(context.attempts) >= limits.max_state_root_invocations:
                    raise LifetimeError(LifetimeReason.CAPACITY_EXCEEDED)
                attempt = RecoveryAttempt(self, context, reference, limits)
                context.attempts[reference.invocation_id] = attempt
                created = True
        if created:
            try:
                owner = ParentLifetime(
                    limits=limits, recovery=reference, launcher=launcher, clock=clock
                )
            except (LifetimeError, OSError, RuntimeError):
                self.remove(attempt)
                raise
            owner.mutations.expected_root = context.root
            attempt.owner = owner
            attempt.task = asyncio.create_task(_resume_owned(owner))
            attempt.reconcile()
        task = attempt.task
        if task is None:
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        return await asyncio.shield(task)


RECOVERY_ATTEMPTS: RecoveryAttempts = RecoveryAttempts()


async def resume_close(
    reference: CleanupReference, limits: LifetimeLimits
) -> ClosureReport:
    launcher = InstalledIOHelperLauncher()
    return await RECOVERY_ATTEMPTS.resume(
        reference,
        limits,
        launcher=launcher,
        home=launcher.home,
        state_home=launcher.state_home,
    )


async def _resume_owned(owner: ParentLifetime) -> ClosureReport:
    try:
        try:
            await owner.ready()
        except LifetimeError:
            owner.admission_open = False
        return await owner.close(None)
    finally:
        owner.dispose_recovery_observers()
