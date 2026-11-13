import asyncio
import hashlib
import os
import socket
from collections.abc import Callable
from functools import partial

from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    ACCEPTANCE_PROGRESS_NS,
    ENVELOPE_BYTES,
    FRAME_PROGRESS_NS,
    OBSERVATION_BYTES,
    SECTION_FRAME_BYTES,
    START_PROGRESS_NS,
    AttachRequest,
    FrameType,
    LogRequest,
    MessageRefusal,
    OutputRequest,
    StatusValue,
    TransferRequest,
    control_request,
    delivery_error,
    message_refused,
    read_delivery,
    read_observation,
    read_output_declaration,
    read_output_trailer,
    read_status,
    send_message,
    write_transfer_bytes,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import CommandObservation, TransferItem, Workload
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
from kora.runner.registry import TransitionExecutor, refusal

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


async def observe_command(
    path: str,
    runner: ProcessIdentity,
    dispatcher_id: str,
    source: PeerSource,
    executor: TransitionExecutor,
    clock: Clock,
    request_id: str,
) -> CommandObservation:
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
        answered = clock.monotonic_ns() + START_PROGRESS_NS
        await channel.write_typed_frame(
            FrameType.JSON, control_request("observe", request_id), answered
        )
        _, payload = await channel.read_typed_frame(
            answered, {FrameType.JSON.value: OBSERVATION_BYTES}
        )
        return read_observation(payload, request_id)
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


async def transfer_output(
    path: str,
    runner: ProcessIdentity,
    dispatcher_id: str,
    source: PeerSource,
    executor: TransitionExecutor,
    clock: Clock,
    spool: int,
    header: OutputRequest | LogRequest,
    message_deadline: int,
    *,
    whole: bool,
    close_spool: Callable[[], None],
    active: Callable[[], None] | None = None,
) -> int:
    request_id = header.request_id
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
        await channel.write_typed_frame(
            FrameType.JSON,
            canonical_bytes(header.model_dump(mode="json")),
            min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline),
        )
        return await receive_output(
            channel,
            clock,
            executor,
            spool,
            request_id,
            message_deadline,
            active,
            close_spool,
            whole=whole,
        )
    except ChannelError as error:
        if error.reason in (ChannelFailure.MALFORMED, ChannelFailure.CAPACITY):
            raise delivery_error("integrity_error") from None
        raise
    except KoraProtocolError as error:
        if error.code == "message_refused":
            raise delivery_error("integrity_error") from None
        raise
    finally:
        if endpoint_owned:
            channel.close()


async def receive_output(
    channel: BoundedChannel,
    clock: Clock,
    executor: TransitionExecutor,
    descriptor: int,
    request_id: str,
    message_deadline: int,
    active: Callable[[], None] | None,
    close_spool: Callable[[], None],
    *,
    whole: bool,
) -> int:
    reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
    _, payload = await channel.read_typed_frame(message_deadline, reservations)
    declared = read_output_declaration(payload, request_id)
    digest = hashlib.sha256()
    remaining = declared.size
    while remaining:
        length = min(SECTION_FRAME_BYTES, remaining)
        frame_deadline = min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline)
        kind, piece = await channel.read_typed_frame(
            frame_deadline, {**reservations, FrameType.BYTES.value: length}
        )
        if kind == FrameType.JSON:
            read_output_trailer(piece, request_id)
            raise message_refused(MessageRefusal.MALFORMED)
        if not piece:
            raise message_refused(MessageRefusal.MALFORMED)

        def write(payload: bytes = piece) -> None:
            if active is not None:
                active()
            if clock.monotonic_ns() >= message_deadline:
                raise ChannelError(ChannelFailure.DEADLINE)
            try:
                write_transfer_bytes(descriptor, memoryview(payload))
            except OSError:
                raise refusal("runner_unreachable") from None

        await executor.run(write, after_uncertain_submission=close_spool)
        digest.update(piece)
        remaining -= len(piece)
    _, payload = await channel.read_typed_frame(
        min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline), reservations
    )
    trailer = read_output_trailer(payload, request_id)
    computed = "sha256:" + digest.hexdigest()
    if (whole and computed != declared.sha256) or trailer != declared.sha256:
        raise delivery_error("integrity_error")
    await channel.expect_eof(
        min(clock.monotonic_ns() + FRAME_PROGRESS_NS, message_deadline)
    )
    return declared.size


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
            clock.monotonic_ns() + ACCEPTANCE_PROGRESS_NS, reservations
        )
        read_delivery(payload, request_id, "accepted")
    finally:
        channel.close()
