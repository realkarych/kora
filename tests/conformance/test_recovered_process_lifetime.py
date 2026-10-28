import asyncio
import ctypes
import errno
import os
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from kora.protocols.lifetime import ProcessRegistration
from kora.protocols.time import SystemClock
from kora.runner.identity import (
    GroupObservation,
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.processes import (
    ExitAnchor,
    ProcessCondition,
    ProcessError,
    RecoveredProcess,
    SignalResult,
)


class ReapedDomain:
    def __init__(
        self,
        leader: socket.socket,
        reaper: socket.socket,
        registration: ProcessRegistration,
    ) -> None:
        self.leader = leader
        self.reaper = reaper
        self.registration = registration
        self.reaped = False
        self.member: socket.socket | None = None
        self.member_pid: int | None = None

    async def reap(self) -> int:
        value = await asyncio.wait_for(
            asyncio.get_running_loop().sock_recv(self.reaper, 64), 5
        )
        self.reaped = True
        return int(value)


@asynccontextmanager
async def domain(mode: str) -> AsyncGenerator[ReapedDomain]:
    clock = SystemClock()
    with tempfile.TemporaryDirectory(prefix="kr-", dir="/tmp") as directory:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address = str(Path(directory) / "s")
        listener.bind(address)
        listener.listen(2)
        listener.setblocking(False)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(
                    Path(__file__).resolve().parents[1]
                    / "support/recovered_process_child.py"
                ),
                address,
                mode,
                "reaper",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        anchor = ExitAnchor(process.pid)
        loop = asyncio.get_running_loop()
        reaper, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
        assert await asyncio.wait_for(loop.sock_recv(reaper, 64), 5) == b"reaper"
        leader, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
        pid = int(await asyncio.wait_for(loop.sock_recv(leader, 64), 5))
        observed = NativeIdentityReader().read(pid)
        assert observed.process is not None
        registration = ProcessRegistration(
            process=observed.process.identity,
            process_group_id=observed.process.process_group_id,
            session_id=observed.process.session_id,
            acquisition_operation_id="a" * 32,
        )
        owned = ReapedDomain(leader, reaper, registration)
        member_anchor = None
        if mode == "family":
            owned.member, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
            owned.member_pid = int(
                await asyncio.wait_for(loop.sock_recv(owned.member, 64), 5)
            )
            member_anchor = ExitAnchor(owned.member_pid, direct_child=False)
        try:
            yield owned
        finally:
            leader.close()
            if owned.member is not None:
                owned.member.close()
            if member_anchor is not None:
                await member_anchor.wait(clock.monotonic_ns() + 5_000_000_000, clock)
                member_anchor.close()
            if not owned.reaped:
                await owned.reap()
            reaper.close()
            await anchor.wait(clock.monotonic_ns() + 5_000_000_000, clock)
            process.wait()
            anchor.close()
            listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "resistant"])
async def test_recovered_nonchild_signal_requires_live_leader_and_actual_reaper(
    mode: str,
) -> None:
    clock = SystemClock()
    async with domain(mode) as fixture:
        deadline = clock.monotonic_ns() + 5_000_000_000
        owner = RecoveredProcess(fixture.registration, clock, NativeIdentityReader())

        before = owner.observe(deadline)
        term = owner.signal(signal.SIGTERM, deadline)
        if mode == "resistant":
            assert owner.signal(signal.SIGKILL, deadline) is SignalResult.SENT
        status = await fixture.reap()
        await owner.settle(deadline)
        owner.release(deadline)
        owner.release(deadline)

        assert before.live_members == (fixture.registration.process,)
        assert term is SignalResult.SENT
        assert status == -(signal.SIGTERM if mode == "normal" else signal.SIGKILL)
        assert owner.closed
        assert owner.watcher_count == 0
        with pytest.raises(ChildProcessError):
            os.waitpid(fixture.registration.process.pid, os.WNOHANG)


class RecoveryIdentityDecisions:
    def __init__(self, pid: int) -> None:
        self.native = NativeIdentityReader()
        self.pid = pid
        self.reads = 0
        self.failure: str | None = None
        self.omit = False

    def read(self, pid: int) -> IdentityObservation:
        result = self.native.read(pid)
        if pid != self.pid:
            return result
        self.reads += 1
        if self.failure == "unreadable":
            return IdentityObservation(IdentityState.UNKNOWN)
        process = result.process
        if process is not None and self.failure in ("reused", "race", "scope"):
            if self.failure == "race" and self.reads == 1:
                return result
            identity = process.identity.model_copy(
                update={"host_id": "foreign"}
                if self.failure == "scope"
                else {"start_time": process.identity.start_time + 1}
            )
            return replace(result, process=replace(process, identity=identity))
        return result

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        result = self.native.group(process_group_id, deadline_ns)
        return (
            replace(
                result,
                members=tuple(
                    value for value in result.members if value[0] == self.pid
                ),
            )
            if self.omit
            else result
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "reused", "race", "scope"])
async def test_recovery_identity_refusal_disposes_partial_watchers(
    failure: str,
) -> None:
    clock = SystemClock()
    async with domain("normal") as fixture:
        source = RecoveryIdentityDecisions(fixture.registration.process.pid)
        source.failure = failure
        baseline = set(os.listdir("/dev/fd"))
        owner = RecoveredProcess(fixture.registration, clock, source)
        deadline = clock.monotonic_ns() + 5_000_000_000

        observed = owner.observe(deadline)
        refused = owner.signal(signal.SIGKILL, deadline)
        with pytest.raises(ProcessError):
            owner.release(deadline)
        owner.dispose_observation()
        source.failure = None
        after_disposal = owner.observe(deadline)

        assert observed.condition is ProcessCondition.UNKNOWN
        assert refused is SignalResult.UNKNOWN
        assert after_disposal.condition is ProcessCondition.UNKNOWN
        assert not owner.closed
        assert owner.watcher_count == 0
        assert set(os.listdir("/dev/fd")) - baseline == set()


@pytest.mark.asyncio
async def test_recovered_leader_reaped_with_omitted_live_member_never_signals() -> None:
    clock = SystemClock()
    async with domain("family") as fixture:
        source = RecoveryIdentityDecisions(fixture.registration.process.pid)
        owner = RecoveredProcess(fixture.registration, clock, source)
        deadline = clock.monotonic_ns() + 5_000_000_000
        before = owner.observe(deadline)
        assert fixture.member_pid in {member.pid for member in before.live_members}
        fixture.leader.close()
        await fixture.reap()
        source.omit = True

        first = owner.observe(deadline)
        second = owner.observe(deadline)
        refused = owner.signal(signal.SIGKILL, deadline)
        with pytest.raises(ProcessError):
            owner.release(deadline)
        owner.dispose_observation()

        assert first.condition is ProcessCondition.UNKNOWN
        assert second.condition is ProcessCondition.UNKNOWN
        assert refused is SignalResult.UNKNOWN
        assert (
            NativeIdentityReader().read(fixture.member_pid).state
            is IdentityState.PRESENT
        )
        assert owner.watcher_count == 0


@pytest.mark.asyncio
async def test_recovered_watch_capacity_and_cancelled_waiter_release() -> None:
    clock = SystemClock()
    async with domain("family") as fixture:
        baseline = set(os.listdir("/dev/fd"))
        deadline = clock.monotonic_ns() + 5_000_000_000
        limited = RecoveredProcess(
            fixture.registration, clock, NativeIdentityReader(), max_members=1
        )
        owner = RecoveredProcess(fixture.registration, clock, NativeIdentityReader())
        entered = asyncio.Event()

        async def wait() -> None:
            entered.set()
            await owner.settle(deadline)

        refused = limited.observe(deadline)
        limited.dispose_observation()
        waiter = asyncio.create_task(wait())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        expired = owner.observe(clock.monotonic_ns() - 1)
        retained = owner.watcher_count
        owner.dispose_observation()
        owner.dispose_observation()

        assert refused.condition is ProcessCondition.UNKNOWN
        assert expired.condition is ProcessCondition.UNKNOWN
        assert retained == len({fixture.registration.process.pid, fixture.member_pid})
        assert owner.watcher_count == 0
        assert set(os.listdir("/dev/fd")) - baseline == set()
        with pytest.raises(ChildProcessError):
            os.waitpid(fixture.registration.process.pid, os.WNOHANG)


@pytest.mark.asyncio
async def test_already_reaped_empty_group_recovers_with_stale_errno() -> None:
    clock = SystemClock()
    async with domain("normal") as fixture:
        fixture.leader.close()
        await fixture.reap()
        reader = NativeIdentityReader()
        ctypes.set_errno(errno.EPERM)
        deadline = clock.monotonic_ns() + 5_000_000_000
        owner = RecoveredProcess(fixture.registration, clock, reader)

        empty = reader.group(fixture.registration.process_group_id, deadline)
        observed = owner.observe(deadline)
        owner.release(deadline)

        assert empty.state is IdentityState.PRESENT
        assert empty.members == ()
        assert observed.settled
        assert observed.group_empty
        assert observed.observed_exits == ()
        assert owner.watcher_count == 0
