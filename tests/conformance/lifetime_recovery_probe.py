import asyncio
import os
import sys
from pathlib import Path

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.lifetime import (
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)


async def retain(home: Path) -> None:
    owner = ParentLifetime(
        launcher=InstalledIOHelperLauncher(home=home, state_home=home / "state")
    )
    tree = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
    )
    await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
    os.write(1, owner.invocation_id.encode("ascii"))


if __name__ == "__main__":
    asyncio.run(retain(Path(sys.argv[1])))
