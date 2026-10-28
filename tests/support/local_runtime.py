import os
import select
import signal
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.identity import ProcessIdentity
from kora.protocols.time import Clock, SystemClock
from kora.runner.filesystem import DirectoryAnchor, open_local_directory
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    IdentityReader,
    IdentityState,
    NativeIdentityReader,
    ProcessSnapshot,
)
from kora.runner.registry import (
    Applied,
    AttemptKey,
    DomainRecord,
    Entry,
    Registry,
    Transition,
    TransitionExecutor,
    lineage_of,
    open_domain,
)

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


@dataclass
class OpenedRegistry:
    root: DirectoryAnchor
    registry: Registry
    domain: DomainRecord
    executor: TransitionExecutor

    @property
    def execution_domain_id(self) -> str:
        return self.domain.execution_domain_id

    def key(self, attempt_id: str) -> AttemptKey:
        return self.registry.key(attempt_id)

    async def transition(
        self,
        attempt_id: str,
        decide: Callable[[Entry | None], Transition],
        ctx: ProtocolContext,
    ) -> Applied:
        key = self.key(attempt_id)
        return await self.executor.locked(
            lambda: self.registry.try_transition(key, decide), ctx, "transition"
        )

    async def entry(self, attempt_id: str) -> Entry | None:
        key = self.key(attempt_id)
        return await self.executor.run(lambda: self.registry.entry(key))

    async def close(self) -> None:
        await self.executor.close()
        self.registry.directory.close()
        self.root.close()


async def open_registry(
    path: Path,
    ctx: ProtocolContext,
    identities: IdentityReader | None = None,
) -> OpenedRegistry:
    executor = TransitionExecutor()
    new_identifier = Identifiers()
    try:
        reader = ConstructedIdentityReader() if identities is None else identities
        lineage = lineage_of(reader)
        root = await executor.run(
            lambda: open_local_directory(path), DirectoryAnchor.close
        )
        try:
            domain, directory = await executor.locked(
                lambda: open_domain(root, lineage, new_identifier, new_identifier),
                ctx,
                "open",
                lambda opened: opened[1].close(),
            )
        except BaseException:
            root.close()
            raise
    except BaseException:
        await executor.close()
        raise
    registry = Registry(directory, domain.execution_domain_id, new_identifier)
    return OpenedRegistry(root, registry, domain, executor)
