import asyncio
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import TypeAlias, cast

from pydantic import TypeAdapter

from kora.conformance.controllers import HelperInventory, MutationOwner
from kora.conformance.diagnostics import LifetimeDiagnostics
from kora.conformance.io_helper import (
    CreateTreeMutation,
    HelperError,
    InstalledIOHelperLauncher,
    IOHelperLauncher,
    RemoveTreeMutation,
)
from kora.conformance.journal import JournalEntry, JournalSnapshot
from kora.protocols.common import BoundaryModel
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
from kora.runner.channels import ChannelError, wait_future
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


class LifetimeError(Exception):
    def __init__(self, reason: LifetimeReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


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
    helpers: HelperInventory


def consume_task_failure(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


def finish_future(
    future: asyncio.Future[None], task: asyncio.Task[ClosureReport]
) -> None:
    if not future.done():
        future.set_result(None)


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
        self.closures: dict[ResourceId | None, asyncio.Task[ClosureReport]] = {}
        self.control_bytes = 0
        self.execution_bytes = 0
        self.transport_control_bytes = 0
        self.transport_execution_bytes = 0
        self.transport_pending = 0
        self.endpoint_count = 0
        self.closure_started = asyncio.Event()
        self.cooperative_deadline_ns: int | None = None
        self.borrowed_scopes: dict[str, ResourceId] = {}
        self.mutations = MutationOwner(
            launcher if launcher is not None else InstalledIOHelperLauncher(),
            self.clock,
            self.limits,
        )
        self.diagnostics = LifetimeDiagnostics(self.invocation_id)
        self._bootstrap = asyncio.create_task(
            self._establish() if recovery is None else self._recover(recovery)
        )
        self._bootstrap.add_done_callback(consume_task_failure)

    def deadline(self) -> int:
        return self.clock.monotonic_ns() + int(
            self.limits.acquisition_seconds * 1_000_000_000
        )

    async def _establish(self) -> None:
        await self.mutations.bootstrap(self.snapshot(), self.deadline())
        self.bootstrap_succeeded = True
        self.diagnostics.emit("started", LifetimeReason.COMPLETE, 0)

    async def _recover(self, reference: CleanupReference) -> None:
        self.admission_open = False
        snapshot = await self.mutations.recover(reference, self.owner, self.deadline())
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
        for record in self.records.values():
            entry = record.entry
            if entry.state is ResourceState.CLOSED:
                continue
            if isinstance(entry.registration, ProcessRegistration):
                record.recovered = RecoveredProcess(
                    entry.registration, self.clock, self.identities
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
                record.directory = parent.open_child(identity)
            elif not isinstance(
                entry.registration, (BorrowedServiceRegistration, ProcessRegistration)
            ):
                raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        self.bootstrap_succeeded = True
        self.diagnostics.emit("recovery", LifetimeReason.COMPLETE, len(self.records))

    async def ready(self) -> None:
        try:
            await asyncio.shield(self._bootstrap)
        except (
            HelperError,
            ChannelError,
            ProcessError,
            FilesystemError,
            OSError,
        ) as error:
            self.admission_open = False
            raise LifetimeError(LifetimeReason.JOURNAL_UNAVAILABLE) from error

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
            + self.transport_control_bytes
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
    ) -> asyncio.Task[Result]:
        existing = self.operations.get(key)
        if existing is not None:
            coroutine.close()
            if existing.request != request:
                raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
            return existing.task
        execution = isinstance(request, (ProcessLaunch, LocalRuntimeLaunch))
        size = (
            len(ExecutionConfig.from_launch(request).encode()[1])
            if isinstance(request, ProcessLaunch)
            else len(request.model_dump_json().encode())
        )
        if sum(
            not operation.task.done() for operation in self.operations.values()
        ) + self.transport_pending >= self.limits.max_outstanding_operations or (
            self.execution_bytes + self.transport_execution_bytes + size
            > self.limits.max_execution_snapshot_bytes
            if execution
            else self.control_bytes
            + self.transport_control_bytes
            + self.mutations.inventory().journal_bytes
            + size
            > self.limits.max_control_recovery_bytes
        ):
            coroutine.close()
            raise self._refuse(LifetimeReason.CAPACITY_EXCEEDED)
        if execution:
            self.execution_bytes += size
        else:
            self.control_bytes += size
        task = asyncio.create_task(self._run_operation(coroutine))
        task.add_done_callback(consume_task_failure)
        self.operations[key] = RecordedOperation(request, task, resource_id)
        return task

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
        await self.ready()
        task = self._operation(
            ("reserve", request.operation_id),
            request,
            request.parent_resource_id,
            self._reserve(request),
        )
        return cast(Reservation, await asyncio.shield(task))

    async def _reserve(self, request: ReservationRequest) -> Result:
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
        record = ResourceRecord(entry, token)
        self.records[resource_id] = record
        await self._persist(self.deadline())
        return Reservation(
            token=token,
            kind=request.kind,
            revision=entry.revision,
            state=ResourceState.RESERVED,
        )

    async def acquire(
        self, token: ReservationToken, operation_id: OperationId, request: Request
    ) -> Acquisition:
        await self.ready()
        operation_id = OPERATION_ID.validate_python(operation_id)
        record = self._record(token)
        task = self._operation(
            ("acquire", operation_id),
            request,
            token.resource_id,
            self._acquire(record, operation_id, request),
        )
        existing = self.operations[("acquire", operation_id)]
        if existing.resource_id != token.resource_id:
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        return cast(Acquisition, await asyncio.shield(task))

    async def _acquire(
        self, record: ResourceRecord, operation_id: str, request: Request
    ) -> Result:
        self._admit(record)
        if (
            record.entry.state is not ResourceState.RESERVED
            or request.kind != record.entry.kind
        ):
            raise self._refuse(LifetimeReason.IDENTITY_MISMATCH)
        deadline = self.deadline()
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
            record.process = start_gated(
                request,
                operation_id,
                directory.descriptor,
                deadline,
                self.clock,
                self.identities,
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
            self.mutations.prune()
            if result.directory is None:
                raise self._refuse(LifetimeReason.JOURNAL_UNAVAILABLE)
            record.directory = directory.open_child(result.directory)
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
        await self.ready()
        record = self._record(token)
        request = ActivationAcknowledgement(
            token=token,
            acquisition_operation_id=acquisition_operation_id,
            revision=revision,
            result=ActivationResult.ACTIVE,
        )
        task = self._operation(
            ("acknowledge", acquisition_operation_id),
            request,
            token.resource_id,
            self._acknowledge(record, request),
        )
        return cast(ActivationAcknowledgement, await asyncio.shield(task))

    async def _acknowledge(
        self, record: ResourceRecord, request: ActivationAcknowledgement
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
        await self._persist(self.deadline())
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
        await self.ready()
        deadline = self.deadline()
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

    async def close(self, resource_id: ResourceId | None) -> ClosureReport:
        existing = self.closures.get(resource_id)
        if existing is not None:
            return await asyncio.shield(existing)
        selected = self._selected(resource_id)
        start = self.clock.monotonic_ns()
        if resource_id is None:
            self.admission_open = False
            self.cooperative_deadline_ns = start + int(
                self.limits.cooperative_seconds * 1_000_000_000
            )
            self.closure_started.set()
        for key in selected:
            record = self.records[key]
            record.fenced = True
            if record.process is not None:
                record.process.close_gate()
        earlier = tuple(self.closures.values())
        task = asyncio.create_task(self._close(resource_id, selected, earlier, start))
        task.add_done_callback(consume_task_failure)
        self.closures[resource_id] = task
        return await asyncio.shield(task)

    async def _wait_operations(self, selected: set[str], deadline: int) -> bool:
        tasks: tuple[asyncio.Task[Result] | asyncio.Task[None], ...] = (
            self._bootstrap,
            *tuple(
                operation.task
                for operation in self.operations.values()
                if not operation.task.done()
                and (operation.resource_id in selected or operation.resource_id is None)
            ),
        )
        tasks = tuple(task for task in tasks if not task.done())
        if not tasks:
            return True
        timeout = asyncio.create_task(self.clock.sleep_until(deadline))
        try:
            pending: set[asyncio.Task[Result] | asyncio.Task[None]] = set(tasks)
            while pending and not timeout.done():
                _, pending = await asyncio.wait(
                    (*pending, timeout), return_when=asyncio.FIRST_COMPLETED
                )
                pending.discard(timeout)
            for task in tasks:
                if task.done() and not task.cancelled():
                    task.exception()
            return all(task.done() for task in tasks)
        finally:
            timeout.cancel()
            try:
                await timeout
            except asyncio.CancelledError:
                if not timeout.cancelled():
                    raise

    async def _settle_process(self, record: ResourceRecord, deadline: int) -> bool:
        if self.clock.monotonic_ns() >= deadline:
            return False
        if record.recovered is not None:
            try:
                await record.recovered.settle(deadline)
                return True
            except (ChannelError, ProcessError):
                return False
        process = record.process
        if process is None or process.closed:
            return True
        try:
            await process.settle(deadline)
            await process.drain(deadline)
            return True
        except (ChannelError, ProcessError):
            return False

    async def _close(
        self,
        resource_id: ResourceId | None,
        selected: set[str],
        earlier: tuple[asyncio.Task[ClosureReport], ...],
        start: int,
    ) -> ClosureReport:
        cooperative = start + int(self.limits.cooperative_seconds * 1_000_000_000)
        term = cooperative + int(self.limits.term_seconds * 1_000_000_000)
        killed = term + int(self.limits.kill_observation_seconds * 1_000_000_000)
        final = killed + int(self.limits.drain_seconds * 1_000_000_000)
        earlier_settled = True
        for previous in earlier:
            completion = asyncio.get_running_loop().create_future()
            previous.add_done_callback(partial(finish_future, completion))
            try:
                await wait_future(completion, cooperative, self.clock)
            except ChannelError:
                earlier_settled = False
        await self._wait_operations(selected, cooperative)
        inventory_ready = self._bootstrap.done() and self.bootstrap_succeeded
        if resource_id is None:
            selected.update(self.records)
        records = [record for key, record in self.records.items() if key in selected]
        for record in records:
            if record.process is not None:
                record.process.close_gate()
            if record.entry.state is not ResourceState.CLOSED:
                record.entry = record.entry.model_copy(
                    update={
                        "state": ResourceState.CLOSING,
                        "revision": self.revision + 1,
                    }
                )
        for record in records:
            await self._settle_process(record, cooperative)
        for requested, phase in ((signal.SIGTERM, term), (signal.SIGKILL, killed)):
            for record in records:
                process = record.process or record.recovered
                if (
                    process is not None
                    and not process.closed
                    and self.clock.monotonic_ns() < final
                ):
                    observation = process.observe(final)
                    record.observed_live_processes = len(observation.live_members)
                    if isinstance(observation, ProcessObservation):
                        record.observed_remaining_pipes = int(
                            not observation.capture_eof
                        )
                    if observation.condition is ProcessCondition.LIVE:
                        self.diagnostics.emit(
                            "escalated", LifetimeReason.RESOURCE_LIVE, len(records)
                        )
                        process.signal(requested, final)
            if self.clock.monotonic_ns() < final:
                self.mutations.signal(requested)
            for record in records:
                await self._settle_process(record, phase)
            await self._wait_operations(selected, phase)
        operations_settled = await self._wait_operations(selected, final)
        helpers_settled = await self.mutations.settle(final)
        if resource_id is None:
            selected.update(self.records)
            records = list(self.records.values())
        safe = (
            inventory_ready
            and self.bootstrap_succeeded
            and earlier_settled
            and operations_settled
            and helpers_settled
            and not self.mutations.failed
            and self.clock.monotonic_ns() < final
        )
        for record in records:
            if record.entry.kind is ResourceKind.BORROWED_SERVICE:
                scope = self.records.get(record.dependency or "")
                process = None if scope is None else scope.process or scope.recovered
                if process is None or (
                    record.dependency not in selected and not process.closed
                ):
                    safe = False
        if safe:
            for record in records:
                process = record.process or record.recovered
                if process is not None and not process.closed:
                    try:
                        process.release(final)
                    except ProcessError:
                        safe = False
        if safe:
            for record in reversed(records):
                if record.directory is not None and not record.directory.closed:
                    parent_id = record.entry.parent_resource_id
                    parent = (
                        self.mutations.directory
                        if parent_id is None
                        else self.records[parent_id].directory
                    )
                    if parent is None:
                        safe = False
                        break
                    try:
                        await self.mutations.perform(
                            RemoveTreeMutation(
                                directory=parent.identity,
                                target=record.directory.identity,
                            ),
                            parent,
                            final,
                        )
                        self.mutations.prune()
                        record.directory.close()
                    except (
                        HelperError,
                        ChannelError,
                        ProcessError,
                        FilesystemError,
                        OSError,
                    ):
                        safe = False
                        break
        for record in records:
            process = record.process or record.recovered
            closed = safe or (
                process is not None and process.closed and record.directory is None
            )
            record.entry = record.entry.model_copy(
                update={
                    "state": ResourceState.CLOSED if closed else ResourceState.UNKNOWN,
                    "reason": LifetimeReason.COMPLETE
                    if closed
                    else LifetimeReason.EFFECT_UNSETTLED,
                    "revision": self.revision + 1,
                }
            )
        if self.bootstrap_succeeded:
            try:
                await self._persist(final)
            except LifetimeError:
                safe = False
        if safe and resource_id is None:
            root = self.mutations.root
            directory = self.mutations.directory
            if root is None or directory is None or self.endpoint_count:
                safe = False
            else:
                try:
                    await self.mutations.perform(
                        RemoveTreeMutation(
                            directory=root.identity, target=directory.identity
                        ),
                        root,
                        final,
                    )
                    self.mutations.prune()
                    self.mutations.release()
                except (
                    HelperError,
                    ChannelError,
                    ProcessError,
                    FilesystemError,
                    OSError,
                ):
                    safe = False
        unresolved = tuple(
            self._observation(record, final)
            for record in records
            if record.entry.state is not ResourceState.CLOSED
        )
        outcome = CleanupOutcome.CLOSED if safe else CleanupOutcome.UNKNOWN
        for record in records:
            if record.recovered is not None and not record.recovered.closed:
                record.recovered.dispose_observation()
        self.diagnostics.emit(
            "closed" if safe else "unknown",
            LifetimeReason.COMPLETE if safe else LifetimeReason.EFFECT_UNSETTLED,
            len(records),
        )
        return ClosureReport(
            invocation_id=self.invocation_id,
            resource_id=resource_id,
            revision=self.revision,
            state=outcome,
            original_outcome=self.original_outcome,
            cleanup_outcome=outcome,
            unresolved_count=len(unresolved),
            samples=unresolved[: self.limits.max_unresolved_samples],
            cleanup_reference=None
            if safe
            else CleanupReference(invocation_id=self.invocation_id),
        )

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
            control_bytes=self.control_bytes + self.transport_control_bytes,
            execution_bytes=self.execution_bytes + self.transport_execution_bytes,
            helpers=self.mutations.inventory(),
        )


async def resume_close(
    reference: CleanupReference, limits: LifetimeLimits
) -> ClosureReport:
    owner = ParentLifetime(limits=limits, recovery=reference)
    return await asyncio.shield(asyncio.create_task(_resume_owned(owner)))


async def _resume_owned(owner: ParentLifetime) -> ClosureReport:
    try:
        await owner.ready()
    except LifetimeError:
        owner.admission_open = False
    return await owner.close(None)
