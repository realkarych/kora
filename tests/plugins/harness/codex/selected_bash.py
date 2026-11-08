import asyncio
import os
import signal
import socket
from contextlib import ExitStack
from pathlib import Path

from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.time import SystemClock
from kora.runner.channels import wait_future
from kora.runner.filesystem import DirectoryIdentity
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.installed_executable import InstalledExecutable
from kora.runner.peers import PeerRefused, kernel_source
from kora.runner.processes import ExitAnchor
from tests.plugins.harness.codex.selected_gated_stdio import (
    GatedObservationError,
    live_pid,
    native_environment,
    runner_deadline,
    verify_executed_image,
)
from tests.plugins.harness.codex.selected_launch import (
    BASH_WITNESS_ARGV,
    BASH_WITNESS_SOCKET,
)

CONTROL = Path("/var/lib/kora-hook-probe/control")
WITNESS_PATH = CONTROL / BASH_WITNESS_SOCKET
READY = b"1"


class BashWitness:
    def __init__(self, bash: InstalledExecutable, workspace: DirectoryIdentity) -> None:
        self._bash = bash
        self._workspace = workspace
        self._clock = SystemClock()
        self._reader = NativeIdentityReader(clock=self._clock)
        self._source = kernel_source(self._reader)
        self._owned = ExitStack()
        self._anchors: list[ExitAnchor] = []
        self._task: asyncio.Task[None] | None = None

    def start(self, thread_id: str, call_id: str, deadline: float) -> None:
        if self._task is not None:
            raise GatedObservationError("selected_bash_witness_reused")
        listener = self._owned.enter_context(socket.socket(socket.AF_UNIX))
        listener.setblocking(False)
        listener.bind(str(WITNESS_PATH))
        self._owned.callback(WITNESS_PATH.unlink)
        listener.listen(1)
        self._task = asyncio.create_task(
            self._observe(listener, thread_id, call_id, deadline)
        )

    async def _observe(
        self, listener: socket.socket, thread_id: str, call_id: str, deadline: float
    ) -> None:
        loop = asyncio.get_running_loop()
        async with asyncio.timeout_at(deadline):
            peer, _ = await loop.sock_accept(listener)
            with peer:
                accepted = self._source.stamp()
                if await loop.sock_recv(peer, 2) != READY:
                    raise GatedObservationError("selected_bash_witness_input")
                try:
                    identity = self._source.attest(peer, accepted)
                except PeerRefused:
                    raise GatedObservationError("selected_bash_witness_peer") from None
                self._capture(identity, thread_id, call_id)
                if loop.time() >= deadline:
                    raise GatedObservationError("selected_bash_witness_deadline")
                await loop.sock_sendall(peer, READY)
            await self._wait_reaped(deadline)
            if loop.time() >= deadline:
                raise GatedObservationError("selected_bash_witness_deadline")

    def _capture(self, identity: ProcessIdentity, thread_id: str, call_id: str) -> None:
        staged: list[ExitAnchor] = []
        pid = identity.pid
        with live_pid(pid):
            observed = self._reader.read(pid)
            helper = observed.process
            if (
                observed.state is not IdentityState.PRESENT
                or helper is None
                or helper.identity != identity
                or helper.parent_pid is None
                or helper.parent_pid <= 1
            ):
                raise GatedObservationError("selected_bash_witness_parent")
            bash_pid = helper.parent_pid
            with live_pid(bash_pid):
                current = self._reader.read(pid)
                if (
                    current.state is not IdentityState.PRESENT
                    or current.process != helper
                ):
                    raise GatedObservationError("selected_bash_witness_parent")
                verify_executed_image(bash_pid, self._bash)
                expected = (
                    b"\0".join(os.fsencode(value) for value in BASH_WITNESS_ARGV)
                    + b"\0"
                )
                with Path(f"/proc/{pid}/cmdline").open("rb") as source:
                    if source.read(len(expected) + 1) != expected:
                        raise GatedObservationError("selected_bash_witness_argv")
                expected_environment = {
                    f"CODEX_THREAD_ID={thread_id}".encode(),
                    f"CODEX_TOOL_CALL_ID={call_id}".encode(),
                }
                for member in (pid, bash_pid):
                    cwd = os.stat(f"/proc/{member}/cwd")
                    if (cwd.st_dev, cwd.st_ino) != (
                        self._workspace.device,
                        self._workspace.inode,
                    ) or not expected_environment.issubset(native_environment(member)):
                        raise GatedObservationError("selected_bash_witness_binding")
                    anchor = ExitAnchor(member, direct_child=False)
                    self._owned.callback(anchor.close)
                    if anchor.unknown:
                        raise GatedObservationError("selected_bash_witness_anchor")
                    staged.append(anchor)
        self._anchors.extend(staged)

    async def _wait_reaped(self, deadline: float) -> None:
        cutoff = runner_deadline(deadline, self._clock)
        for anchor in self._anchors:
            await anchor.wait(cutoff, self._clock)
            await wait_future(anchor.next_change, cutoff, self._clock)
            if not anchor.reaped or anchor.unknown:
                raise GatedObservationError("selected_bash_witness_reap")

    async def verify(self) -> None:
        if self._task is None:
            raise GatedObservationError("selected_bash_witness_missing")
        await self._task

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def close(self, deadline: float) -> None:
        self.stop()
        try:
            if self._task is not None:
                await asyncio.gather(self._task, return_exceptions=True)
                if len(self._anchors) != 2:
                    raise GatedObservationError("selected_bash_witness_unsettled")
            for anchor in self._anchors:
                if not anchor.exited:
                    anchor.signal(signal.SIGKILL)
            await self._wait_reaped(deadline)
        finally:
            self._owned.close()


def witness_parent() -> int:
    try:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(10)
            peer.connect(str(WITNESS_PATH))
            peer.sendall(READY)
            return 0 if peer.recv(2) == READY else 1
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(witness_parent())
