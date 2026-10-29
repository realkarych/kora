import asyncio
import socket
import struct
import sys
from dataclasses import dataclass
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import Field, StrictInt, TypeAdapter, ValidationError

from kora.conformance.controllers import duration_ns
from kora.conformance.lifetime import (
    CLOSURE_CONTROL_BYTES,
    CLOSURE_REPORT_BYTES,
    LifetimeError,
    OrdinarySlot,
    ParentLifetime,
    Request,
    RequestInput,
    TransportBudget,
    closure_future,
    consume_failure,
    wire_admission,
)
from kora.protocols.common import BoundaryModel
from kora.protocols.lifetime import (
    Acquisition,
    ActivationAcknowledgement,
    BorrowedServiceRequest,
    ClosureReport,
    EnvironmentVariable,
    LifetimeLimits,
    LifetimeReason,
    LocalRuntimeLaunch,
    OperationId,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    Reservation,
    ReservationRequest,
    ReservationToken,
    ResourceId,
    ResourceObservation,
    TemporaryTreeRequest,
)
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    ChannelFailure,
    cancel_task,
)
from kora.runner.gate import ExecutionConfig
from kora.runner.identity import IdentityState


class ReserveCall(BoundaryModel):
    verb: Literal["reserve"] = "reserve"
    request: ReservationRequest


class AcquireCall(BoundaryModel):
    verb: Literal["acquire"] = "acquire"
    token: ReservationToken
    operation_id: OperationId
    request: TemporaryTreeRequest | BorrowedServiceRequest


class ProcessCall(BoundaryModel):
    verb: Literal["process"] = "process"
    token: ReservationToken
    operation_id: OperationId
    working_directory_resource_id: ResourceId
    stdout: ProcessOutput | None
    stderr: ProcessOutput | None
    domain: ProcessDomain
    execution_bytes: Annotated[StrictInt, Field(ge=0, le=1048576)]


class AcknowledgeCall(BoundaryModel):
    verb: Literal["acknowledge"] = "acknowledge"
    token: ReservationToken
    acquisition_operation_id: OperationId
    revision: Annotated[StrictInt, Field(gt=0, le=2**64 - 1)]


class ObserveCall(BoundaryModel):
    verb: Literal["observe"] = "observe"
    resource_id: ResourceId | None


class CloseCall(BoundaryModel):
    verb: Literal["close"] = "close"
    resource_id: ResourceId | None


Call: TypeAlias = Annotated[
    ReserveCall | AcquireCall | ProcessCall | AcknowledgeCall | ObserveCall | CloseCall,
    Field(discriminator="verb"),
]
CALL: TypeAdapter[Call] = TypeAdapter(Call)
ReplyValue: TypeAlias = (
    Reservation
    | Acquisition
    | ActivationAcknowledgement
    | ClosureReport
    | tuple[ResourceObservation, ...]
)


class SuccessReply(BoundaryModel):
    outcome: Literal["success"] = "success"
    value: ReplyValue


class FailureReply(BoundaryModel):
    outcome: Literal["failure"] = "failure"
    reason: LifetimeReason


Reply: TypeAlias = Annotated[
    SuccessReply | FailureReply, Field(discriminator="outcome")
]


class LifetimeTransportError(Exception):
    def __init__(self, reason: ChannelFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


HEADER_BYTES: int = 256
ENDPOINT_CONTROL_BYTES: int = CLOSURE_REPORT_BYTES + HEADER_BYTES + 2048


class OrdinaryHeader(BoundaryModel):
    kind: Literal["ordinary"] = "ordinary"
    request_id: Annotated[StrictInt, Field(gt=0, le=2**64 - 1)]
    verb: Literal["reserve", "acquire", "process", "acknowledge", "observe"]
    control_bytes: Annotated[StrictInt, Field(gt=0, le=131072)]
    execution_bytes: Annotated[StrictInt, Field(ge=0, le=1048576)]


class CloseHeader(BoundaryModel):
    kind: Literal["close"] = "close"
    resource_id: ResourceId | None


Header: TypeAlias = Annotated[OrdinaryHeader | CloseHeader, Field(discriminator="kind")]
HEADER: TypeAdapter[Header] = TypeAdapter(Header)


class OrdinaryReply(BoundaryModel):
    kind: Literal["ordinary"] = "ordinary"
    request_id: Annotated[StrictInt, Field(gt=0, le=2**64 - 1)]
    reply: Reply


class CloseReply(BoundaryModel):
    kind: Literal["close"] = "close"
    resource_id: ResourceId
    report: ClosureReport


Envelope: TypeAlias = Annotated[OrdinaryReply | CloseReply, Field(discriminator="kind")]
ENVELOPE: TypeAdapter[Envelope] = TypeAdapter(Envelope)


def reply_reservation(verb: str, limits: LifetimeLimits) -> int:
    return 1024 + 512 * limits.max_resource_records if verb == "observe" else 8192


@dataclass
class ClientControl:
    future: asyncio.Future[ClosureReport]
    deadline: int = 0
    requested: bool = False
    sent: bool = False


@dataclass
class ClientRequest:
    call: Call
    encoded: bytes
    metadata: bytes
    execution: bytes
    deadline: int
    future: asyncio.Future[ReplyValue]
    control_bytes: int
    reserve_bytes: int
    sent: bool = False


class LifetimeClient:
    def __init__(
        self,
        endpoint: socket.socket,
        *,
        clock: Clock | None = None,
        limits: LifetimeLimits | None = None,
    ) -> None:
        self.clock = clock if clock is not None else SystemClock()
        self.limits = limits if limits is not None else LifetimeLimits()
        self.channel = BoundedChannel(endpoint, self.clock)
        self.pending: dict[int, ClientRequest] = {}
        self.resources: dict[ResourceId | None, ClientControl] = {}
        self.invocation_id: str | None = None
        self.control_bytes = (
            ENDPOINT_CONTROL_BYTES
            + CLOSURE_CONTROL_BYTES
            + 2 * reply_reservation("observe", self.limits)
        )
        self.execution_bytes = 0
        self.next_request_id = 1
        self.changed = asyncio.Event()
        self.closed = False
        if self.control_bytes > self.limits.max_control_recovery_bytes:
            self.channel.close()
            raise LifetimeTransportError(ChannelFailure.CAPACITY)
        self.resources[None] = ClientControl(closure_future())
        self.reader = asyncio.create_task(self._read())
        self.writer = asyncio.create_task(self._write())
        self.reader.add_done_callback(self._completed)
        self.writer.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._disconnect(ChannelFailure.MALFORMED)

    def disconnect(self) -> None:
        self._disconnect(ChannelFailure.CLOSED)

    def _disconnect(self, reason: ChannelFailure) -> None:
        if self.closed:
            return
        self.closed = True
        self.channel.close()
        self.changed.set()
        for pending in self.pending.values():
            if not pending.future.done():
                pending.future.set_exception(LifetimeTransportError(reason))
        self.pending.clear()
        for control in self.resources.values():
            if not control.future.done():
                control.future.set_exception(LifetimeTransportError(reason))
        self.resources.clear()
        self.control_bytes = 0
        self.execution_bytes = 0

    async def _write(self) -> None:
        try:
            while not self.closed:
                self.changed.clear()
                for resource_id, control in tuple(self.resources.items()):
                    if control.requested and not control.sent:
                        await self.channel.write_frame(
                            HEADER.dump_json(CloseHeader(resource_id=resource_id)),
                            control.deadline,
                        )
                        control.sent = True
                for request_id, pending in tuple(self.pending.items()):
                    if pending.sent:
                        continue
                    call = pending.call
                    if isinstance(call, CloseCall):
                        raise ChannelError(ChannelFailure.MALFORMED)
                    header = OrdinaryHeader(
                        request_id=request_id,
                        verb=call.verb,
                        control_bytes=len(pending.encoded) + len(pending.metadata),
                        execution_bytes=len(pending.execution),
                    )
                    await self.channel.write_frame(
                        HEADER.dump_json(header), pending.deadline
                    )
                    await self.channel.write_frame(pending.encoded, pending.deadline)
                    if pending.metadata:
                        await self.channel.write_frame(
                            pending.metadata, pending.deadline
                        )
                        await self.channel.write_record(
                            pending.execution, pending.deadline
                        )
                    pending.sent = True
                    break
                if any(not pending.sent for pending in self.pending.values()) or any(
                    control.requested and not control.sent
                    for control in self.resources.values()
                ):
                    continue
                deadlines = [pending.deadline for pending in self.pending.values()]
                deadlines.extend(
                    control.deadline
                    for control in self.resources.values()
                    if control.requested and not control.future.done()
                )
                if not deadlines:
                    await self.changed.wait()
                    continue
                deadline = min(deadlines)
                if self.clock.monotonic_ns() >= deadline:
                    raise ChannelError(ChannelFailure.DEADLINE)
                changed = asyncio.create_task(self.changed.wait())
                timeout = asyncio.create_task(self.clock.sleep_until(deadline))
                try:
                    await asyncio.wait(
                        (changed, timeout), return_when=asyncio.FIRST_COMPLETED
                    )
                    if timeout.done() and self.clock.monotonic_ns() >= deadline:
                        raise ChannelError(ChannelFailure.DEADLINE)
                finally:
                    try:
                        await cancel_task(changed)
                    finally:
                        await cancel_task(timeout)
        except (ChannelError, OSError) as error:
            self._disconnect(
                error.reason
                if isinstance(error, ChannelError)
                else ChannelFailure.CLOSED
            )

    def _validate_reply(self, pending: ClientRequest, value: ReplyValue) -> None:
        call = pending.call
        valid = False
        if isinstance(call, ReserveCall) and isinstance(value, Reservation):
            valid = value.kind is call.request.kind
            if self.invocation_id is None:
                self.invocation_id = value.token.invocation_id
            valid = valid and value.token.invocation_id == self.invocation_id
            if valid and value.token.resource_id not in self.resources:
                self.resources[value.token.resource_id] = ClientControl(
                    closure_future()
                )
                pending.reserve_bytes = 0
        elif isinstance(call, (AcquireCall, ProcessCall)) and isinstance(
            value, Acquisition
        ):
            valid = (
                value.token == call.token and value.operation_id == call.operation_id
            )
        elif isinstance(call, AcknowledgeCall) and isinstance(
            value, ActivationAcknowledgement
        ):
            valid = (
                value.token == call.token
                and value.acquisition_operation_id == call.acquisition_operation_id
                and value.revision == call.revision
            )
        elif isinstance(call, ObserveCall) and isinstance(value, tuple):
            valid = all(
                observation.resource_id in self.resources for observation in value
            )
        if not valid:
            raise ChannelError(ChannelFailure.MALFORMED)

    async def _read(self) -> None:
        try:
            while not self.closed:
                await self.channel.wait_readable()
                deadline = self.clock.monotonic_ns() + duration_ns(
                    self.limits.acquisition_seconds
                )
                reservation = max(
                    reply_reservation("observe", self.limits), CLOSURE_REPORT_BYTES
                )
                envelope = ENVELOPE.validate_json(
                    await self.channel.read_record(deadline, reservation)
                )
                if isinstance(envelope, CloseReply):
                    control = self.resources.get(envelope.resource_id)
                    if (
                        control is None
                        or not control.requested
                        or control.future.done()
                        or envelope.report.resource_id != envelope.resource_id
                        or envelope.report.invocation_id != self.invocation_id
                    ):
                        raise ChannelError(ChannelFailure.MALFORMED)
                    if self.clock.monotonic_ns() >= control.deadline:
                        raise ChannelError(ChannelFailure.DEADLINE)
                    control.future.set_result(envelope.report)
                    self.changed.set()
                    continue
                pending = self.pending.get(envelope.request_id)
                if pending is None or not pending.sent or pending.future.done():
                    raise ChannelError(ChannelFailure.MALFORMED)
                if self.clock.monotonic_ns() >= pending.deadline:
                    raise ChannelError(ChannelFailure.DEADLINE)
                reply = envelope.reply
                if isinstance(reply, SuccessReply):
                    self._validate_reply(pending, reply.value)
                    pending.future.set_result(reply.value)
                else:
                    pending.future.set_exception(LifetimeError(reply.reason))
                self.control_bytes -= pending.control_bytes + pending.reserve_bytes
                self.execution_bytes -= len(pending.execution)
                del self.pending[envelope.request_id]
                self.changed.set()
        except (ChannelError, ValidationError, OSError) as error:
            self._disconnect(
                error.reason
                if isinstance(error, ChannelError)
                else ChannelFailure.MALFORMED
            )

    async def _request(
        self, call: Call, launch: ProcessLaunch | None = None
    ) -> ReplyValue:
        if self.closed:
            raise LifetimeTransportError(ChannelFailure.CLOSED)
        if (
            len(self.pending) >= self.limits.max_outstanding_operations
            or self.next_request_id > 2**64 - 1
        ):
            raise LifetimeTransportError(ChannelFailure.CAPACITY)
        deadline = self.clock.monotonic_ns() + duration_ns(
            self.limits.acquisition_seconds
        )
        encoded = CALL.dump_json(call)
        metadata, execution = (
            (b"", b"")
            if launch is None
            else ExecutionConfig.from_launch(launch).encode()
        )
        reply_bytes = reply_reservation(call.verb, self.limits)
        control_bytes = len(encoded) + len(metadata) + 2 * reply_bytes
        reserve_bytes = CLOSURE_CONTROL_BYTES if isinstance(call, ReserveCall) else 0
        if (
            len(encoded) > self.limits.max_control_frame_bytes
            or len(metadata) > self.limits.max_control_frame_bytes
            or self.control_bytes + control_bytes + reserve_bytes
            > self.limits.max_control_recovery_bytes
            or self.execution_bytes + len(execution)
            > self.limits.max_execution_snapshot_bytes
        ):
            raise LifetimeTransportError(ChannelFailure.CAPACITY)
        future: asyncio.Future[ReplyValue] = asyncio.get_running_loop().create_future()
        future.add_done_callback(consume_failure)
        self.pending[self.next_request_id] = ClientRequest(
            call,
            encoded,
            metadata,
            execution,
            deadline,
            future,
            control_bytes,
            reserve_bytes,
        )
        self.next_request_id += 1
        self.control_bytes += control_bytes + reserve_bytes
        self.execution_bytes += len(execution)
        self.changed.set()
        timeout = asyncio.create_task(self.clock.sleep_until(deadline))
        try:
            await asyncio.wait((future, timeout), return_when=asyncio.FIRST_COMPLETED)
            if not future.done():
                self._disconnect(ChannelFailure.DEADLINE)
            return future.result()
        finally:
            await cancel_task(timeout)

    async def reserve(self, request: ReservationRequest) -> Reservation:
        return cast(Reservation, await self._request(ReserveCall(request=request)))

    async def acquire(
        self, token: ReservationToken, operation_id: OperationId, request: Request
    ) -> Acquisition:
        if isinstance(request, LocalRuntimeLaunch):
            raise LifetimeError(LifetimeReason.ACQUISITION_FAILED)
        if isinstance(request, ProcessLaunch):
            _, payload = ExecutionConfig.from_launch(request).encode()
            call = ProcessCall(
                token=token,
                operation_id=operation_id,
                working_directory_resource_id=request.working_directory_resource_id,
                stdout=request.stdout,
                stderr=request.stderr,
                domain=request.domain,
                execution_bytes=len(payload),
            )
            return cast(Acquisition, await self._request(call, request))
        return cast(
            Acquisition,
            await self._request(
                AcquireCall(token=token, operation_id=operation_id, request=request)
            ),
        )

    async def acknowledge(
        self,
        token: ReservationToken,
        acquisition_operation_id: OperationId,
        revision: int,
    ) -> ActivationAcknowledgement:
        return cast(
            ActivationAcknowledgement,
            await self._request(
                AcknowledgeCall(
                    token=token,
                    acquisition_operation_id=acquisition_operation_id,
                    revision=revision,
                )
            ),
        )

    async def observe(
        self, resource_id: ResourceId | None
    ) -> tuple[ResourceObservation, ...]:
        return cast(
            tuple[ResourceObservation, ...],
            await self._request(ObserveCall(resource_id=resource_id)),
        )

    async def close(self, resource_id: ResourceId | None) -> ClosureReport:
        if self.closed:
            raise LifetimeTransportError(ChannelFailure.CLOSED)
        control = self.resources.get(resource_id)
        if control is None:
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        if not control.requested:
            control.requested = True
            control.deadline = self.clock.monotonic_ns() + sum(
                duration_ns(seconds)
                for seconds in (
                    self.limits.cooperative_seconds,
                    self.limits.term_seconds,
                    self.limits.kill_observation_seconds,
                    self.limits.drain_seconds,
                )
            )
            self.changed.set()
        return await asyncio.shield(control.future)


def peer_process_id(endpoint: socket.socket) -> int:
    if sys.platform == "linux":
        return struct.unpack(
            "3i", endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[0]
    if sys.platform == "darwin":
        return struct.unpack("i", endpoint.getsockopt(0, 2, 4))[0]
    raise LifetimeTransportError(ChannelFailure.MALFORMED)


@dataclass
class EndpointResource:
    subscribed: bool = False
    sent: bool = False


@dataclass
class PendingCall:
    header: OrdinaryHeader
    deadline: int
    slot: OrdinarySlot
    control_bytes: int
    input_charge: RequestInput
    task: asyncio.Task[None] | None = None
    encoded: bytes | None = None


class LifetimeEndpoint:
    def __init__(
        self,
        owner: ParentLifetime,
        child_resource_id: ResourceId,
        endpoint: socket.socket,
    ) -> None:
        record = owner.records.get(child_resource_id)
        registration = (
            None
            if record is None or record.acquisition is None
            else record.acquisition.registration
        )
        if (
            not isinstance(registration, ProcessRegistration)
            or record is None
            or record.process is None
            or record.process.closed
        ):
            endpoint.close()
            raise LifetimeError(LifetimeReason.IDENTITY_UNKNOWN)
        peer = peer_process_id(endpoint)
        observed = owner.identities.read(peer)
        if (
            peer != registration.process.pid
            or observed.state is not IdentityState.PRESENT
            or observed.process is None
            or observed.process.identity != registration.process
        ):
            endpoint.close()
            raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)
        if owner.endpoint_count >= owner.limits.max_outstanding_operations:
            endpoint.close()
            raise LifetimeError(LifetimeReason.CAPACITY_EXCEEDED)
        self.owner = owner
        self.channel = BoundedChannel(endpoint, owner.clock)
        self.resources: dict[ResourceId, EndpointResource] = {}
        self.pending: dict[int, PendingCall] = {}
        self.budget = TransportBudget()
        self._closing = False
        self.changed = asyncio.Event()
        self.last_request_id = 0
        try:
            self._reserve_bytes(ENDPOINT_CONTROL_BYTES, 0)
        except ChannelError:
            self.channel.close()
            raise
        owner.endpoint_count += 1

    @property
    def control_bytes(self) -> int:
        return self.budget.control_bytes

    @property
    def execution_bytes(self) -> int:
        return self.budget.execution_bytes

    def _reserve_bytes(self, control: int, execution: int) -> None:
        if (
            control
            + self.owner.input_control_bytes
            + self.owner.control_bytes
            + self.owner.mutations.inventory().journal_bytes
            > self.owner.limits.max_control_recovery_bytes
            or execution + self.owner.input_execution_bytes + self.owner.execution_bytes
            > self.owner.limits.max_execution_snapshot_bytes
        ):
            raise ChannelError(ChannelFailure.CAPACITY)
        self.budget.control_bytes += control
        self.budget.execution_bytes += execution
        self.owner.transport_control_bytes += control
        self.owner.transport_execution_bytes += execution

    def _release_bytes(self, control: int, execution: int) -> None:
        self.budget.control_bytes -= control
        self.budget.execution_bytes -= execution
        self.owner.transport_control_bytes -= control
        self.owner.transport_execution_bytes -= execution

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.channel.close()
        self.changed.set()
        self.owner.endpoint_count -= 1
        self.owner.request_close(None)

    def _completed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._shutdown()
        self.changed.set()

    def _release_pending(self, request_id: int) -> None:
        pending = self.pending.pop(request_id)
        pending.input_charge.transfer()
        self._release_bytes(pending.control_bytes, 0)
        self.owner.release_wire(pending.slot)
        self.owner.transport_pending -= 1

    async def run(self) -> ClosureReport:
        reader = asyncio.create_task(self._read())
        writer = asyncio.create_task(self._write())
        reader.add_done_callback(self._completed)
        writer.add_done_callback(self._completed)
        try:
            await asyncio.gather(reader, writer)
        finally:
            self._shutdown()
            interruption: asyncio.CancelledError | None = None
            failures: list[BaseException] = []

            async def settle(tasks: tuple[asyncio.Task[None], ...]) -> None:
                nonlocal interruption
                for task in tasks:
                    task.cancel()
                joined = asyncio.gather(*tasks, return_exceptions=True)
                while not joined.done():
                    try:
                        await asyncio.shield(joined)
                    except asyncio.CancelledError as error:
                        if interruption is None:
                            interruption = error
                failures.extend(
                    result
                    for result in joined.result()
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                )

            await settle((reader, writer))
            await settle(
                tuple(
                    pending.task
                    for pending in self.pending.values()
                    if pending.task is not None
                )
            )
            for request_id in tuple(self.pending):
                self._release_pending(request_id)
            self.resources.clear()
            self._release_bytes(ENDPOINT_CONTROL_BYTES, 0)
            failure: BaseException | None = (
                None
                if not failures
                else failures[0]
                if len(failures) == 1
                else BaseExceptionGroup("lifetime_endpoint_cleanup_failures", failures)
            )
            if interruption is not None:
                try:
                    raise interruption
                finally:
                    if failure is not None:
                        raise failure
            if failure is not None:
                raise failure
        return await self.owner.close(None)

    def _check_resource(self, resource_id: ResourceId | None) -> None:
        if resource_id is not None and resource_id not in self.resources:
            raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)

    async def _read(self) -> None:
        try:
            while not self._closing:
                await self.channel.wait_readable()
                deadline = self.owner.deadline()
                header = HEADER.validate_json(
                    await self.channel.read_frame(deadline, HEADER_BYTES)
                )
                if isinstance(header, CloseHeader):
                    self._check_resource(header.resource_id)
                    future = self.owner.request_close(header.resource_id)
                    if header.resource_id is None:
                        self._shutdown()
                        return
                    resource = self.resources[header.resource_id]
                    if not resource.subscribed:
                        resource.subscribed = True
                        future.add_done_callback(self._close_ready)
                    self.changed.set()
                    continue
                if header.request_id <= self.last_request_id:
                    raise ChannelError(ChannelFailure.MALFORMED)
                self.last_request_id = header.request_id
                slot = self.owner.admit_ordinary(wire=True)
                reserved_control = header.control_bytes + 2 * reply_reservation(
                    header.verb, self.owner.limits
                )
                reserved = False
                transferred = False
                try:
                    self._reserve_bytes(reserved_control, header.execution_bytes)
                    reserved = True
                    encoded = await self.channel.read_frame(
                        deadline,
                        min(
                            header.control_bytes,
                            self.owner.limits.max_control_frame_bytes,
                        ),
                    )
                    call = CALL.validate_json(encoded)
                    if call.verb != header.verb or isinstance(call, CloseCall):
                        raise ChannelError(ChannelFailure.MALFORMED)
                    launch = None
                    consumed = len(encoded)
                    if isinstance(call, ProcessCall):
                        if call.execution_bytes != header.execution_bytes:
                            raise ChannelError(ChannelFailure.MALFORMED)
                        metadata = await self.channel.read_frame(
                            deadline,
                            min(
                                header.control_bytes - consumed,
                                self.owner.limits.max_control_frame_bytes,
                            ),
                        )
                        consumed += len(metadata)
                        payload = await self.channel.read_record(
                            deadline, header.execution_bytes
                        )
                        if len(payload) != header.execution_bytes:
                            raise ChannelError(ChannelFailure.MALFORMED)
                        config = ExecutionConfig.decode(metadata, payload)
                        launch = ProcessLaunch(
                            executable=config.executable,
                            argv=config.argv,
                            environment=tuple(
                                EnvironmentVariable(name=name, value=value)
                                for name, value in config.environment
                            ),
                            working_directory_resource_id=call.working_directory_resource_id,
                            stdin=None,
                            stdout=call.stdout,
                            stderr=call.stderr,
                            domain=call.domain,
                        )
                        del metadata, payload, config
                    elif header.execution_bytes:
                        raise ChannelError(ChannelFailure.MALFORMED)
                    if consumed != header.control_bytes:
                        raise ChannelError(ChannelFailure.MALFORMED)
                    pending = PendingCall(
                        header,
                        deadline,
                        slot,
                        reserved_control - header.control_bytes,
                        RequestInput(
                            self.owner,
                            self.budget,
                            header.control_bytes,
                            header.execution_bytes,
                        ),
                    )
                    self.pending[header.request_id] = pending
                    self.owner.transport_pending += 1
                    pending.task = asyncio.create_task(
                        self._handle(pending, call, launch)
                    )
                    del encoded, call, launch
                    pending.task.add_done_callback(self._completed)
                    transferred = True
                finally:
                    if not transferred:
                        if reserved:
                            self._release_bytes(
                                reserved_control, header.execution_bytes
                            )
                        self.owner.release_wire(slot)
        except (ChannelError, LifetimeError, ValidationError, UnicodeError, OSError):
            self._shutdown()

    def _close_ready(self, future: asyncio.Future[ClosureReport]) -> None:
        self.changed.set()

    async def _value(self, call: Call, launch: ProcessLaunch | None) -> ReplyValue:
        if isinstance(call, ReserveCall):
            self._check_resource(call.request.parent_resource_id)
            reservation = await self.owner.reserve(call.request)
            self.resources.setdefault(reservation.token.resource_id, EndpointResource())
            return reservation
        if isinstance(call, (AcquireCall, ProcessCall)):
            self._check_resource(call.token.resource_id)
            if isinstance(call, ProcessCall):
                self._check_resource(call.working_directory_resource_id)
                if launch is None:
                    raise LifetimeError(LifetimeReason.ACQUISITION_FAILED)
                return await self.owner.acquire(call.token, call.operation_id, launch)
            return await self.owner.acquire(call.token, call.operation_id, call.request)
        if isinstance(call, AcknowledgeCall):
            self._check_resource(call.token.resource_id)
            return await self.owner.acknowledge(
                call.token, call.acquisition_operation_id, call.revision
            )
        if isinstance(call, ObserveCall):
            self._check_resource(call.resource_id)
            observations = await self.owner.observe(call.resource_id)
            return tuple(
                observation
                for observation in observations
                if observation.resource_id in self.resources
            )
        raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)

    async def _handle(
        self, pending: PendingCall, call: Call, launch: ProcessLaunch | None
    ) -> None:
        try:
            with wire_admission(
                self.owner, pending.slot, pending.deadline, pending.input_charge
            ):
                reply: Reply = SuccessReply(value=await self._value(call, launch))
        except LifetimeError as error:
            reply = FailureReply(reason=error.reason)
        envelope = OrdinaryReply(request_id=pending.header.request_id, reply=reply)
        encoded = ENVELOPE.dump_json(envelope)
        if len(encoded) > reply_reservation(pending.header.verb, self.owner.limits):
            raise ChannelError(ChannelFailure.CAPACITY)
        pending.encoded = encoded
        self.changed.set()

    async def _write(self) -> None:
        try:
            while not self._closing:
                self.changed.clear()
                for resource_id, resource in tuple(self.resources.items()):
                    if not resource.subscribed or resource.sent:
                        continue
                    future = self.owner.closures[resource_id]
                    if not future.done():
                        continue
                    report = future.result()
                    control = self.owner.records[resource_id].cleanup
                    if control is None:
                        raise ChannelError(ChannelFailure.MALFORMED)
                    encoded = ENVELOPE.dump_json(
                        CloseReply(resource_id=resource_id, report=report)
                    )
                    if len(encoded) > CLOSURE_REPORT_BYTES:
                        raise ChannelError(ChannelFailure.CAPACITY)
                    await self.channel.write_record(encoded, control.final)
                    resource.sent = True
                for request_id, pending in tuple(self.pending.items()):
                    if pending.encoded is not None:
                        await self.channel.write_record(
                            pending.encoded, pending.deadline
                        )
                        self._release_pending(request_id)
                await self.changed.wait()
        except (ChannelError, OSError):
            self._shutdown()
