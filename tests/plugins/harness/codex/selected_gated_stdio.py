import asyncio
import os
import select
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Protocol, cast

from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.process_stdio import StdioStreams
from kora.runner.processes import GatedProcess, start_gated

MAX_DESCRIPTORS = 256
MAX_ENVIRONMENT = 65_536


class GatedObservationError(ValueError):
    pass


class Descriptor(Protocol):
    def fileno(self) -> int: ...


def descriptor_targets(pid: int) -> dict[int, str]:
    targets: dict[int, str] = {}
    with os.scandir(f"/proc/{pid}/fd") as entries:
        for index, entry in enumerate(islice(entries, MAX_DESCRIPTORS + 1)):
            if index == MAX_DESCRIPTORS:
                raise GatedObservationError("selected_gated_descriptor_limit")
            try:
                targets[int(entry.name)] = os.readlink(entry.path)
            except FileNotFoundError:
                continue
    return targets


def native_environment(pid: int) -> frozenset[bytes]:
    with Path(f"/proc/{pid}/environ").open("rb") as source:
        payload = source.read(MAX_ENVIRONMENT + 1)
    if len(payload) > MAX_ENVIRONMENT:
        raise GatedObservationError("selected_gated_environment_limit")
    return frozenset(payload.split(b"\0"))


@contextmanager
def live_pid(pid: int) -> Iterator[None]:
    if sys.platform != "linux":
        raise GatedObservationError("selected_gated_requires_linux")
    identity = os.pidfd_open(pid)
    try:
        if select.select((identity,), (), (), 0)[0]:
            raise GatedObservationError("selected_gated_child_exited")
        yield
        if select.select((identity,), (), (), 0)[0]:
            raise GatedObservationError("selected_gated_child_exited")
    finally:
        os.close(identity)


def verify_role(
    pid: int,
    peer: str,
    expected: dict[int, str],
    key: bytes | None,
) -> None:
    with live_pid(pid):
        targets = descriptor_targets(pid)
        environment = native_environment(pid)
        observed = {item for item in environment if item.startswith(b"CODEX_API_KEY=")}
        if (
            peer in targets.values()
            or any(targets.get(number) != value for number, value in expected.items())
            or any(
                value in expected.values() and number not in expected
                for number, value in targets.items()
            )
            or observed != ({key} if key is not None else set())
        ):
            raise GatedObservationError("selected_gated_role_identity")


def native_launch(argv: tuple[str, ...], environment: dict[str, str]) -> ProcessLaunch:
    return ProcessLaunch(
        executable=argv[0],
        argv=argv,
        environment=tuple(
            EnvironmentVariable(name=name, value=value)
            for name, value in environment.items()
        ),
        working_directory_resource_id="1" * 32,
        stdin=None,
        stdout=None,
        stderr=None,
        domain=ProcessDomain.DETACHED,
    )


def runner_deadline(deadline: float, clock: SystemClock) -> int:
    remaining = deadline - asyncio.get_running_loop().time()
    return clock.monotonic_ns() + max(0, int(remaining * 1_000_000_000))


class SelectedGatedApp:
    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        deadline: float,
        frame_limit: int,
    ) -> None:
        self._clock = SystemClock()
        descriptor = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._owner = start_gated(
                native_launch(argv, environment),
                new_operation_id(),
                descriptor,
                runner_deadline(deadline, self._clock),
                self._clock,
                stdio_limit=frame_limit,
            )
        finally:
            os.close(descriptor)
        self._pid: int | None = None
        self._gate_targets: dict[int, str] = {}
        self._endpoint: Descriptor | None = None
        self._peer: str | None = None
        self._child: str | None = None

    async def prepare(self) -> StdioStreams:
        registration = await self._owner.prepare()
        self._pid = registration.process.pid
        with live_pid(self._pid):
            self._gate_targets = descriptor_targets(self._pid)
        child = self._gate_targets.get(0)
        if (
            child is None
            or not child.startswith("socket:[")
            or self._gate_targets.get(1) != child
            or sum(value == child for value in self._gate_targets.values()) != 2
        ):
            raise GatedObservationError("selected_gated_unexpected_child_stdio")
        await self._owner.acknowledge()
        streams = self._owner.borrow_stdio()
        endpoint = cast(Descriptor, streams[1].get_extra_info("socket"))
        peer = f"socket:[{os.fstat(endpoint.fileno()).st_ino}]"
        if (
            peer == child
            or peer in self._gate_targets.values()
            or child in descriptor_targets(os.getpid()).values()
        ):
            raise GatedObservationError("selected_gated_unexpected_peer")
        self._endpoint = endpoint
        self._peer = peer
        self._child = child
        return streams

    def verify(self, executor: int, provider_key: str) -> None:
        assert self._pid is not None
        assert self._peer is not None
        assert self._child is not None
        verify_role(
            self._pid,
            self._peer,
            {0: self._child, 1: self._child},
            f"CODEX_API_KEY={provider_key}".encode(),
        )
        verify_role(executor, self._peer, {}, None)

    async def close(self, deadline: float) -> None:
        owner = self._owner
        if owner.closed:
            return
        cutoff = runner_deadline(deadline, self._clock)
        owner.close_gate()
        owner.signal(signal.SIGTERM, cutoff)
        term = min(cutoff, self._clock.monotonic_ns() + 2_000_000_000)
        try:
            await owner.settle(term)
        except ChannelError as error:
            if error.reason is not ChannelFailure.DEADLINE:
                raise
            owner.signal(signal.SIGKILL, cutoff)
            await owner.settle(cutoff)
        await owner.drain(cutoff)
        owner.release(cutoff)
        if self._endpoint is not None and self._endpoint.fileno() != -1:
            raise GatedObservationError("selected_gated_parent_endpoint_open")


async def refuse_setup(
    executable: str, deadline: float, provider_key: str, bearer: str
) -> None:
    clock = SystemClock()
    owner: GatedProcess = start_gated(
        native_launch(
            (executable, "gated-argv-canary"),
            {
                "CODEX_API_KEY": provider_key,
                "KORA_PROBE_BEARER": bearer,
                "KORA_PROBE_ENVIRONMENT": "gated-environment-canary",
                "KORA_PROBE_PROTOCOL": "gated-protocol-canary",
            },
        ),
        new_operation_id(),
        -1,
        runner_deadline(deadline, clock),
        clock,
        stdio_limit=131_072,
    )
    if not owner.closed:
        raise GatedObservationError("selected_gated_unexpected_start")
    await owner.prepare()
