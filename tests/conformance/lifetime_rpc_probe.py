import asyncio
import socket
import sys

from kora.conformance.transport import (
    CALL,
    LifetimeClient,
    LifetimeTransportError,
    ProcessCall,
)
from kora.protocols.lifetime import (
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.channels import ChannelFailure
from kora.runner.gate import ExecutionConfig


async def run(address: str, mode: str, feedback_address: str) -> None:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.connect(address)
    client = LifetimeClient(endpoint)
    reservation_request = ReservationRequest(
        operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
    )
    tree = await client.reserve(reservation_request)
    assert await client.reserve(reservation_request) == tree
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
        environment=(),
        working_directory_resource_id=tree.token.resource_id,
        stdin=None,
        stdout=None,
        stderr=None,
        domain=ProcessDomain.INVOCATION,
    )
    operation_id = new_operation_id()
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
    if mode in ("cancel_frame", "cancel_execution"):
        if mode == "cancel_frame":
            await asyncio.get_running_loop().sock_sendall(
                endpoint, (65536).to_bytes(4, "big") + b"{"
            )
        else:
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
            await client.channel.write_frame(CALL.dump_json(call), deadline)
            await client.channel.write_frame(metadata, deadline)
            await client.channel.write_frame(len(payload).to_bytes(4, "big"), deadline)
            await client.channel.write_frame(payload[:1], deadline)
        feedback = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        feedback.sendto(b"p", feedback_address)
        feedback.close()
        assert await asyncio.get_running_loop().sock_recv(endpoint, 1) == b""
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
