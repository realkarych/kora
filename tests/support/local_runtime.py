import json
import os
import select
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.plugins.runtime.local.runtime import LocalRuntime, LocalRuntimeConfiguration
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import ProcessIdentity
from kora.protocols.time import Clock, SystemClock
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    IdentityReader,
    IdentityState,
    NativeIdentityReader,
    ProcessSnapshot,
)
from kora.runner.registry import MAX_BOOTS, Entry

FIXTURE_ROOT: Final[Path] = Path("/opt/kora-local-runtime")
PATH_MARKER: Final[str] = "kora-path-marker"
HOLDER: Final[str] = """
import fcntl, os, signal, sys
descriptor = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(descriptor, fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
signal.pause()
"""
READY_DEADLINE_SECONDS: Final[float] = 30.0


def prepared(name: str) -> Path:
    path = FIXTURE_ROOT / name
    if not path.exists():
        pytest.fail(
            f"host fixture {name} is not prepared; run "
            "scripts/ci/prepare-local-runtime-fixtures.sh",
            pytrace=False,
        )
    return path


def private_directory(parent: Path, name: str) -> Path:
    directory = parent / name
    directory.mkdir(mode=0o700)
    return directory


def state_root(tmp_path: Path) -> Path:
    return private_directory(tmp_path, f"{PATH_MARKER}-state")


class ConstructedIdentityReader:
    def __init__(
        self,
        host_id: str | None = "a" * 32,
        boot_id: str | None = "boot-1",
        pid_namespace: str | None = "pid:[4026531836]",
    ) -> None:
        self.host_id = host_id
        self.boot_id = boot_id
        self.pid_namespace = pid_namespace

    def read(self, pid: int) -> IdentityObservation:
        identity = ProcessIdentity(
            host_id=self.host_id,
            boot_id=self.boot_id,
            pid_namespace=self.pid_namespace,
            pid=pid,
            start_time=1,
            leads_group=False,
        )
        return IdentityObservation(
            IdentityState.PRESENT, ProcessSnapshot(identity, pid, pid, False)
        )

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        return GroupObservation(IdentityState.UNKNOWN)


Steering = Callable[[IdentityObservation, int], IdentityObservation]


class SteeredIdentityReader:
    def __init__(self) -> None:
        self.native = NativeIdentityReader()
        self.steered: dict[int, Steering] = {}
        self.reads: dict[int, int] = {}

    def read(self, pid: int) -> IdentityObservation:
        observation = self.native.read(pid)
        steering = self.steered.get(pid)
        if steering is None:
            return observation
        self.reads[pid] = self.reads.get(pid, 0) + 1
        return steering(observation, self.reads[pid])

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        return self.native.group(process_group_id, deadline_ns)


def unreadable(observation: IdentityObservation, count: int) -> IdentityObservation:
    return IdentityObservation(IdentityState.UNKNOWN)


def restarted(observation: IdentityObservation, count: int) -> IdentityObservation:
    process = observation.process
    if count < 2 or process is None:
        return observation
    identity = process.identity.model_copy(
        update={"start_time": process.identity.start_time + 1}
    )
    return IdentityObservation(
        observation.state,
        ProcessSnapshot(
            identity, process.process_group_id, process.session_id, process.dead
        ),
    )


def frozen() -> Steering:
    first: list[IdentityObservation] = []

    def steer(observation: IdentityObservation, count: int) -> IdentityObservation:
        if not first:
            first.append(observation)
        return first[0]

    return steer


def gated_steering(entered: threading.Event, release: threading.Event) -> Steering:
    def steer(observation: IdentityObservation, count: int) -> IdentityObservation:
        entered.set()
        assert release.wait(READY_DEADLINE_SECONDS)
        return observation

    return steer


def runtime_context(
    capture: Capture,
    clock: Clock | None = None,
    deadline: ExecutionDeadline | None = None,
) -> ProtocolContext:
    return ProtocolContext(
        namespace_id="local-runtime",
        run_id=None,
        attempt_id=None,
        logger=capture.diagnostics.logger(CONFORMANCE_COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=deadline,
        clock=SystemClock() if clock is None else clock,
        activity=NoActivity(),
    )


def open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def observer_namespace_text() -> str:
    if sys.platform == "darwin":
        observation = NativeIdentityReader().read(os.getpid())
        assert observation.process is not None
        boot = observation.process.identity.boot_id
        assert boot is not None
        return boot
    return os.readlink("/proc/self/ns/pid")


@contextmanager
def stopped_holder(lock: Path) -> Generator[subprocess.Popen[bytes]]:
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(lock)], stdout=subprocess.PIPE
    )
    try:
        assert holder.stdout is not None
        ready, _, _ = select.select([holder.stdout], [], [], READY_DEADLINE_SECONDS)
        assert ready
        assert holder.stdout.readline() == b"held\n"
        os.kill(holder.pid, signal.SIGSTOP)
        yield holder
    finally:
        holder.kill()
        holder.wait(timeout=READY_DEADLINE_SECONDS)
        if holder.stdout is not None:
            holder.stdout.close()


class Identifiers:
    def __init__(self) -> None:
        self.next = 0

    def __call__(self) -> str:
        self.next += 1
        return f"{self.next:032x}"


class GatedIdentifiers(Identifiers):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.started = threading.Event()
        self.gate = threading.Event()

    def __call__(self) -> str:
        worker = threading.current_thread().name.startswith("kora-runtime-local")
        if self.armed and worker:
            self.armed = False
            self.started.set()
            self.gate.wait(READY_DEADLINE_SECONDS)
        return super().__call__()


async def open_runtime(
    path: Path,
    ctx: ProtocolContext,
    identities: IdentityReader | None = None,
    new_identifier: Callable[[], str] | None = None,
    socket_directory: Path | None = None,
) -> LocalRuntime:
    return await LocalRuntime.open(
        LocalRuntimeConfiguration(state_root=str(path)),
        ctx,
        identities=ConstructedIdentityReader() if identities is None else identities,
        new_identifier=Identifiers() if new_identifier is None else new_identifier,
        socket_directory=socket_directory,
    )


async def recorded(runtime: LocalRuntime, attempt_id: str) -> Entry | None:
    return await runtime.entry(runtime.registry.key(attempt_id))


async def protocol_error(outcome: Awaitable[object]) -> KoraProtocolError:
    try:
        await outcome
    except KoraProtocolError as error:
        return error
    pytest.fail("the operation did not raise a protocol error", pytrace=False)


def fill_boot_log(root: Path) -> None:
    record = {
        "format": "local/1",
        "execution_domain_id": "f" * 32,
        "host_id": "a" * 32,
        "boots": [
            {"boot_id": f"boot-old-{index}", "pid_namespace": "pid:[1]"}
            for index in range(MAX_BOOTS)
        ],
    }
    (root / "domain.json").write_text(json.dumps(record))
    (root / "domain.json").chmod(0o600)
    (root / "registry").mkdir(mode=0o700)
