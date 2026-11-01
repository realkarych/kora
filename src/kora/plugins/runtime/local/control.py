import asyncio
import socket

from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    ENVELOPE_BYTES,
    FRAME_PROGRESS_NS,
    AttachRequest,
    FrameType,
    StatusValue,
    control_request,
    read_delivery,
    read_status,
    send_message,
)
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import Workload
from kora.protocols.time import Clock
from kora.runner.channels import BoundedChannel, cancel_task, wait_future
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
) -> None:
    try:
        listener = await executor.run(lambda: source.attest(endpoint, None))
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
