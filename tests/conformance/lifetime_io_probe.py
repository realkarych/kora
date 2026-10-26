import asyncio
import os
import signal
import socket
from types import FrameType

from kora.conformance.io_helper import MUTATION, respond_mutation
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel

PROBE: int = int(os.environ["KORA_TEST_OBSERVATION_FD"])


def pause(directory: int) -> None:
    os.write(PROBE, b"r")
    descriptor = os.open("stall", os.O_RDONLY, dir_fd=directory)
    try:
        os.write(PROBE, b"e")
        if os.read(descriptor, 1) != b"":
            raise ValueError("FIFO lease received unexpected data")
    finally:
        os.close(descriptor)


def acknowledge_term(signal_number: int, frame: FrameType | None) -> None:
    if signal_number == signal.SIGTERM:
        os.write(PROBE, b"t")


async def run() -> None:
    endpoint = socket.socket(fileno=int(os.environ["KORA_IO_CONTROL_FD"]))
    descriptors = tuple(
        int(value) for value in os.environ["KORA_IO_DESCRIPTORS"].split(",") if value
    )
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 30_000_000_000
    channel = BoundedChannel(endpoint, clock)
    fifo_directory = os.environ.get("KORA_TEST_FIFO_DIRECTORY_FD")
    phase = os.environ["KORA_TEST_PAUSE_PHASE"]
    if fifo_directory is not None:
        if os.environ["KORA_TEST_RESIST_TERM"] == "1":
            signal.signal(signal.SIGTERM, acknowledge_term)
    try:
        mutation = MUTATION.validate_json(await channel.read_record(deadline, 1048576))
        if fifo_directory is not None and phase in ("before", "both"):
            pause(int(fifo_directory))
        await respond_mutation(endpoint, channel, mutation, descriptors, deadline)
    finally:
        channel.close()
        for descriptor in descriptors[:-1]:
            os.close(descriptor)
        if fifo_directory is not None and phase in ("after", "both"):
            pause(int(fifo_directory))
        os.close(PROBE)


if __name__ == "__main__":
    asyncio.run(run())
