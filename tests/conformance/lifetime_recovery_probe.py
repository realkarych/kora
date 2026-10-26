import asyncio
import os
import sys
from pathlib import Path

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.lifetime import (
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)


async def retain(home: Path, mode: str) -> None:
    owner = ParentLifetime(
        launcher=InstalledIOHelperLauncher(home=home, state_home=home / "state")
    )
    tree = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
    )
    await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
    for _ in range(0 if mode == "tree" else 2 if mode == "pair" else 1):
        process = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.PROCESS_DOMAIN,
                parent_resource_id=tree.token.resource_id,
            )
        )
        acquisition = await owner.acquire(
            process.token,
            new_operation_id(),
            ProcessLaunch(
                executable=sys.executable,
                argv=(
                    sys.executable,
                    "-I",
                    "-c",
                    "pass" if mode == "gone" else "import signal; signal.pause()",
                ),
                environment=(),
                working_directory_resource_id=tree.token.resource_id,
                stdin=None,
                stdout=None,
                stderr=None,
                domain=ProcessDomain.INVOCATION,
            ),
        )
        await owner.acknowledge(
            process.token, acquisition.operation_id, acquisition.revision
        )
        if mode == "gone":
            handle = owner.records[process.token.resource_id].process
            assert handle is not None
            await handle.settle(owner.deadline())
            await handle.drain(owner.deadline())
            handle.release(owner.deadline())
    os.write(1, owner.invocation_id.encode("ascii"))


if __name__ == "__main__":
    asyncio.run(retain(Path(sys.argv[1]), sys.argv[2]))
