import asyncio
import socket
import struct
import sys
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import Field, StrictInt, TypeAdapter, ValidationError

from kora.conformance.lifetime import LifetimeError, ParentLifetime, Request
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
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
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
REPLY: TypeAdapter[Reply] = TypeAdapter(Reply)


class LifetimeTransportError(Exception):
    def __init__(self, reason: ChannelFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


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
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[ReplyValue]] = set()

    def disconnect(self) -> None:
        self.channel.close()

    async def _exchange(self, call: Call, launch: ProcessLaunch | None) -> ReplyValue:
        async with self._lock:
            deadline = self.clock.monotonic_ns() + int(
                self.limits.acquisition_seconds * 1_000_000_000
            )
            try:
                encoded = CALL.dump_json(call)
                if len(encoded) > self.limits.max_control_frame_bytes:
                    raise ChannelError(ChannelFailure.CAPACITY)
                await self.channel.write_frame(encoded, deadline)
                if launch is not None:
                    metadata, payload = ExecutionConfig.from_launch(launch).encode()
                    await self.channel.write_frame(metadata, deadline)
                    await self.channel.write_record(payload, deadline)
                reply = REPLY.validate_json(
                    await self.channel.read_record(
                        deadline, self.limits.max_control_recovery_bytes
                    )
                )
            except (ChannelError, ValidationError, OSError) as error:
                self.channel.close()
                reason = (
                    error.reason
                    if isinstance(error, ChannelError)
                    else ChannelFailure.MALFORMED
                )
                raise LifetimeTransportError(reason) from error
            if isinstance(reply, FailureReply):
                raise LifetimeError(reply.reason)
            return reply.value

    def _completed(self, task: asyncio.Task[ReplyValue]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _request(
        self, call: Call, launch: ProcessLaunch | None = None
    ) -> ReplyValue:
        if len(self._tasks) >= self.limits.max_outstanding_operations:
            raise LifetimeTransportError(ChannelFailure.CAPACITY)
        task = asyncio.create_task(self._exchange(call, launch))
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

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
        return cast(
            ClosureReport, await self._request(CloseCall(resource_id=resource_id))
        )


class PendingCall:
    def __init__(
        self,
        call: Call,
        launch: ProcessLaunch | None,
        control_bytes: int,
        execution_bytes: int,
    ) -> None:
        self.call = call
        self.launch = launch
        self.control_bytes = control_bytes
        self.execution_bytes = execution_bytes


def peer_process_id(endpoint: socket.socket) -> int:
    if sys.platform == "linux":
        return struct.unpack(
            "3i", endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[0]
    if sys.platform == "darwin":
        return struct.unpack("i", endpoint.getsockopt(0, 2, 4))[0]
    raise LifetimeTransportError(ChannelFailure.MALFORMED)


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
        self.child_resource_id = child_resource_id
        self.channel = BoundedChannel(endpoint, owner.clock)
        self.queue: asyncio.Queue[PendingCall | None] = asyncio.Queue(
            owner.limits.max_outstanding_operations
        )
        self.resources: set[ResourceId] = set()
        self.control_bytes = 0
        self.execution_bytes = 0
        self._closing = False
        self._reader: asyncio.Task[None] | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._closure: asyncio.Task[ClosureReport] | None = None
        owner.endpoint_count += 1

    def _reserve_bytes(self, control: int, execution: int) -> None:
        if (
            control
            + self.owner.transport_control_bytes
            + self.owner.control_bytes
            + self.owner.mutations.inventory().journal_bytes
            > self.owner.limits.max_control_recovery_bytes
            or execution
            + self.owner.transport_execution_bytes
            + self.owner.execution_bytes
            > self.owner.limits.max_execution_snapshot_bytes
        ):
            raise ChannelError(ChannelFailure.CAPACITY)
        self.control_bytes += control
        self.execution_bytes += execution
        self.owner.transport_control_bytes += control
        self.owner.transport_execution_bytes += execution

    def _release_bytes(self, control: int, execution: int) -> None:
        self.control_bytes -= control
        self.execution_bytes -= execution
        self.owner.transport_control_bytes -= control
        self.owner.transport_execution_bytes -= execution

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.channel.close()
        self.owner.endpoint_count -= 1
        self._closure = asyncio.create_task(self.owner.close(None))

    async def run(self) -> ClosureReport:
        self._reader = asyncio.create_task(self._read())
        self._dispatcher = asyncio.create_task(self._dispatch())
        try:
            await asyncio.gather(self._reader, self._dispatcher)
        finally:
            self._shutdown()
        closure = self._closure
        if closure is None:
            raise LifetimeError(LifetimeReason.EFFECT_UNSETTLED)
        return await asyncio.shield(closure)

    async def _read(self) -> None:
        try:
            while not self._closing:
                deadline = self.owner.deadline()
                frame_reservation = self.owner.limits.max_control_frame_bytes
                self._reserve_bytes(frame_reservation, 0)
                reserved_control = frame_reservation
                reserved_execution = 0
                queued = False
                try:
                    encoded = await self.channel.read_frame(deadline)
                    if len(encoded) > frame_reservation:
                        raise ChannelError(ChannelFailure.CAPACITY)
                    call = CALL.validate_json(encoded)
                    self._release_bytes(frame_reservation - len(encoded), 0)
                    reserved_control = len(encoded)
                    launch = None
                    if isinstance(call, ProcessCall):
                        self._reserve_bytes(frame_reservation, call.execution_bytes)
                        reserved_control += frame_reservation
                        reserved_execution = call.execution_bytes
                        metadata = await self.channel.read_frame(deadline)
                        if len(metadata) > frame_reservation:
                            raise ChannelError(ChannelFailure.CAPACITY)
                        payload = await self.channel.read_record(
                            deadline, call.execution_bytes
                        )
                        if len(payload) != call.execution_bytes:
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
                        del payload, metadata, config
                    if (
                        self.owner.transport_pending
                        + self.owner.inventory().pending_operations
                        >= self.owner.limits.max_outstanding_operations
                    ):
                        raise ChannelError(ChannelFailure.CAPACITY)
                    self.queue.put_nowait(
                        PendingCall(call, launch, reserved_control, reserved_execution)
                    )
                    self.owner.transport_pending += 1
                    queued = True
                    del encoded, call, launch
                finally:
                    if not queued:
                        self._release_bytes(reserved_control, reserved_execution)
        except (
            ChannelError,
            ValidationError,
            UnicodeError,
            asyncio.QueueFull,
            OSError,
        ):
            self._shutdown()
        finally:
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is not None:
                    self.owner.transport_pending -= 1
                    self._release_bytes(pending.control_bytes, pending.execution_bytes)
            self.queue.put_nowait(None)

    def _check_resource(self, resource_id: ResourceId | None) -> None:
        if resource_id is not None and resource_id not in self.resources:
            raise LifetimeError(LifetimeReason.IDENTITY_MISMATCH)

    async def _handle(self, pending: PendingCall) -> ReplyValue:
        call = pending.call
        if isinstance(call, ReserveCall):
            self._check_resource(call.request.parent_resource_id)
            reservation = await self.owner.reserve(call.request)
            self.resources.add(reservation.token.resource_id)
            return reservation
        if isinstance(call, (AcquireCall, ProcessCall)):
            self._check_resource(call.token.resource_id)
            if isinstance(call, ProcessCall):
                self._check_resource(call.working_directory_resource_id)
                if pending.launch is None:
                    raise LifetimeError(LifetimeReason.ACQUISITION_FAILED)
                return await self.owner.acquire(
                    call.token, call.operation_id, pending.launch
                )
            return await self.owner.acquire(call.token, call.operation_id, call.request)
        if isinstance(call, AcknowledgeCall):
            self._check_resource(call.token.resource_id)
            return await self.owner.acknowledge(
                call.token, call.acquisition_operation_id, call.revision
            )
        self._check_resource(call.resource_id)
        if isinstance(call, ObserveCall):
            observations = await self.owner.observe(call.resource_id)
            return tuple(
                observation
                for observation in observations
                if observation.resource_id in self.resources
            )
        if call.resource_id is None:
            self._shutdown()
            raise LifetimeTransportError(ChannelFailure.CLOSED)
        return await self.owner.close(call.resource_id)

    async def _dispatch(self) -> None:
        while not self._closing:
            pending = await self.queue.get()
            if pending is None:
                return
            self.owner.transport_pending -= 1
            self._release_bytes(0, pending.execution_bytes)
            pending.execution_bytes = 0
            try:
                try:
                    result = await self._handle(pending)
                    reply: Reply = SuccessReply(value=result)
                except LifetimeError as error:
                    reply = FailureReply(reason=error.reason)
                encoded = REPLY.dump_json(reply)
                self._reserve_bytes(len(encoded), 0)
                try:
                    await self.channel.write_record(encoded, self.owner.deadline())
                finally:
                    self._release_bytes(len(encoded), 0)
            except (ChannelError, LifetimeTransportError, OSError):
                self._shutdown()
            finally:
                self._release_bytes(pending.control_bytes, pending.execution_bytes)
