import asyncio
import json
import os
import shutil
import signal
import socket
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import mkdtemp
from typing import TypeVar, get_args

from pydantic import BaseModel, ConfigDict

from kora.conformance.harness import HarnessCase, HarnessCaseContext
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import Launch, LaunchEnd, LaunchInputs, Placement
from kora.protocols.lifetime import (
    ActivationResult,
    CleanupOutcome,
    ConformanceLifetime,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRegistration,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.vcs import WorkspaceTarget

T = TypeVar("T")


async def settled(task: asyncio.Task[T]) -> T:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class Handshake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pid: int
    cwd: str
    placements: list[str]


class FixtureLaunch:
    def __init__(self, cli_path: Path, startup_pause: asyncio.Event | None) -> None:
        self.cli_path = cli_path
        self.startup_pause = startup_pause
        self.process: asyncio.subprocess.Process | None = None
        self.starting: asyncio.Task[asyncio.subprocess.Process] | None = None
        self.ready = asyncio.Event()
        self.closed = False
        self.killed = False
        self.handshake: Handshake | None = None

    async def start(
        self, inputs: LaunchInputs, placements: Mapping[Placement, Path]
    ) -> None:
        assert self.starting is None
        assert not self.closed
        self.starting = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(self.cli_path),
                *(str(path) for path in placements.values()),
                cwd=inputs.cwd,
                env=dict(inputs.environment),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=8192,
            )
        )
        try:
            await settled(self.starting)
        finally:
            if self.starting.done() and not self.starting.cancelled():
                self.process = self.starting.result()
                self.ready.set()
        assert self.process is not None
        assert self.process.stdout is not None
        prefix = await self.process.stdout.readexactly(1)
        if self.startup_pause is not None:
            self.startup_pause.set()
            await asyncio.get_running_loop().create_future()
        self.handshake = Handshake.model_validate_json(
            prefix + await self.process.stdout.readline()
        )
        assert self.handshake.pid == self.process.pid
        assert self.handshake.cwd == str(inputs.cwd)
        assert self.handshake.placements == [str(path) for path in placements.values()]
        assert all(
            (path / "accessed").read_text() == "child" for path in placements.values()
        )

    async def command(self, command: str) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        self.process.stdin.write((command + "\n").encode())
        await self.process.stdin.drain()
        if command in {"allowed", "denied", "hold"}:
            assert self.process.stdout is not None
            assert await self.process.stdout.readline() == (command + "\n").encode()

    async def ended(self) -> LaunchEnd:
        await self.ready.wait()
        if self.process is None:
            return LaunchEnd("launch_failed", None, None)
        code = await self.process.wait()
        return LaunchEnd(
            "cancel" if self.killed else None,
            code if code >= 0 else None,
            -code if code < 0 else None,
        )

    async def kill(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.killed = True
            self.process.kill()
        if self.process is not None:
            await self.process.wait()

    async def close(self) -> None:
        if self.closed:
            return
        await self.kill()
        if self.process is not None:
            assert self.process.stdout is not None
            assert self.process.stdin is not None
            self.process.stdin.close()
            await self.process.stdin.wait_closed()
            remaining = 8192
            while payload := await self.process.stdout.read(remaining + 1):
                remaining -= len(payload)
                assert remaining >= 0
            assert self.process.stdout.at_eof()
        self.closed = True
        self.ready.set()


class CaseLaunches:
    def __init__(self, root: Path, mode: str, startup_pause: asyncio.Event) -> None:
        self.root = root
        self.mode = mode
        self.startup_pause = startup_pause
        self.workspace = root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.placements: Mapping[Placement, Path] = {
            name: root / name for name in get_args(Placement)
        }
        for path in self.placements.values():
            path.mkdir(mode=0o700)
        self.launch: FixtureLaunch | None = None
        self.closed = False
        self.loop = asyncio.get_running_loop()
        self.cleanup_loop: asyncio.AbstractEventLoop | None = None

    async def allocate(self, inputs: LaunchInputs) -> Launch:
        assert self.launch is None
        assert not self.closed
        assert inputs.cwd == self.workspace
        executable = self.placements["harness_input"] / "child"
        executable.write_text(
            "#!"
            + sys.executable
            + "\n"
            + Path(__file__).with_name("launching_child.py").read_text()
        )
        executable.chmod(0o700)
        self.launch = FixtureLaunch(
            executable, self.startup_pause if self.mode == "startup_cancel" else None
        )
        if self.mode == "allocation_failure":
            raise RuntimeError("allocation failed")
        return self.launch

    async def close(self) -> None:
        self.cleanup_loop = asyncio.get_running_loop()
        assert self.cleanup_loop is self.loop
        if self.launch is not None:
            await self.launch.close()
        self.closed = True


class ContextOwner:
    def __init__(self, root: Path | None = None, mode: str = "success") -> None:
        self.root = root
        self.mode = mode
        self.lifetime: ConformanceLifetime | None = None
        self.feedback: str | None = None
        self.reported = False
        self.cases: list[CaseLaunches] = []
        self.active: CaseLaunches | None = None
        self.entered = asyncio.Event()
        self.startup_pause = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()

    @asynccontextmanager
    async def context(
        self, case: HarnessCase, protocol: ProtocolContext
    ) -> AsyncGenerator[HarnessCaseContext]:
        resource_id: str | None = None
        root: Path | None = None
        current: CaseLaunches | None = None
        try:
            if self.lifetime is None:
                assert self.root is not None
                root = Path(mkdtemp(dir=self.root))
            else:
                reservation = await self.lifetime.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                resource_id = reservation.token.resource_id
                acquisition = await self.lifetime.acquire(
                    reservation.token, new_operation_id(), TemporaryTreeRequest()
                )
                registration = acquisition.registration
                assert isinstance(registration, TemporaryTreeRegistration)
                root = Path.cwd().parent / registration.name
                metadata = root.stat()
                assert (metadata.st_dev, metadata.st_ino, metadata.st_uid) == (
                    registration.device,
                    registration.inode,
                    registration.owner_uid,
                )
                activation = await self.lifetime.acknowledge(
                    reservation.token, acquisition.operation_id, acquisition.revision
                )
                assert activation.result is ActivationResult.ACTIVE
            current = CaseLaunches(root, self.mode, self.startup_pause)
            self.cases.append(current)
            self.active = current
            self.entered.set()
            if self.mode == "acquisition_failure":
                raise RuntimeError("acquisition failed")
            if self.mode == "acquisition_cancel":
                await asyncio.get_running_loop().create_future()
            yield HarnessCaseContext(
                WorkspaceTarget(
                    root=str(current.workspace), host_path=str(current.workspace)
                ),
                current,
            )
        finally:
            cleanup = asyncio.create_task(self.close(current, root, resource_id))
            try:
                await settled(cleanup)
            finally:
                if (
                    cleanup.done()
                    and not cleanup.cancelled()
                    and cleanup.exception() is None
                ):
                    self.active = None

    async def close(
        self, current: CaseLaunches | None, root: Path | None, resource_id: str | None
    ) -> None:
        self.cleanup_entered.set()
        if self.mode == "cleanup_cancel":
            await self.cleanup_release.wait()
        async with asyncio.timeout(5):
            if current is not None:
                await current.close()
            if self.lifetime is not None and resource_id is not None:
                report = await self.lifetime.close(resource_id)
                assert report.state is CleanupOutcome.CLOSED
            elif root is not None:
                shutil.rmtree(root)

    async def report_started(self, current: CaseLaunches) -> None:
        if self.feedback is None or self.reported:
            return
        self.reported = True
        launch = current.launch
        assert launch is not None
        assert launch.process is not None
        if self.mode in {"installed_cancel", "pytest_exit"}:
            await launch.command("hold")
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
            endpoint.setblocking(False)
            await loop.sock_connect(endpoint, self.feedback)
            payload = json.dumps(
                {"pid": launch.process.pid, "root": str(current.root)}
            ).encode()
            await loop.sock_sendall(endpoint, payload + b"\n")
            assert await loop.sock_recv(endpoint, 1) == b"a"
        if self.mode == "pytest_exit":
            os.kill(os.getpid(), signal.SIGKILL)
        if self.mode == "installed_cancel":
            await loop.create_future()
