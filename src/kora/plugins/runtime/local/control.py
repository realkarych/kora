import asyncio
import os
import socket
from collections.abc import Callable
from functools import partial

from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    ENVELOPE_BYTES,
    FRAME_PROGRESS_NS,
    SECTION_FRAME_BYTES,
    START_PROGRESS_NS,
    AttachRequest,
    FrameType,
    MessageRefusal,
    StatusValue,
    TransferRequest,
    control_request,
    message_refused,
    read_delivery,
    read_status,
    send_message,
)
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import TransferItem, Workload
from kora.protocols.time import Clock
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    ChannelFailure,
    cancel_task,
    wait_future,
)
from kora.runner.native_io import NativeIOSubmissionUncertain
from kora.runner.peers import PeerRefused, PeerSource
from kora.runner.registry import TransitionExecutor

NOT_RUNNER: str = "not_runner"


class ListenerRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def connected(path: str, clock: Clock, deadline_ns: int) -> socket.socket:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.setblocking(False)
    try:
        loop = asyncio.get_running_loop()
        connecting = asyncio.ensure_future(loop.sock_connect(endpoint, path))
        try:
            await wait_future(connecting, deadline_ns, clock)
        finally:
            await cancel_task(connecting)
        return endpoint
    except BaseException:
        endpoint.close()
        raise


async def attested(
    endpoint: socket.socket,
    runner: ProcessIdentity,
    source: PeerSource,
    executor: TransitionExecutor,
    *,
    after_uncertain_submission: Callable[[], None] | None = None,
) -> None:
    try:
        listener = await executor.run(
            lambda: source.attest(endpoint, None),
            after_uncertain_submission=after_uncertain_submission,
        )
    except PeerRefused as refused:
        raise ListenerRefused(refused.reason.value) from None
    if listener != runner:
        raise ListenerRefused(NOT_RUNNER)


async def runner_status(
    path: str,
    runner: ProcessIdentity,
    source: PeerSource,
    executor: TransitionExecutor,
    clock: Clock,
    request_id: str,
) -> StatusValue:
    deadline = clock.monotonic_ns() + FRAME_PROGRESS_NS
    endpoint = await connected(path, clock, deadline)
    channel = BoundedChannel(endpoint, clock)
    try:
        await attested(endpoint, runner, source, executor)
        request = control_request("status", request_id)
        await channel.write_typed_frame(FrameType.JSON, request, deadline)
        reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
        _, payload = await channel.read_typed_frame(deadline, reservations)
        return read_status(payload, request_id)
    finally:
        channel.close()


async def start_command(
    path: str,
    runner: ProcessIdentity,
    dispatcher_id: str,
    source: PeerSource,
    executor: TransitionExecutor,
    clock: Clock,
    request_id: str,
) -> None:
    deadline = clock.monotonic_ns() + FRAME_PROGRESS_NS
    endpoint = await connected(path, clock, deadline)
    channel = BoundedChannel(endpoint, clock)
    try:
        await attested(endpoint, runner, source, executor)
        request = AttachRequest(
            op="attach", request_id=request_id, dispatcher_id=dispatcher_id
        )
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
        _, payload = await channel.read_typed_frame(deadline, reservations)
        read_delivery(payload, request_id, "attached")
        started = clock.monotonic_ns() + START_PROGRESS_NS
        await channel.write_typed_frame(
            FrameType.JSON, control_request("start", request_id), started
        )
        _, payload = await channel.read_typed_frame(started, reservations)
        read_delivery(payload, request_id, "started")
    finally:
        channel.close()


async def transfer_input(
    path: str,
    runner: ProcessIdentity,
    dispatcher_id: str,
    source: PeerSource,
    executor: TransitionExecutor,
    clock: Clock,
    request_id: str,
    spool: int,
    item: TransferItem,
    message_deadline: int,
    *,
    close_spool: Callable[[], None],
    active: Callable[[], None] | None = None,
) -> None:
    deadline = min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline)
    endpoint = await connected(path, clock, deadline)
    channel = BoundedChannel(endpoint, clock)
    endpoint_owned = True

    def dispose_attestation() -> None:
        try:
            endpoint.close()
        finally:
            close_spool()

    try:
        try:
            await attested(
                endpoint,
                runner,
                source,
                executor,
                after_uncertain_submission=dispose_attestation,
            )
        except NativeIOSubmissionUncertain:
            endpoint_owned = False
            raise
        request = AttachRequest(
            op="attach", request_id=request_id, dispatcher_id=dispatcher_id
        )
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
        _, payload = await channel.read_typed_frame(deadline, reservations)
        read_delivery(payload, request_id, "attached")
        header = TransferRequest(op="transfer_in", request_id=request_id, item=item)
        await channel.write_typed_frame(
            FrameType.JSON,
            canonical_bytes(header.model_dump(mode="json")),
            min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline),
        )
        await _send_spool(
            channel,
            clock,
            executor,
            spool,
            item.size,
            message_deadline,
            active,
            close_spool,
        )
        answered = min(clock.monotonic_ns() + START_PROGRESS_NS, message_deadline)
        _, payload = await channel.read_typed_frame(answered, reservations)
        read_delivery(payload, request_id, "transferred")
    finally:
        if endpoint_owned:
            channel.close()


async def _send_spool(
    channel: BoundedChannel,
    clock: Clock,
    executor: TransitionExecutor,
    descriptor: int,
    size: int,
    message_deadline: int,
    active: Callable[[], None] | None,
    close_spool: Callable[[], None],
) -> None:
    def read(length: int, offset: int) -> bytes:
        if active is not None:
            active()
        if clock.monotonic_ns() >= message_deadline:
            raise ChannelError(ChannelFailure.DEADLINE)
        return os.pread(descriptor, length, offset)

    offset = 0
    while offset < size:
        length = min(SECTION_FRAME_BYTES, size - offset)
        chunk = await executor.run(
            partial(read, length, offset), after_uncertain_submission=close_spool
        )
        if not chunk:
            raise message_refused(MessageRefusal.MALFORMED)
        deadline = min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline)
        await channel.write_typed_frame(FrameType.BYTES, chunk, deadline)
        offset += len(chunk)


async def deliver_workload(
    path: str,
    runner: ProcessIdentity,
    workload: Workload,
    source: PeerSource,
    executor: TransitionExecutor,
    clock: Clock,
    request_id: str,
) -> None:
    encoded = await executor.run(
        lambda: canonical_bytes(workload.model_dump(mode="json"))
    )
    deadline = clock.monotonic_ns() + FRAME_PROGRESS_NS
    endpoint = await connected(path, clock, deadline)
    channel = BoundedChannel(endpoint, clock)
    try:
        await attested(endpoint, runner, source, executor)
        request = AttachRequest(
            op="attach", request_id=request_id, dispatcher_id=workload.dispatcher_id
        )
        await channel.write_typed_frame(
            FrameType.JSON, canonical_bytes(request.model_dump(mode="json")), deadline
        )
        reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
        _, payload = await channel.read_typed_frame(deadline, reservations)
        read_delivery(payload, request_id, "attached")
        await send_message(
            channel, ACCEPT_WORKLOAD, request_id, (encoded,), clock, executor=executor
        )
        _, payload = await channel.read_typed_frame(
            clock.monotonic_ns() + FRAME_PROGRESS_NS, reservations
        )
        read_delivery(payload, request_id, "accepted")
    finally:
        channel.close()
