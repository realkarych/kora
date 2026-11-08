import asyncio
import os
import select
import signal
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, cast

from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.process_stdio import StdioStreams
from kora.runner.processes import (
    GatedProcess,
    ProcessCondition,
    ProcessError,
    start_gated,
)

MAX_DESCRIPTORS = 256
MAX_ENVIRONMENT = 65_536
GateBoundary = Literal["before-activation", "before-delivery"]


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
def live_pid(pid: int) -> Generator[int]:
    if sys.platform != "linux":
        raise GatedObservationError("selected_gated_requires_linux")
    identity = os.pidfd_open(pid)
    try:
        if select.select((identity,), (), (), 0)[0]:
            raise GatedObservationError("selected_gated_child_exited")
        yield identity
        if select.select((identity,), (), (), 0)[0]:
            raise GatedObservationError("selected_gated_child_exited")
    finally:
        os.close(identity)


def verify_role(
    pid: int,
    identity: int,
    peer: str,
    expected: dict[int, str],
    key: bytes | None,
) -> None:
    if select.select((identity,), (), (), 0)[0]:
        raise GatedObservationError("selected_gated_child_exited")
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
        or select.select((identity,), (), (), 0)[0]
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
    origin = clock.monotonic_ns()
    remaining = deadline - asyncio.get_running_loop().time()
    return origin + max(0, int(remaining * 1_000_000_000))


async def close_gated(
    owner: GatedProcess, cutoff: int, clock: SystemClock, *, natural: bool = False
) -> None:
    owner.close_gate()
    if owner.closed:
        return
    if natural:
        await owner.settle(cutoff)
    else:
        owner.signal(signal.SIGTERM, cutoff)
        term = min(cutoff, clock.monotonic_ns() + 2_000_000_000)
        try:
            await owner.settle(term)
        except ChannelError as error:
            if error.reason is not ChannelFailure.DEADLINE:
                raise
            owner.signal(signal.SIGKILL, cutoff)
            await owner.settle(cutoff)
    await owner.drain(cutoff)
    owner.release(cutoff)


class SelectedGatedApp:
    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        clock: SystemClock,
        cutoff_ns: int,
        frame_limit: int,
        *,
        register: Callable[[GatedProcess], None] | None = None,
    ) -> None:
        self._clock = clock
        descriptor = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._owner = start_gated(
                native_launch(argv, environment),
                new_operation_id(),
                descriptor,
                cutoff_ns,
                self._clock,
                stdio_limit=frame_limit,
            )
            if register is not None:
                register(self._owner)
        finally:
            os.close(descriptor)
        self._pid: int | None = None
        self._gate_targets: dict[int, str] = {}
        self._endpoint: Descriptor | None = None
        self._peer: str | None = None
        self._child: str | None = None

    async def prepare(
        self, boundary: Callable[[GateBoundary], None] | None = None
    ) -> StdioStreams:
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
        if boundary is not None:
            boundary("before-activation")
        await self._owner.acknowledge()
        if boundary is not None:
            boundary("before-delivery")
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

    def verify(self, executor: int, executor_identity: int, provider_key: str) -> None:
        assert self._pid is not None
        assert self._peer is not None
        assert self._child is not None
        with live_pid(self._pid) as app_identity:
            verify_role(
                self._pid,
                app_identity,
                self._peer,
                {0: self._child, 1: self._child},
                f"CODEX_API_KEY={provider_key}".encode(),
            )
        verify_role(executor, executor_identity, self._peer, {}, None)

    def verify_bound_pair(
        self,
        executor: "SelectedGatedApp",
        expected_app: dict[str, str],
        expected_executor: dict[str, str],
    ) -> None:
        assert self._peer is not None
        assert self._child is not None
        assert executor._peer is not None
        assert executor._child is not None
        if len({self._peer, self._child, executor._peer, executor._child}) != 4:
            raise GatedObservationError("selected_bound_streams_shared")
        self.verify_bound_role(expected_app, executor._peer, executor._child)
        executor.verify_bound_role(expected_executor, self._peer, self._child)

    def verify_bound_role(
        self, environment: dict[str, str], other_peer: str, other_child: str
    ) -> None:
        assert self._pid is not None
        assert self._peer is not None
        assert self._child is not None
        expected = frozenset(
            f"{name}={value}".encode() for name, value in environment.items()
        )
        with live_pid(self._pid):
            targets = descriptor_targets(self._pid)
            if (
                targets.get(0) != self._child
                or targets.get(1) != self._child
                or sum(value == self._child for value in targets.values()) != 2
                or not {self._peer, other_peer, other_child}.isdisjoint(
                    targets.values()
                )
                or native_environment(self._pid) != expected.union((b"",))
            ):
                raise GatedObservationError("selected_bound_role_identity")

    async def close(self, deadline: float) -> None:
        await close_gated(
            self._owner, runner_deadline(deadline, self._clock), self._clock
        )
        self.verify_closed()

    def verify_closed(self) -> None:
        if not self._owner.closed:
            raise GatedObservationError("selected_gated_child_not_reaped")
        if self._endpoint is not None and self._endpoint.fileno() != -1:
            raise GatedObservationError("selected_gated_parent_endpoint_open")


async def refuse_setup(
    executable: str,
    deadline: float,
    cleanup_seconds: int,
    provider_key: str,
    bearer: str,
    report_cleanup_failure: Callable[[], None],
) -> None:
    clock = SystemClock()
    with TemporaryDirectory(prefix="kora-codex-gated-", dir="/tmp") as workspace:
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            owner = start_gated(
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
                descriptor,
                runner_deadline(deadline, clock),
                clock,
                stdio_limit=131_072,
            )
        finally:
            os.close(descriptor)
        try:
            await owner.prepare()
            owner.close_gate()
            try:
                await owner.acknowledge()
            except ProcessError as error:
                if error.condition is ProcessCondition.UNKNOWN:
                    return
                raise
            raise GatedObservationError("selected_gated_expected_refusal")
        finally:
            original_failure = sys.exception()
            cutoff = clock.monotonic_ns() + cleanup_seconds * 1_000_000_000
            try:
                await close_gated(owner, cutoff, clock)
            except (ChannelError, ProcessError, OSError):
                if original_failure is None:
                    raise
                report_cleanup_failure()
