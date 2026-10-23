import asyncio
import dis
import os
import signal
import socket
from types import FrameType

from kora.conformance.io_helper import MUTATION, respond_mutation
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel


def stall_open(directory: int) -> int:
    return os.open("stall", os.O_RDONLY, dir_fd=directory)


CALL_OFFSETS: frozenset[int] = frozenset(
    instruction.offset
    for instruction in dis.get_instructions(stall_open)
    if instruction.opname in ("CALL", "CALL_KW", "CALL_FUNCTION")
)
PROBE: int = int(os.environ["KORA_TEST_OBSERVATION_FD"])


def acknowledge_call(signal_number: int, frame: FrameType | None) -> None:
    entered = (
        signal_number == signal.SIGUSR1
        and frame is not None
        and frame.f_code is stall_open.__code__
        and frame.f_lasti in CALL_OFFSETS
    )
    os.write(PROBE, b"e" if entered else b"n")


def pause(directory: int) -> None:
    os.write(PROBE, b"r")
    descriptor = stall_open(directory)
    os.close(descriptor)


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
        signal.signal(signal.SIGUSR1, acknowledge_call)
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
