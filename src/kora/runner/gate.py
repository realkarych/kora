import asyncio
import os
import socket
import struct
from dataclasses import dataclass

from pydantic import TypeAdapter

from kora.protocols.lifetime import (
    EnvironmentName,
    Executable,
    ExecutionString,
    ProcessLaunch,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import (
    MAX_FRAME_BYTES,
    MAX_RECORD_BYTES,
    BoundedChannel,
    ChannelError,
    ChannelFailure,
)

EXECUTABLE_VALUE: TypeAdapter[str] = TypeAdapter(Executable)
EXECUTION_VALUE: TypeAdapter[str] = TypeAdapter(ExecutionString)
ENVIRONMENT_NAME: TypeAdapter[str] = TypeAdapter(EnvironmentName)


@dataclass(frozen=True)
class ExecutionConfig:
    executable: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]

    @classmethod
    def from_launch(cls, launch: ProcessLaunch) -> "ExecutionConfig":
        return cls(
            launch.executable,
            launch.argv,
            tuple((item.name, item.value) for item in launch.environment),
        )

    def encode(self) -> tuple[bytes, bytes]:
        fields = (
            self.executable,
            *self.argv,
            *(value for item in self.environment for value in item),
        )
        encoded = tuple(field.encode("utf-8") for field in fields)
        lengths = (
            len(self.argv),
            len(self.environment),
            *(len(item) for item in encoded),
        )
        metadata = struct.pack(f"!{len(lengths)}I", *lengths)
        payload = b"".join(encoded)
        if len(metadata) > MAX_FRAME_BYTES or len(payload) > MAX_RECORD_BYTES:
            raise ChannelError(ChannelFailure.CAPACITY)
        return metadata, payload

    @classmethod
    def decode(cls, metadata: bytes, payload: bytes) -> "ExecutionConfig":
        if len(metadata) < 12 or len(metadata) % 4:
            raise ChannelError(ChannelFailure.MALFORMED)
        values = tuple(
            int.from_bytes(metadata[offset : offset + 4], "big")
            for offset in range(0, len(metadata), 4)
        )
        arguments, variables, *lengths = values
        if (
            not 1 <= arguments <= 4096
            or variables > 4096
            or len(lengths) != 1 + arguments + 2 * variables
            or sum(lengths) != len(payload)
            or len(payload) > MAX_RECORD_BYTES
        ):
            raise ChannelError(ChannelFailure.MALFORMED)
        fields: list[str] = []
        offset = 0
        for length in lengths:
            field = payload[offset : offset + length].decode("utf-8")
            if "\0" in field:
                raise ChannelError(ChannelFailure.MALFORMED)
            fields.append(field)
            offset += length
        executable, *remaining = fields
        executable = EXECUTABLE_VALUE.validate_python(executable)
        argv = tuple(
            EXECUTION_VALUE.validate_python(value) for value in remaining[:arguments]
        )
        environment = tuple(
            (
                ENVIRONMENT_NAME.validate_python(remaining[index]),
                EXECUTION_VALUE.validate_python(remaining[index + 1]),
            )
            for index in range(arguments, len(remaining), 2)
        )
        names = tuple(name for name, _ in environment)
        if not argv[0] or len(names) != len(set(names)):
            raise ChannelError(ChannelFailure.MALFORMED)
        return cls(executable, argv, environment)


async def receive_execution(
    endpoint: socket.socket, cwd_descriptor: int, deadline_ns: int
) -> ExecutionConfig:
    channel = BoundedChannel(endpoint, SystemClock())
    try:
        metadata = await channel.read_frame(deadline_ns)
        payload = await channel.read_record(deadline_ns, MAX_RECORD_BYTES)
        config = ExecutionConfig.decode(metadata, payload)
        os.fchdir(cwd_descriptor)
        await channel.write_frame(b"ready", deadline_ns)
        acknowledgement = await channel.read_frame(deadline_ns)
        if acknowledgement != b"activate":
            raise ChannelError(ChannelFailure.MALFORMED)
        return config
    finally:
        channel.close()
        os.close(cwd_descriptor)


def execute_workload(config: ExecutionConfig) -> None:
    os.execve(config.executable, config.argv, dict(config.environment))


def main() -> int:
    control = os.environ.get("KORA_GATE_CONTROL_FD", "")
    directory = os.environ.get("KORA_GATE_CWD_FD", "")
    if not all(
        value.isascii() and value.isdecimal() and 1 <= len(value) <= 10
        for value in (control, directory)
    ):
        return 70
    control_descriptor, cwd_descriptor = int(control), int(directory)
    if (
        min(control_descriptor, cwd_descriptor) < 3
        or control_descriptor == cwd_descriptor
    ):
        return 70
    endpoint = socket.socket(fileno=control_descriptor)
    header = endpoint.recv(8, socket.MSG_WAITALL)
    if len(header) != 8:
        endpoint.close()
        os.close(cwd_descriptor)
        return 71
    deadline = int.from_bytes(header, "big")
    try:
        config = asyncio.run(receive_execution(endpoint, cwd_descriptor, deadline))
        execute_workload(config)
    except (ChannelError, OSError, ValueError):
        return 72
    return 73


if __name__ == "__main__":
    raise SystemExit(main())
