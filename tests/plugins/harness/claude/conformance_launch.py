import asyncio
import os
import shutil
import signal
import socket
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import mkdtemp
from typing import get_args

from kora.conformance.harness import HarnessCase, HarnessCaseContext
from kora.plugins.harness.claude.configuration import CLI_VERSION
from kora.plugins.harness.claude.run import settled
from kora.protocols.common import ProtocolContext
from kora.protocols.harness import Launch, LaunchEnd, LaunchInputs, Placement
from kora.protocols.vcs import WorkspaceTarget

RECORDINGS = Path(__file__).with_name("recordings")


def wrapper_text(
    endpoint: Path,
    player: Path,
    inputs: LaunchInputs,
    recording: Path,
    version: str,
    extra_environment: Mapping[str, str],
) -> str:
    environment = (
        dict(inputs.environment)
        | dict(extra_environment)
        | {"KORA_CLAUDE_REPLAY_RECORDING": str(recording)}
    )
    return f"""#!{sys.executable} -I
import os
import signal
import socket
import subprocess
import sys
if sys.argv[1:] == ["-v"]:
    raise SystemExit(0)
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect({str(endpoint)!r})
os.setsid()
connection.sendall((str(os.getpid()) + "\\n").encode())
child = subprocess.Popen(
    [{sys.executable!r}, "-I", {str(player)!r}, {version!r},
     str(connection.fileno())],
    env={environment!r}, cwd={str(inputs.cwd)!r},
    pass_fds=(connection.fileno(),),
)
status = child.wait()
report = f"exit {{status}}\\n" if status >= 0 else f"signal {{-status}}\\n"
connection.sendall(report.encode())
connection.close()
if status < 0:
    os.kill(os.getpid(), -status)
raise SystemExit(status)
"""


class ClaudeLaunch:
    def __init__(self, cli_path: Path, listener: socket.socket) -> None:
        self.cli_path = cli_path
        self.listener = listener
        self.pid: int | None = None
        self.ready = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.initializing = asyncio.Event()
        self.paused = asyncio.Event()
        self.decided = asyncio.Event()
        self.waiting = asyncio.Event()
        self.eof = False
        self.killed = False
        self.kill_requested = asyncio.Event()
        self.report = asyncio.create_task(self.receive())

    async def receive(self) -> LaunchEnd:
        loop = asyncio.get_running_loop()
        connection: socket.socket | None = None
        try:
            try:
                async with asyncio.timeout(5):
                    connection, _ = await loop.sock_accept(self.listener)
                    raw = b""
                    while b"\n" not in raw:
                        part = await loop.sock_recv(connection, 1024)
                        if not part:
                            return LaunchEnd("launch_failed", None, None)
                        raw += part
                    pid, raw = raw.split(b"\n", 1)
                    self.pid = int(pid)
                    self.ready.set()
            except TimeoutError:
                return LaunchEnd("launch_failed", None, None)
            while True:
                if b"interrupt\n" in raw:
                    self.interrupted.set()
                    raw = raw.replace(b"interrupt\n", b"")
                if b"initializing\n" in raw:
                    self.initializing.set()
                    raw = raw.replace(b"initializing\n", b"")
                for notification, event in (
                    (b"pause\n", self.paused),
                    (b"decided\n", self.decided),
                    (b"waiting\n", self.waiting),
                ):
                    if notification in raw:
                        event.set()
                        raw = raw.replace(notification, b"")
                assert len(raw) < 128
                part = await loop.sock_recv(connection, 1024)
                if not part:
                    break
                raw += part
            if not raw:
                return LaunchEnd(
                    "cancel" if self.killed else "launcher_died",
                    None,
                    signal.SIGKILL if self.killed else None,
                )
            kind, number = raw.decode().strip().split()
            return LaunchEnd(
                None,
                int(number) if kind == "exit" else None,
                int(number) if kind == "signal" else None,
            )
        finally:
            self.eof = True
            self.ready.set()
            if connection is not None:
                connection.close()
            self.listener.close()

    async def kill(self) -> None:
        self.kill_requested.set()
        await self.ready.wait()
        if not self.eof:
            assert self.pid is not None
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.killed = True
        await self.ended()

    async def ended(self) -> LaunchEnd:
        return await asyncio.shield(self.report)

    async def close(self) -> None:
        if not self.report.done():
            await self.kill()
        await self.report
        self.listener.close()


class ClaudeLaunches:
    def __init__(
        self,
        root: Path,
        recording: Path,
        version: str = CLI_VERSION,
        extra_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self.fifo = root / "pause.fifo"
        os.mkfifo(self.fifo, 0o600)
        self.extra_environment = dict(extra_environment or {}) | {
            "KORA_CLAUDE_REPLAY_FIFO": str(self.fifo)
        }
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.placements: Mapping[Placement, Path] = {
            name: root / name for name in get_args(Placement)
        }
        for path in self.placements.values():
            path.mkdir()
        self.recording = root / "recording.ndjson"
        shutil.copyfile(recording, self.recording)
        self.version = version
        self.launch: ClaudeLaunch | None = None
        self.inputs: LaunchInputs | None = None
        self.allocated = asyncio.Event()

    def release_pause(self) -> None:
        descriptor = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(descriptor, b"x")
        finally:
            os.close(descriptor)

    async def allocate(self, inputs: LaunchInputs) -> Launch:
        assert self.launch is None
        assert inputs.cwd == self.workspace
        self.inputs = inputs
        directory = self.placements["harness_input"]
        player = directory / "player.py"
        shutil.copyfile(Path(__file__).with_name("conformance_player.py"), player)
        endpoint = self.root / "s"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(endpoint))
            listener.listen(1)
            listener.setblocking(False)
            wrapper = directory / "launch"
            wrapper.write_text(
                wrapper_text(
                    endpoint,
                    player,
                    inputs,
                    self.recording,
                    self.version,
                    self.extra_environment,
                )
            )
            wrapper.chmod(0o700)
            self.launch = ClaudeLaunch(wrapper, listener)
            self.allocated.set()
            return self.launch
        finally:
            if self.launch is None:
                listener.close()

    async def close(self) -> None:
        if self.launch is not None:
            await self.launch.close()
        shutil.rmtree(self.root)


class ContextOwner:
    def __init__(self) -> None:
        self.active: ClaudeLaunches | None = None
        self.cases: list[ClaudeLaunches] = []

    @asynccontextmanager
    async def context(
        self, case: HarnessCase, ctx: ProtocolContext
    ) -> AsyncGenerator[HarnessCaseContext]:
        current = ClaudeLaunches(
            Path(mkdtemp(prefix="kc", dir="/tmp")),
            RECORDINGS / f"conformance-{case.value}-2.1.301.ndjson",
        )
        self.active = current
        self.cases.append(current)
        try:
            yield HarnessCaseContext(
                WorkspaceTarget(
                    root=str(current.workspace), host_path=str(current.workspace)
                ),
                current,
            )
        finally:
            await settled(asyncio.create_task(current.close()))
            self.active = None


async def wait_signal(event: asyncio.Event, operation: asyncio.Task[object]) -> None:
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait({waiter, operation}, return_when=asyncio.FIRST_COMPLETED)
        if operation.done():
            operation.result()
        assert event.is_set()
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
