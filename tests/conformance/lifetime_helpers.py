import asyncio
import fcntl
import os
import signal
import socket
import subprocess
import sys
import termios
from collections.abc import Sequence
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from kora.conformance.io_helper import (
    ChildReaper,
    InstalledIOHelperLauncher,
    IOHelper,
    KernelChildReaper,
)
from kora.protocols.identity import ProcessIdentity
from kora.protocols.time import Clock
from kora.runner.channels import wait_future
from kora.runner.identity import (
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.processes import ExitAnchor, RecoveredProcess, SignalResult

if TYPE_CHECKING:
    from kora.conformance.lifetime import ParentLifetime

HELPER_START_GUARD_SECONDS: float = 5.0


async def unwind_owned(owned: AsyncExitStack) -> None:
    await owned.__aexit__(*sys.exc_info())


def unread_bytes(endpoint: socket.socket) -> int:
    return int.from_bytes(
        fcntl.ioctl(endpoint.fileno(), termios.FIONREAD, bytes(4)), sys.byteorder
    )


async def turn() -> None:
    loop = asyncio.get_running_loop()
    wake: asyncio.Future[None] = loop.create_future()
    loop.call_soon(wake.set_result, None)
    await wake


class CleanupDeadline:
    def __init__(self, clock: Clock, budget_ns: int) -> None:
        self.clock = clock
        self.budget_ns = budget_ns
        self.value: int | None = None

    def __call__(self) -> int:
        if self.value is None:
            self.value = self.clock.monotonic_ns() + self.budget_ns
        return self.value


def signal_recovered(process: RecoveredProcess, cleanup: CleanupDeadline) -> None:
    process.signal(signal.SIGKILL, cleanup())


async def kill_observed(process: RecoveredProcess, deadline: int, clock: Clock) -> None:
    while process.signal(signal.SIGKILL, deadline) is SignalResult.UNKNOWN:
        if clock.monotonic_ns() >= deadline:
            return
        await turn()


async def release_recovered(
    process: RecoveredProcess, cleanup: CleanupDeadline, clock: Clock
) -> None:
    deadline = cleanup()
    try:
        await kill_observed(process, deadline, clock)
        await process.settle(deadline)
        process.release(deadline)
    finally:
        if not process.closed:
            process.dispose_observation()


async def stop_child(
    process: subprocess.Popen[bytes],
    anchor: ExitAnchor,
    cleanup: CleanupDeadline,
    clock: Clock,
) -> None:
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
        if process.returncode is None:
            await anchor.wait(cleanup(), clock)
    finally:
        anchor.close()
        if process.poll() is None:
            process.returncode = await reap_once_reapable(
                KernelChildReaper(), process.pid, cleanup(), clock
            )


async def reap_once_reapable(
    reaper: ChildReaper, pid: int, deadline: int, clock: Clock
) -> int:
    while (returncode := reaper.reap(pid)) is None:
        watch = reaper.watch(pid)
        try:
            returncode = reaper.reap(pid)
            if returncode is not None:
                return returncode
            await wait_future(watch.changed, deadline, clock)
        finally:
            watch.close()
    return returncode


async def stop_io_helper(helper: IOHelper, cleanup: CleanupDeadline) -> None:
    try:
        if not helper.closed:
            deadline = cleanup()
            assert helper.signal(signal.SIGKILL, deadline)
            assert await helper.settle(deadline)
    finally:
        with ExitStack() as local:
            local.callback(helper.process.poll)
            local.callback(helper.anchor.close)
            local.callback(helper.channel.close)
            for descriptor in helper.take_descriptors():
                local.callback(os.close, descriptor)


async def join_helper_operation(
    operation: asyncio.Task[object], observed: Sequence[BaseException] = ()
) -> None:
    if operation.done():
        if not operation.cancelled():
            error = operation.exception()
            if error is not None and not any(error is known for known in observed):
                raise error
        return
    operation.cancel()
    try:
        await operation
    except asyncio.CancelledError:
        assert operation.cancelled()


class RetainedHelperGroup:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.identities = NativeIdentityReader(clock=clock)
        self.leader = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            process_group=0,
        )
        self.anchor = ExitAnchor(self.leader.pid)
        self.stopped: tuple[ProcessIdentity, ...] = ()
        self.exited: tuple[ProcessIdentity, ...] = ()

    @property
    def process_group_id(self) -> int:
        return self.leader.pid

    def scan(self, deadline: int) -> tuple[IdentityObservation, ...] | None:
        group = self.identities.group(self.leader.pid, deadline)
        members = tuple(
            member for pid, member in group.members if pid != self.leader.pid
        )
        if group.state is not IdentityState.PRESENT or any(
            member.state is IdentityState.UNKNOWN for member in members
        ):
            return None
        return members

    async def live_members(self, deadline: int) -> tuple[ProcessIdentity, ...]:
        while (members := self.scan(deadline)) is None:
            assert self.clock.monotonic_ns() < deadline
            await turn()
        return tuple(
            member.process.identity
            for member in members
            if member.process is not None and not member.process.dead
        )

    async def known(self, pid: int, deadline: int) -> IdentityObservation:
        observed = self.identities.read(pid)
        while observed.state is IdentityState.UNKNOWN:
            assert self.clock.monotonic_ns() < deadline
            await turn()
            observed = self.identities.read(pid)
        return observed

    async def member_exit(self, identity: ProcessIdentity, deadline: int) -> None:
        anchor = ExitAnchor(identity.pid, direct_child=False)
        try:
            current = (await self.known(identity.pid, deadline)).process
            if (
                current is not None
                and current.identity == identity
                and not current.dead
            ):
                await anchor.wait(deadline, self.clock)
            self.exited = (*self.exited, identity)
        finally:
            anchor.close()

    async def close(self, cleanup: CleanupDeadline) -> None:
        deadline = cleanup()
        try:
            self.stopped = await self.live_members(deadline)
            os.killpg(self.leader.pid, signal.SIGKILL)
            await self.anchor.wait(deadline, self.clock)
            for identity in self.stopped:
                await self.member_exit(identity, deadline)
            assert not await self.live_members(deadline)
        finally:
            await self.release_leader(deadline)

    async def release_leader(self, deadline: int) -> None:
        try:
            if self.leader.stdin is not None:
                self.leader.stdin.close()
            if not self.anchor.exited:
                kill_remaining(self.leader.pid)
                await self.anchor.wait(deadline, self.clock)
        finally:
            self.anchor.close()
            self.leader.poll()


def kill_remaining(process_group_id: int) -> None:
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return


def lifetime_failure_state(owner: "ParentLifetime") -> str:
    control = owner.invocation_cleanup
    return repr(
        {
            "clock": owner.clock.monotonic_ns(),
            "close": (control.start, control.cooperative, control.term, control.final),
            "closure": (control.result.done(), control.failed, control.published),
            "inventory": owner.inventory(),
            "helpers": tuple(
                (
                    helper.process.pid,
                    helper.closed,
                    helper.anchor.exited,
                    helper.anchor.unknown,
                    helper.process.returncode,
                )
                for helper in owner.mutations.helpers[:16]
            ),
            "tasks": tuple(
                (frame.f_code.co_name[:64], frame.f_lineno)
                for task in tuple(asyncio.all_tasks())[:16]
                for frame in task.get_stack(limit=1)
            ),
        }
    )


class ProbeIOHelperLauncher:
    def __init__(
        self,
        home: Path,
        observation_descriptor: int,
        fifo_directory: int | None = None,
        phase: str = "before",
        pause_at: int | tuple[int, ...] | None = None,
        resist_term: bool = False,
        process_group: int = 0,
        reaper: ChildReaper | None = None,
        helper_type: type[IOHelper] = IOHelper,
    ) -> None:
        self.reaper = reaper
        self.helper_type = helper_type
        self.home = home
        self.observation_descriptor = observation_descriptor
        self.fifo_directory = fifo_directory
        self.phase = phase
        self.pause_at = pause_at
        self.resist_term = resist_term
        self.process_group = process_group
        self.starts = 0
        self.start_waiters: list[asyncio.Future[None]] = []

    async def wait_for_start(
        self, count: int, watched: asyncio.Task[object] | None = None
    ) -> None:
        while self.starts < count:
            started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            waiting: set[asyncio.Future[None] | asyncio.Task[object]] = {started}
            if watched is not None:
                waiting.add(watched)
            self.start_waiters.append(started)
            try:
                await asyncio.wait(
                    waiting,
                    timeout=HELPER_START_GUARD_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                self.start_waiters.remove(started)
                started.cancel()
            if started.cancelled() and watched is not None and watched.done():
                raise AssertionError(
                    f"completed before helper start {count}: {watched.result()!r}"
                )
            if started.cancelled():
                raise TimeoutError(f"helper start {self.starts + 1} did not begin")

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        self.starts += 1
        for waiter in self.start_waiters:
            if not waiter.done():
                waiter.set_result(None)
        selected = (self.pause_at,) if isinstance(self.pause_at, int) else self.pause_at
        if selected is not None and self.starts not in selected:
            return InstalledIOHelperLauncher(
                home=self.home, state_home=self.home / "state", reaper=self.reaper
            ).start(descriptors, clock)
        parent, child = socket.socketpair()
        inherited = (child.fileno(), self.observation_descriptor, *descriptors)
        environment = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / "state"),
            "KORA_IO_CONTROL_FD": str(child.fileno()),
            "KORA_IO_DESCRIPTORS": ",".join(str(value) for value in descriptors),
            "KORA_TEST_OBSERVATION_FD": str(self.observation_descriptor),
            "KORA_TEST_PAUSE_PHASE": self.phase,
            "KORA_TEST_RESIST_TERM": "1" if self.resist_term else "0",
        }
        if self.fifo_directory is not None:
            inherited = (*inherited, self.fifo_directory)
            environment["KORA_TEST_FIFO_DIRECTORY_FD"] = str(self.fifo_directory)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("lifetime_io_probe.py")),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
                pass_fds=inherited,
                process_group=self.process_group,
            )
        except OSError:
            parent.close()
            raise
        finally:
            child.close()
        return self.helper_type(process, parent, clock, self.reaper)


async def stalled_lifetime_parent(
    home: Path,
    invocation_id: str,
    control: int,
    observation: int,
    fifo: int,
    sink_mode: str,
    scenario: str,
    process_group: int,
) -> None:
    import asyncio

    from kora.conformance.common import utc_now
    from kora.conformance.diagnostics import COMPONENT
    from kora.conformance.lifetime import LifetimeError, ParentLifetime
    from kora.observability import Level, LoggingSettings, configure_process
    from kora.protocols.lifetime import (
        Acquisition,
        CleanupReference,
        LifetimeLimits,
        ReservationRequest,
        ResourceKind,
        TemporaryTreeRequest,
        new_operation_id,
    )
    from kora.protocols.time import SystemClock
    from kora.runner.channels import BoundedChannel

    diagnostics = configure_process(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "lifetime-exit", utc_now
    )
    clock = SystemClock()
    channel = BoundedChannel(socket.socket(fileno=control), clock)
    pause_at = 5 if scenario == "publisher" else 1 if scenario == "claim" else 2
    launcher = ProbeIOHelperLauncher(
        home,
        observation,
        fifo,
        phase="after" if scenario == "claim" else "before",
        pause_at=pause_at,
        process_group=process_group,
    )
    owner = ParentLifetime(
        limits=LifetimeLimits(
            cooperative_seconds=0.1,
            term_seconds=0.1,
            kill_observation_seconds=2.0,
            drain_seconds=1.0,
        ),
        launcher=launcher,
        recovery=None
        if scenario == "publisher"
        else CleanupReference(invocation_id=invocation_id),
    )
    pending: asyncio.Task[Acquisition] | None = None
    if scenario == "publisher":
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        pending = asyncio.create_task(
            owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        )
    deadline = owner.deadline()
    while launcher.starts < pause_at:
        if clock.monotonic_ns() >= deadline:
            raise TimeoutError
        await turn()
    while (sink_mode == "broken" and not diagnostics.snapshot().sink_failed) or (
        sink_mode == "blocked" and diagnostics.writer_is_idle()
    ):
        if clock.monotonic_ns() >= deadline:
            raise TimeoutError
        await turn()
    await channel.write_frame(sink_mode.encode("ascii"), deadline)
    await channel.write_frame(owner.invocation_id.encode("ascii"), deadline)
    helper = owner.mutations.helpers[-1]
    if helper.identity is None:
        raise RuntimeError("helper identity unavailable")
    await channel.write_frame(helper.identity.model_dump_json().encode(), deadline)
    if await channel.read_frame(deadline) != b"close":
        raise ValueError("close control required")
    final_deadline = clock.monotonic_ns() + int(
        (
            owner.limits.cooperative_seconds
            + owner.limits.term_seconds
            + owner.limits.kill_observation_seconds
            + owner.limits.drain_seconds
        )
        * 1_000_000_000
    )
    report = await owner.close(None)
    if pending is not None:
        try:
            await pending
        except LifetimeError:
            if not pending.done():
                raise
    diagnostics.shutdown(
        max(0.0, min(1.0, (final_deadline - clock.monotonic_ns()) / 1_000_000_000))
    )
    await channel.write_record(report.model_dump_json().encode(), deadline)
    channel.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        stalled_lifetime_parent(
            Path(sys.argv[1]),
            sys.argv[2],
            int(sys.argv[3]),
            int(sys.argv[4]),
            int(sys.argv[5]),
            sys.argv[6],
            sys.argv[7],
            int(sys.argv[8]),
        )
    )
    sys.exit(1)
