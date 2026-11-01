import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Final

from kora.conformance.capture import Capture
from kora.plugins.runtime.local.channel import (
    ENVELOPE_BYTES,
    FrameType,
    StatusValue,
    control_request,
    read_status,
)
from kora.plugins.runtime.local.control import connected
from kora.plugins.runtime.local.serving import (
    COMPONENT,
    BoundSocket,
    ControlServer,
    rebound,
)
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.time import Clock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.peers import (
    CredentialSource,
    DarwinSource,
    PeerCredentials,
    PeerSource,
)
from kora.runner.registry import Registry

SOCKET_NAME: Final[str] = "control-socket"
FAR_NS: Final[int] = 3600 * 1_000_000_000
CONDITION_SECONDS: Final[float] = 10.0
SLEEPER: Final[tuple[str, ...]] = (sys.executable, "-c", "import time; time.sleep(60)")


def judging_source() -> CredentialSource | DarwinSource:
    reader = NativeIdentityReader()
    if sys.platform == "darwin":
        return DarwinSource(reader)
    return CredentialSource(reader)


class AlteredSource:
    def __init__(
        self,
        inner: CredentialSource | DarwinSource,
        *,
        uid_offset: int = 0,
        pid: Callable[[int], int] | None = None,
        stamp_offset: int = 0,
    ) -> None:
        self._inner = inner
        self._uid_offset = uid_offset
        self._pid = pid
        self._stamp_offset = stamp_offset

    def stamp(self) -> int:
        return self._inner.stamp() + self._stamp_offset

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        real = self._inner.credentials(endpoint)
        pid = real.pid if self._pid is None else self._pid(real.pid)
        altered = PeerCredentials(real.uid + self._uid_offset, pid)
        return self._inner.judge(altered, accepted)


class GatedSource:
    def __init__(self, inner: PeerSource) -> None:
        self._inner = inner
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.endpoint: socket.socket | None = None

    def stamp(self) -> int:
        return self._inner.stamp()

    def attest(self, endpoint: socket.socket, accepted: int | None) -> ProcessIdentity:
        self.endpoint = endpoint
        self.entered.set()
        self.gate.wait(CONDITION_SECONDS)
        return self._inner.attest(endpoint, accepted)


class GatedBinder:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.bound: BoundSocket | None = None

    def __call__(self, path: Path, name: str) -> BoundSocket:
        bound = rebound(path, name)
        self.bound = bound
        self.entered.set()
        try:
            if not self.gate.wait(CONDITION_SECONDS):
                raise TimeoutError("rebind barrier was not released")
            return bound
        except BaseException:
            bound.close()
            raise


@contextmanager
def sleeping_process() -> Generator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(SLEEPER)
    try:
        yield process
    finally:
        process.kill()
        process.wait(timeout=CONDITION_SECONDS)


def identity_of(pid: int) -> ProcessIdentity:
    observation = NativeIdentityReader().read(pid)
    assert observation.process is not None
    return observation.process.identity


async def until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + CONDITION_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "the condition did not hold in time"
        await asyncio.sleep(0)


@asynccontextmanager
async def served(
    directory: Path, clock: Clock, source: PeerSource, capture: Capture
) -> AsyncGenerator[tuple[ControlServer, str]]:
    bound = rebound(directory, SOCKET_NAME)
    log = capture.diagnostics.logger(COMPONENT)
    registry = Registry(open_local_directory(directory), "domain-1", lambda: "a" * 32)
    server = ControlServer(
        bound,
        directory,
        SOCKET_NAME,
        clock,
        source,
        log,
        registry,
        registry.key("attempt-1"),
        identity_of(os.getpid()),
        NativeIdentityReader(),
    )
    server.start()
    try:
        yield server, str(directory / SOCKET_NAME)
    finally:
        await server.close()
        registry.directory.close()


async def client(path: str, clock: Clock) -> BoundedChannel:
    endpoint = await connected(path, clock, clock.monotonic_ns() + FAR_NS)
    return BoundedChannel(endpoint, clock)


async def asked_status(channel: BoundedChannel, clock: Clock) -> StatusValue:
    deadline = clock.monotonic_ns() + FAR_NS
    request = control_request("status", "request-1")
    await channel.write_typed_frame(FrameType.JSON, request, deadline)
    reservations = {FrameType.JSON.value: ENVELOPE_BYTES}
    _, payload = await channel.read_typed_frame(deadline, reservations)
    return read_status(payload, "request-1")


def refusals(capture: Capture) -> dict[str, int]:
    counted: dict[str, int] = {}
    for record in capture.finish():
        if record.get("event") == "runtime.local.runner.auth_refused":
            assert record.get("level") == "WARNING"
            assert set(record) >= {"reason", "count"}
            assert "/" not in str(record.get("reason"))
            reason = str(record.get("reason"))
            count = record.get("count")
            assert isinstance(count, int)
            counted[reason] = counted.get(reason, 0) + count
    return counted


def listener_refusals(capture: Capture) -> list[str]:
    return [
        str(record.get("reason"))
        for record in capture.finish()
        if record.get("event") == "runtime.local.listener_refused"
    ]
