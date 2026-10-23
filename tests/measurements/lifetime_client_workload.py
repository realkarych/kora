import asyncio
import os
import socket
import sys

from kora.conformance.transport import LifetimeClient
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)


async def run(address: str) -> None:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.connect(address)
    client = LifetimeClient(endpoint)
    try:
        request = ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
        reserved = await client.reserve(request)
        assert await client.reserve(request) == reserved
        observed = await client.observe(reserved.token.resource_id)
        assert len(observed) == 1
        await client.acquire(reserved.token, new_operation_id(), TemporaryTreeRequest())
        child = await client.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.PROCESS_DOMAIN,
                parent_resource_id=reserved.token.resource_id,
            )
        )
        operation = new_operation_id()
        launch = ProcessLaunch(
            executable=sys.executable,
            argv=(sys.executable, "-I", "-c", "pass"),
            environment=tuple(
                EnvironmentVariable(name=f"KORA_WIRE_{number}", value="w" * 110000)
                for number in range(2)
            ),
            working_directory_resource_id=reserved.token.resource_id,
            stdin=None,
            stdout=ProcessOutput.CAPTURE,
            stderr=ProcessOutput.CAPTURE,
            domain=ProcessDomain.INVOCATION,
        )
        copied = ProcessLaunch.model_validate_json(launch.model_dump_json())
        assert copied.environment[0].value is not launch.environment[0].value
        first, replay = await asyncio.gather(
            client.acquire(child.token, operation, launch),
            client.acquire(child.token, operation, copied),
        )
        assert first == replay
        await client.acknowledge(child.token, first.operation_id, first.revision)
        os.write(1, b"x" * 32768)
        os.write(2, b"y" * 32768)
    finally:
        client.disconnect()
        await asyncio.gather(client.reader, client.writer)
    assert not client.pending
    assert not client.resources
    assert client.control_bytes == 0
    assert client.execution_bytes == 0


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
