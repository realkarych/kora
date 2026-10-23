import asyncio
import socket
import sys

from kora.conformance.transport import LifetimeClient, LifetimeTransportError
from kora.protocols.lifetime import (
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.channels import ChannelFailure


async def run(address: str, terminal: bool) -> None:
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
    if terminal:
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
    asyncio.run(run(sys.argv[1], sys.argv[2] == "terminal"))
