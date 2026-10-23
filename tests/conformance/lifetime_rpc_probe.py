import asyncio
import socket
import sys

from kora.conformance.lifetime import LifetimeError
from kora.conformance.transport import (
    CALL,
    HEADER,
    LifetimeClient,
    LifetimeTransportError,
    ObserveCall,
    OrdinaryHeader,
    ProcessCall,
    ReserveCall,
)
from kora.protocols.lifetime import (
    Acquisition,
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    Reservation,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.channels import ChannelFailure
from kora.runner.gate import ExecutionConfig


async def close_pending(
    client: LifetimeClient,
    feedback: socket.socket,
    feedback_address: str,
    resource_id: str,
    acquisitions: list[asyncio.Task[Acquisition]],
) -> None:
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(feedback, resource_id.encode("ascii"), feedback_address)
    command = await loop.sock_recv(feedback, 16)
    if command == b"cancel":
        acquisitions[1].cancel()
        try:
            await acquisitions[1]
        except asyncio.CancelledError:
            if not acquisitions[1].cancelled():
                raise
        assert len(client.pending) == len(acquisitions)
        await loop.sock_sendto(feedback, b"c", feedback_address)
        command = await loop.sock_recv(feedback, 16)
    assert command == b"close"
    closing = asyncio.create_task(client.close(resource_id))
    cancelled = asyncio.create_task(client.close(resource_id))
    started: asyncio.Future[None] = loop.create_future()
    loop.call_soon(started.set_result, None)
    await started
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        if not cancelled.cancelled():
            raise
    for acquisition in acquisitions:
        if acquisition.cancelled():
            continue
        try:
            await acquisition
        except LifetimeError:
            pass
        else:
            raise AssertionError("fenced acquisition returned a usable lease")
    report = await closing
    assert report.cleanup_reference is None
    assert await client.close(resource_id) == report
    assert not client.pending
    feedback.close()
    client.disconnect()


def progress(address: str, mode: str, phase: bytes) -> None:
    if mode.startswith("cancel"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as feedback:
            feedback.sendto(phase, address)


async def partial_input(
    client: LifetimeClient,
    endpoint: socket.socket,
    tree: Reservation,
    mode: str,
    feedback_address: str,
) -> None:
    if mode == "cancel_frame":
        await client.channel.write_frame(
            HEADER.dump_json(
                OrdinaryHeader(
                    request_id=client.next_request_id,
                    verb="observe",
                    control_bytes=256,
                    execution_bytes=0,
                )
            ),
            client.clock.monotonic_ns() + 5_000_000_000,
        )
        await asyncio.get_running_loop().sock_sendall(
            endpoint, (256).to_bytes(4, "big") + b"{"
        )
    else:
        process = await client.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.PROCESS_DOMAIN,
                parent_resource_id=tree.token.resource_id,
            )
        )
        progress(feedback_address, mode, b"s")
        request = ProcessLaunch(
            executable=sys.executable,
            argv=(sys.executable, "-I", "-c", "pass"),
            environment=(),
            working_directory_resource_id=tree.token.resource_id,
            stdin=None,
            stdout=None,
            stderr=None,
            domain=ProcessDomain.INVOCATION,
        )
        metadata, payload = ExecutionConfig.from_launch(request).encode()
        call = ProcessCall(
            token=process.token,
            operation_id=new_operation_id(),
            working_directory_resource_id=tree.token.resource_id,
            stdout=None,
            stderr=None,
            domain=ProcessDomain.INVOCATION,
            execution_bytes=len(payload),
        )
        deadline = client.clock.monotonic_ns() + 5_000_000_000
        await client.channel.write_frame(
            HEADER.dump_json(
                OrdinaryHeader(
                    request_id=client.next_request_id,
                    verb="process",
                    control_bytes=len(CALL.dump_json(call)) + len(metadata),
                    execution_bytes=len(payload),
                )
            ),
            deadline,
        )
        await client.channel.write_frame(CALL.dump_json(call), deadline)
        await client.channel.write_frame(metadata, deadline)
        await client.channel.write_frame(len(payload).to_bytes(4, "big"), deadline)
        await client.channel.write_frame(payload[:1], deadline)
    feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    feedback.sendto(b"p", feedback_address)
    feedback.close()
    await client.reader
    assert client.closed
    assert client.channel.closed
    client.disconnect()


async def invalid_header(
    client: LifetimeClient,
    endpoint: socket.socket,
    mode: str,
    feedback_address: str,
) -> None:
    feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    feedback.bind(feedback_address + "-child")
    feedback.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_sendto(feedback, b"r", feedback_address)
        assert await loop.sock_recv(feedback, 1) == b"s"
        deadline = client.clock.monotonic_ns() + 5_000_000_000
        if mode == "header_partial":
            await loop.sock_sendall(endpoint, (32).to_bytes(4, "big")[:2])
        elif mode == "header_oversized":
            await loop.sock_sendall(endpoint, (257).to_bytes(4, "big"))
        elif mode == "header_malformed":
            await client.channel.write_frame(b"{", deadline)
        else:
            call = (
                ReserveCall(
                    request=ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                if mode == "header_verb"
                else ObserveCall(resource_id=None)
            )
            encoded = CALL.dump_json(call)
            await client.channel.write_frame(
                HEADER.dump_json(
                    OrdinaryHeader(
                        request_id=1
                        if mode == "header_replayed"
                        else client.next_request_id,
                        verb="observe",
                        control_bytes=len(encoded),
                        execution_bytes=0,
                    )
                ),
                deadline,
            )
            if mode == "header_verb":
                await client.channel.write_frame(encoded, deadline)
        await loop.sock_sendto(feedback, b"p", feedback_address)
        await client.reader
        assert client.closed
        assert client.channel.closed
    finally:
        feedback.close()
        client.disconnect()


async def run(address: str, mode: str, feedback_address: str) -> None:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.connect(address)
    client = LifetimeClient(endpoint)
    reservation_request = ReservationRequest(
        operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
    )
    tree = await client.reserve(reservation_request)
    progress(feedback_address, mode, b"r")
    assert await client.reserve(reservation_request) == tree
    progress(feedback_address, mode, b"d")
    if mode.startswith("header_"):
        await invalid_header(client, endpoint, mode, feedback_address)
        return
    if mode in ("cancel_frame", "cancel_execution"):
        await partial_input(client, endpoint, tree, mode, feedback_address)
        return
    if mode.startswith("concurrent-"):
        feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        feedback.bind(feedback_address + "-child")
        feedback.setblocking(False)
        operation_id = new_operation_id()
        acquisitions = [
            asyncio.create_task(
                client.acquire(tree.token, operation_id, TemporaryTreeRequest())
            )
            for _ in range(int(mode.split("-")[1]))
        ]
        await close_pending(
            client, feedback, feedback_address, tree.token.resource_id, acquisitions
        )
        return
    acquired = await client.acquire(
        tree.token, new_operation_id(), TemporaryTreeRequest()
    )
    await client.acknowledge(tree.token, acquired.operation_id, acquired.revision)
    process = await client.reserve(
        ReservationRequest(
            operation_id=new_operation_id(),
            kind=ResourceKind.PROCESS_DOMAIN,
            parent_resource_id=tree.token.resource_id,
        )
    )
    request = ProcessLaunch(
        executable=sys.executable,
        argv=(sys.executable, "-I", "-c", "pass"),
        environment=tuple(
            EnvironmentVariable(name=f"PAYLOAD{index}", value="x" * 60000)
            for index in range(15 if mode == "replay-near" else 5)
        )
        if mode.startswith("replay-")
        else (),
        working_directory_resource_id=tree.token.resource_id,
        stdin=None,
        stdout=None,
        stderr=None,
        domain=ProcessDomain.INVOCATION,
    )
    operation_id = new_operation_id()
    if mode.startswith("replay-"):
        feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        feedback.bind(feedback_address + "-child")
        feedback.setblocking(False)
        acquisitions = [
            asyncio.create_task(client.acquire(process.token, operation_id, request))
            for _ in range(1 if mode == "replay-near" else 3)
        ]
        await close_pending(
            client, feedback, feedback_address, tree.token.resource_id, acquisitions
        )
        return
    acquisition = await client.acquire(process.token, operation_id, request)
    assert await client.acquire(process.token, operation_id, request) == acquisition
    await client.acknowledge(process.token, operation_id, acquisition.revision)
    observations = await client.observe(None)
    assert {observation.resource_id for observation in observations} == {
        tree.token.resource_id,
        process.token.resource_id,
    }
    report = await client.close(tree.token.resource_id)
    assert report.cleanup_reference is None
    if mode == "terminal":
        failure = None
        try:
            await client.close(None)
        except LifetimeTransportError as error:
            failure = error.reason
        else:
            raise AssertionError("a closed caller cannot receive a closure receipt")
        assert failure is ChannelFailure.CLOSED
    client.disconnect()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2], sys.argv[3]))
