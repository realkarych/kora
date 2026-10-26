import asyncio
import errno
import gc
import os
import socket
import sys
from pathlib import Path

from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import resume_close
from kora.protocols.lifetime import CleanupReference, LifetimeLimits
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel


def retained_descriptors(journal: JournalSnapshot) -> int:
    identities = {
        (identity.device, identity.inode)
        for identity in (journal.root, journal.directory, journal.claim)
        if identity is not None
    }
    directory = Path("/proc/self/fd" if sys.platform == "linux" else "/dev/fd")
    descriptors = [int(entry.name) for entry in directory.iterdir()]
    count = 0
    for descriptor in descriptors:
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            count += (metadata.st_dev, metadata.st_ino) in identities
    return count


async def run(home: Path, invocation_id: str, descriptor: int) -> None:
    clock = SystemClock()
    channel = BoundedChannel(socket.socket(fileno=descriptor), clock)
    report = await resume_close(
        CleanupReference(invocation_id=invocation_id),
        LifetimeLimits(
            cooperative_seconds=0.1,
            term_seconds=0.1,
            kill_observation_seconds=2.0,
            drain_seconds=1.0,
        ),
    )
    gc.collect()
    journal = JournalSnapshot.model_validate_json(
        (next(home.rglob(invocation_id)) / "journal").read_bytes()
    )
    deadline = clock.monotonic_ns() + 10_000_000_000
    await channel.write_record(report.model_dump_json().encode(), deadline)
    await channel.write_frame(
        retained_descriptors(journal).to_bytes(4, "big"), deadline
    )
    if await channel.read_frame(deadline) != b"exit":
        raise ValueError("exit control required")
    channel.close()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
