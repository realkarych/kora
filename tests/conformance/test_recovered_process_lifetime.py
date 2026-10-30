import asyncio
import ctypes
import errno
import gc
import os
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import TracebackType

import pytest

from kora.conformance.bridge import finish_cleanup
from kora.protocols.lifetime import ProcessRegistration
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
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
from tests.conformance.lifetime_helpers import (
    CleanupDeadline,
    join_helper_operation,
    turn,
)
from tests.conformance.lifetime_support import ControlledLifetimeClock


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
async def domain(
    mode: str,
    *,
    failure_phase: str | None = None,
    entered: asyncio.Event | None = None,
    started: list[subprocess.Popen[bytes]] | None = None,
    registrations: list[ProcessRegistration] | None = None,
    witnesses: list[bytes] | None = None,
    cleanup_entered: asyncio.Event | None = None,
    cleanup_release: asyncio.Event | None = None,
) -> AsyncGenerator[ReapedDomain]:
    async with held_domain(
        mode,
        failure_phase=failure_phase,
        entered=entered,
        started=started,
        registrations=registrations,
        witnesses=witnesses,
        cleanup_entered=cleanup_entered,
        cleanup_release=cleanup_release,
    ) as fixture:
        yield fixture


async def finish_held_domain(
    process: subprocess.Popen[bytes],
    anchor: ExitAnchor,
    handles: ExitStack,
    cleanup: CleanupDeadline,
    registrations: list[ProcessRegistration],
    original: BaseException | None,
    require_witness: bool,
    witnesses: list[bytes] | None,
    cleanup_entered: asyncio.Event | None,
    cleanup_release: asyncio.Event | None,
) -> None:
    clock = cleanup.clock

    async def close() -> None:
        deadline = cleanup()
        try:
            if cleanup_entered is not None:
                cleanup_entered.set()
            if cleanup_release is not None:
                await asyncio.wait_for(
                    cleanup_release.wait(),
                    max(0, (deadline - clock.monotonic_ns()) / 1_000_000_000),
                )
        finally:
            try:
                handles.close()
                await anchor.wait(deadline, clock)
                assert process.wait() == 0
                assert process.stdout is not None
                witness = process.stdout.read(2)
                if require_witness:
                    assert witness == b"r"
                if witnesses is not None:
                    witnesses.append(witness)
                for registration in registrations:
                    observer = RecoveredProcess(
                        registration, clock, NativeIdentityReader()
                    )
                    try:
                        await observer.settle(deadline)
                        observer.release(deadline)
                    finally:
                        observer.dispose_observation()
            finally:
                anchor.close()
                process.poll()

    await finish_cleanup(close, original)


@asynccontextmanager
async def held_domain(
    mode: str,
    *,
    failure_phase: str | None = None,
    entered: asyncio.Event | None = None,
    started: list[subprocess.Popen[bytes]] | None = None,
    registrations: list[ProcessRegistration] | None = None,
    witnesses: list[bytes] | None = None,
    cleanup_entered: asyncio.Event | None = None,
    cleanup_release: asyncio.Event | None = None,
) -> AsyncGenerator[ReapedDomain]:
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    retained = [] if registrations is None else registrations

    async def identity(peer: socket.socket, phase: str) -> int:
        if failure_phase == phase and entered is not None:
            entered.set()
            waiting: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            await waiting
        return int(
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_recv(peer, 64),
                0 if failure_phase == phase else 5,
            )
        )

    with tempfile.TemporaryDirectory(prefix="kr-", dir="/tmp") as directory:
        async with AsyncExitStack() as owned:
            handles = ExitStack()
            owned.callback(handles.close)
            listener = handles.enter_context(
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            )
            address = str(Path(directory) / "s")
            listener.bind(address)
            listener.listen(3)
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
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            if process.stdin is not None:
                handles.callback(process.stdin.close)
            if process.stdout is not None:
                owned.callback(process.stdout.close)
            if started is not None:
                started.append(process)
            anchor = ExitAnchor(process.pid)

            async def finish(
                _kind: type[BaseException] | None,
                original: BaseException | None,
                _traceback: TracebackType | None,
            ) -> None:
                await finish_held_domain(
                    process,
                    anchor,
                    handles,
                    cleanup,
                    retained,
                    original,
                    failure_phase in ("leader_recv", "member_recv") or mode != "family",
                    witnesses,
                    cleanup_entered,
                    cleanup_release,
                )

            owned.push_async_exit(finish)
            loop = asyncio.get_running_loop()
            reaper, _ = await asyncio.wait_for(
                loop.sock_accept(listener), 0 if failure_phase == "accept" else 5
            )
            handles.callback(reaper.close)
            assert await asyncio.wait_for(loop.sock_recv(reaper, 64), 5) == b"reaper"
            await loop.sock_sendall(reaper, b"s")
            leader, _ = await asyncio.wait_for(loop.sock_accept(listener), 5)
            handles.callback(leader.close)
            pid = await identity(leader, "leader_recv")
            observed = NativeIdentityReader().read(pid)
            assert observed.process is not None
            registration = ProcessRegistration(
                process=observed.process.identity,
                process_group_id=observed.process.process_group_id,
                session_id=observed.process.session_id,
                acquisition_operation_id="b" * 32,
            )
            retained.append(registration)
            fixture = ReapedDomain(leader, reaper, registration)
            if mode in ("family", "held_family"):
                await loop.sock_sendall(leader, b"s")
                fixture.member, _ = await asyncio.wait_for(
                    loop.sock_accept(listener), 5
                )
                handles.callback(fixture.member.close)
                fixture.member_pid = await identity(fixture.member, "member_recv")
            yield fixture


class SettlementClock(ControlledLifetimeClock):
    def __init__(self) -> None:
        super().__init__()
        self.entered: asyncio.Event = asyncio.Event()
        self.sleeps: list[int] = []

    async def sleep_until(self, deadline_ns: int) -> None:
        self.sleeps.append(deadline_ns)
        self.entered.set()
        await super().sleep_until(deadline_ns)


async def settlement_waiting(
    waiter: asyncio.Task[None], clock: SettlementClock, count: int = 1
) -> None:
    async def entered() -> None:
        while len(clock.sleeps) < count:
            clock.entered.clear()
            await clock.entered.wait()

    barrier = asyncio.create_task(entered())
    try:
        await asyncio.wait(
            (waiter, barrier), timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter.done():
            waiter.result()
        assert barrier.done()
        assert not waiter.done()
    finally:
        await join_helper_operation(barrier)


async def release_reaper(fixture: ReapedDomain) -> None:
    await asyncio.get_running_loop().sock_sendall(fixture.reaper, b"r")
    await fixture.reap()


def assert_empty_domain(fixture: ReapedDomain) -> None:
    reader = NativeIdentityReader()
    assert reader.read(fixture.registration.process.pid).state is IdentityState.ABSENT
    group = reader.group(fixture.registration.process_group_id)
    assert group.state is IdentityState.PRESENT
    assert group.members == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_observed_alive", [True, False])
@pytest.mark.parametrize(
    "completion", ["reap", "deadline", "cancel", "dispose", "dispose_at_deadline"]
)
async def test_recovered_settle_waits_for_reap_of_exited_leader(
    first_observed_alive: bool, completion: str
) -> None:
    clock = SettlementClock()
    actual = SystemClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            owner = RecoveredProcess(
                fixture.registration, clock, NativeIdentityReader()
            )
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000
            if first_observed_alive:
                assert owner.observe(deadline).condition is ProcessCondition.LIVE
            exited = ExitAnchor(fixture.registration.process.pid, direct_child=False)
            owned.callback(exited.close)
            os.kill(fixture.registration.process.pid, signal.SIGKILL)
            await exited.wait(actual.monotonic_ns() + 5_000_000_000, actual)
            listed = NativeIdentityReader().group(fixture.registration.process_group_id)
            assert listed.state is IdentityState.PRESENT
            assert len(listed.members) == 1
            native = listed.members[0][1]
            if sys.platform == "linux":
                assert native.state is IdentityState.PRESENT
                assert native.process is not None
                assert native.process.dead
            else:
                assert native.state is IdentityState.ABSENT
            waiter = asyncio.create_task(owner.settle(deadline))
            observed_errors: list[BaseException] = []
            owned.push_async_callback(join_helper_operation, waiter, observed_errors)

            await settlement_waiting(waiter, clock)
            observation = owner.observe(deadline)
            assert observation.condition is ProcessCondition.UNKNOWN
            assert not observation.group_empty
            assert owner.watcher_count == int(first_observed_alive)
            assert len(clock.waiters) == 1
            if not first_observed_alive:
                assert owner.readiness() == ()
                assert clock.sleeps[-1] == clock.monotonic_ns() + 100_000_000
            for requested in (signal.SIGTERM, signal.SIGKILL):
                assert owner.signal(requested, deadline) is SignalResult.UNKNOWN
            with pytest.raises(ProcessError):
                owner.release(deadline)
            remaining = waiter
            if completion == "cancel":
                remaining = asyncio.create_task(owner.settle(deadline))
                owned.push_async_callback(
                    join_helper_operation, remaining, observed_errors
                )
                await settlement_waiting(remaining, clock, len(clock.sleeps) + 1)
                assert len(clock.waiters) == 2
                native_pending = owner.readiness()
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                assert len(clock.waiters) == 1
                assert all(not future.cancelled() for future in native_pending)
                assert not remaining.done()
            elif completion in ("deadline", "dispose_at_deadline"):
                clock.advance(5_000_000_000)
                if completion == "dispose_at_deadline":
                    owner.dispose_observation()
                with pytest.raises(ChannelError) as expired:
                    await waiter
                observed_errors.append(expired.value)
                assert expired.value.reason is ChannelFailure.DEADLINE
                owner.dispose_observation()
                assert not clock.waiters
            elif completion == "dispose":
                owner.dispose_observation()
                with pytest.raises(ProcessError) as disposed:
                    await asyncio.wait_for(waiter, 5)
                observed_errors.append(disposed.value)
                assert disposed.value.condition is ProcessCondition.UNKNOWN
                assert not clock.waiters
                with pytest.raises(ProcessError) as refused:
                    await owner.settle(deadline)
                assert refused.value.condition is ProcessCondition.UNKNOWN
                assert not owner.closed
            await release_reaper(fixture)
            if completion in ("reap", "cancel"):
                clock.advance(100_000_000)
                await asyncio.wait_for(remaining, 5)
                assert not clock.waiters
                assert owner.observe(deadline).settled
                owner.release(deadline)
                assert owner.closed

            assert_empty_domain(fixture)
            owner.dispose_observation()
            assert owner.watcher_count == 0
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_recovered_late_member_refresh_is_independent_of_live_leader() -> None:
    clock = SettlementClock()
    actual = SystemClock()
    async with domain("held_family") as fixture:
        assert fixture.member_pid is not None
        assert fixture.member is not None
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            owner = RecoveredProcess(
                fixture.registration, clock, NativeIdentityReader()
            )
            owned.callback(owner.dispose_observation)
            exited = ExitAnchor(fixture.member_pid, direct_child=False)
            owned.callback(exited.close)
            os.kill(fixture.member_pid, signal.SIGKILL)
            await exited.wait(actual.monotonic_ns() + 5_000_000_000, actual)
            deadline = clock.monotonic_ns() + 5_000_000_000
            waiter = asyncio.create_task(owner.settle(deadline))
            owned.push_async_callback(join_helper_operation, waiter)

            await settlement_waiting(waiter, clock)
            observation = owner.observe(deadline)
            expected = (
                ProcessCondition.LIVE
                if sys.platform == "linux"
                else ProcessCondition.UNKNOWN
            )
            assert observation.condition is expected
            assert owner.watcher_count == 1
            assert owner.readiness()
            assert list(clock.waiters.values()) == [clock.monotonic_ns() + 100_000_000]
            assert owner.signal(signal.SIGTERM, deadline) is (
                SignalResult.SENT
                if expected is ProcessCondition.LIVE
                else SignalResult.UNKNOWN
            )
            with pytest.raises(ProcessError):
                owner.release(deadline)
            count = len(clock.sleeps)
            clock.advance(400_000_000)
            await settlement_waiting(waiter, clock, count + 1)
            assert list(clock.waiters.values()) == [clock.monotonic_ns() + 100_000_000]
            loop = asyncio.get_running_loop()
            await loop.sock_sendall(fixture.leader, b"r")
            assert (
                await asyncio.wait_for(loop.sock_recv(fixture.leader, 64), 5)
                == b"reaped"
            )
            group = NativeIdentityReader().group(fixture.registration.process_group_id)
            assert fixture.member_pid not in {pid for pid, _ in group.members}
            count = len(clock.sleeps)
            clock.advance(100_000_000)
            await settlement_waiting(waiter, clock, count + 1)
            assert list(clock.waiters.values()) == [deadline]
            assert owner.observe(deadline).condition is ProcessCondition.LIVE
            assert not waiter.done()
            fixture.leader.shutdown(socket.SHUT_WR)
            await release_reaper(fixture)
            await asyncio.wait_for(waiter, 5)
            owner.release(deadline)

            assert owner.closed
            assert owner.watcher_count == 0
            assert not clock.waiters
            assert_empty_domain(fixture)
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


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


class CollectionRefusalDecisions(RecoveryIdentityDecisions):
    def __init__(self, fixture: ReapedDomain, retained: bool, failure: str) -> None:
        super().__init__(fixture.registration.process.pid)
        self.member_pid: int | None = fixture.member_pid
        self.retained: bool = retained
        self.group_failure: str = failure

    def read(self, pid: int) -> IdentityObservation:
        if pid == self.pid and not self.retained:
            return IdentityObservation(IdentityState.ABSENT)
        return super().read(pid)

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        group = super().group(process_group_id, deadline_ns)
        leader = self.native.read(self.pid)
        process = leader.process
        assert process is not None
        assert self.member_pid is not None
        if self.group_failure == "unreadable":
            refused = IdentityObservation(IdentityState.UNKNOWN)
        elif self.group_failure == "malformed":
            refused = IdentityObservation(IdentityState.PRESENT)
        else:
            candidate = (
                replace(process, session_id=process.session_id + 1)
                if self.group_failure == "scope"
                else replace(
                    process,
                    identity=process.identity.model_copy(
                        update={"start_time": process.identity.start_time + 1}
                    ),
                )
            )
            refused = replace(leader, process=candidate)
        return replace(
            group,
            members=(
                (self.member_pid, IdentityObservation(IdentityState.ABSENT)),
                (self.pid, refused),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [True, False])
@pytest.mark.parametrize("failure", ["unreadable", "malformed", "reused", "scope"])
async def test_recovered_terminal_refusal_dominates_waitable_collection(
    retained: bool, failure: str
) -> None:
    clock = SettlementClock()
    actual = SystemClock()
    async with domain("held_family") as fixture:
        assert fixture.member_pid is not None
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            source = CollectionRefusalDecisions(fixture, retained, failure)
            owner = RecoveredProcess(fixture.registration, clock, source)
            owned.callback(owner.dispose_observation)
            exited = ExitAnchor(fixture.member_pid, direct_child=False)
            owned.callback(exited.close)
            os.kill(fixture.member_pid, signal.SIGKILL)
            await exited.wait(actual.monotonic_ns() + 5_000_000_000, actual)
            deadline = clock.monotonic_ns() + 5_000_000_000

            with pytest.raises(ProcessError) as refused:
                await asyncio.wait_for(owner.settle(deadline), 5)
            public = owner.observe(deadline)
            signals = tuple(
                owner.signal(requested, deadline)
                for requested in (signal.SIGTERM, signal.SIGKILL)
            )
            owner.dispose_observation()

            assert refused.value.condition is ProcessCondition.UNKNOWN
            assert public.condition is ProcessCondition.UNKNOWN
            assert signals == (SignalResult.UNKNOWN, SignalResult.UNKNOWN)
            assert clock.sleeps == []
            assert not clock.waiters
            assert not owner.closed
            assert owner.watcher_count == 0
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


class ReapingIdentityDecisions(RecoveryIdentityDecisions):
    def __init__(
        self, fixture: ReapedDomain, *, readable_group: bool, unexplained: bool
    ) -> None:
        super().__init__(fixture.registration.process.pid)
        self.fixture: ReapedDomain = fixture
        self.readable_group: bool = readable_group
        self.unexplained: bool = unexplained
        self.before_reap: IdentityObservation | None = None

    def read(self, pid: int) -> IdentityObservation:
        observed = super().read(pid)
        if pid != self.pid:
            return observed
        if self.before_reap is None:
            assert observed.process is not None
            assert not observed.process.dead
            self.before_reap = observed
            self.fixture.leader.shutdown(socket.SHUT_WR)
            self.fixture.reaper.settimeout(5)
            try:
                self.fixture.reaper.sendall(b"r")
                assert self.fixture.reaper.recv(64) == b"0"
                self.fixture.reaped = True
            finally:
                self.fixture.reaper.setblocking(False)
            return observed
        return self.before_reap if self.unexplained else observed

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        return (
            super().group(process_group_id, deadline_ns)
            if self.readable_group
            else GroupObservation(IdentityState.UNKNOWN)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["settled", "unreadable", "anchor_refusal"])
async def test_recovered_settle_confirms_collection_during_watch_establishment(
    outcome: str,
) -> None:
    clock = SettlementClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            source = ReapingIdentityDecisions(
                fixture,
                readable_group=outcome != "unreadable",
                unexplained=outcome == "anchor_refusal",
            )
            owner = RecoveredProcess(fixture.registration, clock, source)
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000

            if outcome == "settled":
                await asyncio.wait_for(owner.settle(deadline), 5)
                owner.release(deadline)
            else:
                with pytest.raises(ProcessError) as refused:
                    await asyncio.wait_for(owner.settle(deadline), 5)
                assert refused.value.condition is ProcessCondition.UNKNOWN
                with pytest.raises(ProcessError):
                    owner.release(deadline)

            assert fixture.reaped
            assert clock.sleeps == []
            assert not clock.waiters
            assert owner.closed is (outcome == "settled")
            assert owner.watcher_count == 0
            assert_empty_domain(fixture)
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


class CollectingMemberDecisions(RecoveryIdentityDecisions):
    def __init__(self, fixture: ReapedDomain) -> None:
        assert fixture.member_pid is not None
        super().__init__(fixture.member_pid)
        self.fixture: ReapedDomain = fixture
        self.collected: bool = False

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        observed = super().group(process_group_id, deadline_ns)
        if not self.collected:
            assert self.fixture.member is not None
            assert self.pid in {pid for pid, _ in observed.members}
            self.fixture.member.shutdown(socket.SHUT_WR)
            self.fixture.leader.settimeout(5)
            try:
                self.fixture.leader.sendall(b"r")
                assert self.fixture.leader.recv(64) == b"reaped"
                self.collected = True
            finally:
                self.fixture.leader.setblocking(False)
        return observed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["observe", "term", "kill"])
async def test_recovered_collection_race_preserves_unknown_with_live_leader(
    operation: str,
) -> None:
    clock = SettlementClock()
    async with domain("held_family") as fixture:
        assert fixture.member_pid is not None
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            source = CollectingMemberDecisions(fixture)
            owner = RecoveredProcess(fixture.registration, clock, source)
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000

            if operation == "observe":
                observed = owner.observe(deadline)
                assert observed.condition is ProcessCondition.UNKNOWN
            else:
                requested = signal.SIGTERM if operation == "term" else signal.SIGKILL
                assert owner.signal(requested, deadline) is SignalResult.UNKNOWN

            leader = source.native.read(fixture.registration.process.pid).process
            assert leader is not None
            assert leader.identity == fixture.registration.process
            assert not leader.dead
            assert source.native.read(fixture.member_pid).state is IdentityState.ABSENT
            assert source.collected
            assert clock.sleeps == []
            assert owner.watcher_count == 1
            owner.dispose_observation()
            assert owner.watcher_count == 0
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline
    assert_empty_domain(fixture)


class GroupCollectionDecisions(RecoveryIdentityDecisions):
    def __init__(self, fixture: ReapedDomain, fresh: str) -> None:
        super().__init__(fixture.registration.process.pid)
        self.fixture: ReapedDomain = fixture
        self.fresh: str = fresh
        self.before_scan: IdentityObservation | None = None
        self.collected: bool = False
        self.classified: bool = False

    def read(self, pid: int) -> IdentityObservation:
        observed = super().read(pid)
        if pid != self.pid:
            return observed
        if not self.collected:
            if self.before_scan is None:
                self.before_scan = observed
            return observed
        if self.classified:
            return observed
        self.classified = True
        assert observed.state is IdentityState.ABSENT
        assert self.before_scan is not None
        snapshot = self.before_scan.process
        assert snapshot is not None
        if self.fresh == "absent":
            return observed
        if self.fresh == "unknown":
            return IdentityObservation(IdentityState.UNKNOWN)
        if self.fresh == "malformed":
            return IdentityObservation(IdentityState.PRESENT)
        current = replace(snapshot, dead=self.fresh != "live")
        if self.fresh == "identity":
            current = replace(
                current,
                identity=current.identity.model_copy(
                    update={"start_time": current.identity.start_time + 1}
                ),
            )
        elif self.fresh == "domain":
            current = replace(current, session_id=current.session_id + 1)
        return IdentityObservation(IdentityState.PRESENT, current)

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        if not self.collected:
            assert self.before_scan is not None
            os.kill(self.pid, signal.SIGKILL)
            self.fixture.reaper.settimeout(5)
            try:
                self.fixture.reaper.sendall(b"r")
                assert int(self.fixture.reaper.recv(64)) == -signal.SIGKILL
                self.fixture.reaped = True
                self.collected = True
            finally:
                self.fixture.reaper.setblocking(False)
        return super().group(process_group_id, deadline_ns)


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", ["absent", "dead"])
async def test_recovered_settle_confirms_collection_after_live_leader_leaves_scan(
    fresh: str,
) -> None:
    clock = SettlementClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            source = GroupCollectionDecisions(fixture, fresh)
            owner = RecoveredProcess(fixture.registration, clock, source)
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000

            await asyncio.wait_for(owner.settle(deadline), 5)
            owner.release(deadline)

            assert source.collected
            assert source.classified
            assert owner.closed
            assert owner.watcher_count == 0
            assert not clock.waiters
            assert_empty_domain(fixture)
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", ["absent", "dead"])
@pytest.mark.parametrize("operation", ["observe", "term", "kill"])
async def test_recovered_missing_leader_recheck_preserves_signal_refusal(
    fresh: str, operation: str
) -> None:
    clock = SettlementClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            source = GroupCollectionDecisions(fixture, fresh)
            owner = RecoveredProcess(fixture.registration, clock, source)
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000

            if operation == "observe":
                observed = owner.observe(deadline)
                assert observed.condition is ProcessCondition.UNKNOWN
            else:
                requested = signal.SIGTERM if operation == "term" else signal.SIGKILL
                assert owner.signal(requested, deadline) is SignalResult.UNKNOWN
            pending = owner.readiness()
            assert pending
            assert all(not future.done() for future in pending)
            assert source.classified
            await asyncio.wait_for(owner.settle(deadline), 5)
            owner.release(deadline)

            assert owner.closed
            assert owner.watcher_count == 0
            assert not clock.waiters
            assert_empty_domain(fixture)
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fresh", ["unknown", "malformed", "identity", "domain", "live"]
)
async def test_recovered_missing_leader_recheck_refuses_terminal_observation(
    fresh: str,
) -> None:
    clock = SettlementClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            source = GroupCollectionDecisions(fixture, fresh)
            owner = RecoveredProcess(fixture.registration, clock, source)
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000

            with pytest.raises(ProcessError) as refused:
                await asyncio.wait_for(owner.settle(deadline), 5)
            owner.dispose_observation()

            assert refused.value.condition is ProcessCondition.UNKNOWN
            assert source.classified
            assert not owner.closed
            assert owner.watcher_count == 0
            assert clock.sleeps == []
            assert not clock.waiters
            assert_empty_domain(fixture)
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


class DeadlineIdentityDecisions(RecoveryIdentityDecisions):
    def __init__(self, pid: int, clock: SettlementClock, failure: bool) -> None:
        super().__init__(pid)
        self.clock: SettlementClock = clock
        self.failure = "unreadable" if failure else None

    def read(self, pid: int) -> IdentityObservation:
        observed = super().read(pid)
        if pid == self.pid:
            self.clock.advance(5_000_000_000)
        return observed


@pytest.mark.asyncio
@pytest.mark.parametrize("moment", ["initial", "observation", "refusal"])
async def test_recovered_settle_deadline_precedes_observation_and_refusal(
    moment: str,
) -> None:
    clock = SettlementClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        source = DeadlineIdentityDecisions(
            fixture.registration.process.pid, clock, moment == "refusal"
        )
        owner = RecoveredProcess(fixture.registration, clock, source)
        deadline = clock.monotonic_ns() + (0 if moment == "initial" else 5_000_000_000)

        try:
            with pytest.raises(ChannelError) as expired:
                await owner.settle(deadline)
        finally:
            owner.dispose_observation()

        assert expired.value.reason is ChannelFailure.DEADLINE
        assert source.reads == (0 if moment == "initial" else 1)
        assert clock.sleeps == []
        assert not clock.waiters
        assert owner.watcher_count == 0
        assert not owner.closed
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "reused", "race", "scope"])
async def test_recovery_identity_refusal_disposes_partial_watchers(
    failure: str,
) -> None:
    clock = SettlementClock()
    async with domain("normal") as fixture:
        source = RecoveryIdentityDecisions(fixture.registration.process.pid)
        source.failure = failure
        baseline = set(os.listdir("/dev/fd"))
        owner = RecoveredProcess(fixture.registration, clock, source)
        deadline = clock.monotonic_ns() + 5_000_000_000

        with pytest.raises(ProcessError) as settled:
            await asyncio.wait_for(owner.settle(deadline), 5)
        observed = owner.observe(deadline)
        refused = owner.signal(signal.SIGKILL, deadline)
        with pytest.raises(ProcessError):
            owner.release(deadline)
        owner.dispose_observation()
        source.failure = None
        after_disposal = owner.observe(deadline)

        assert observed.condition is ProcessCondition.UNKNOWN
        assert settled.value.condition is ProcessCondition.UNKNOWN
        assert clock.sleeps == []
        assert refused is SignalResult.UNKNOWN
        assert after_disposal.condition is ProcessCondition.UNKNOWN
        assert not owner.closed
        assert owner.watcher_count == 0
        assert set(os.listdir("/dev/fd")) - baseline == set()


@pytest.mark.asyncio
async def test_recovered_live_wait_disposal_wakes_without_collection_refresh() -> None:
    clock = SettlementClock()
    async with domain("held") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        async with AsyncExitStack() as owned:
            owner = RecoveredProcess(
                fixture.registration, clock, NativeIdentityReader()
            )
            owned.callback(owner.dispose_observation)
            deadline = clock.monotonic_ns() + 5_000_000_000
            waiter = asyncio.create_task(owner.settle(deadline))
            observed_errors: list[BaseException] = []
            owned.push_async_callback(join_helper_operation, waiter, observed_errors)
            await settlement_waiting(waiter, clock)
            pending = owner.readiness()
            assert pending
            assert list(clock.waiters.values()) == [deadline]

            owner.dispose_observation()
            with pytest.raises(ProcessError) as disposed:
                await asyncio.wait_for(waiter, 5)
            observed_errors.append(disposed.value)

            assert disposed.value.condition is ProcessCondition.UNKNOWN
            assert all(future.cancelled() for future in pending)
            assert not clock.waiters
            assert owner.watcher_count == 0
            assert not owner.closed
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_recovered_capacity_refusal_does_not_start_a_wait_timer() -> None:
    clock = SettlementClock()
    async with domain("held_family") as fixture:
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        owner = RecoveredProcess(
            fixture.registration, clock, NativeIdentityReader(), max_members=1
        )
        deadline = clock.monotonic_ns() + 5_000_000_000

        try:
            with pytest.raises(ProcessError) as refused:
                await asyncio.wait_for(owner.settle(deadline), 5)
        finally:
            owner.dispose_observation()

        assert refused.value.condition is ProcessCondition.UNKNOWN
        assert clock.sleeps == []
        assert not clock.waiters
        assert owner.watcher_count == 0
        assert not owner.closed
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_recovered_leader_reaped_with_omitted_live_member_never_signals() -> None:
    clock = SystemClock()
    async with domain("family") as fixture:
        source = RecoveryIdentityDecisions(fixture.registration.process.pid)
        owner = RecoveredProcess(fixture.registration, clock, source)
        deadline = clock.monotonic_ns() + 5_000_000_000
        before = owner.observe(deadline)
        assert fixture.member_pid in {member.pid for member in before.live_members}
        os.kill(fixture.registration.process.pid, signal.SIGKILL)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["accept", "leader_recv", "member_recv"])
async def test_recovered_domain_handshake_failure_releases_acquired_resources(
    phase: str,
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    started: list[subprocess.Popen[bytes]] = []
    registrations: list[ProcessRegistration] = []
    witnesses: list[bytes] = []

    with pytest.raises(TimeoutError):
        async with domain(
            "family",
            failure_phase=phase,
            started=started,
            registrations=registrations,
            witnesses=witnesses,
        ):
            raise AssertionError("unexpected successful handshake")
    gc.collect()

    assert len(started) == 1
    assert started[0].returncode == 0
    assert witnesses == [b"" if phase == "accept" else b"r"]
    reader = NativeIdentityReader()
    assert reader.read(started[0].pid).state is IdentityState.ABSENT
    for registration in registrations:
        assert reader.read(registration.process.pid).state is IdentityState.ABSENT
        group = reader.group(registration.process_group_id)
        assert group.state is IdentityState.PRESENT
        assert group.members == ()
    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_recovered_domain_handshake_cancellation_releases_resources() -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    started: list[subprocess.Popen[bytes]] = []
    registrations: list[ProcessRegistration] = []
    witnesses: list[bytes] = []
    entered = asyncio.Event()

    async def acquire() -> None:
        async with domain(
            "family",
            failure_phase="member_recv",
            entered=entered,
            started=started,
            registrations=registrations,
            witnesses=witnesses,
        ):
            raise AssertionError("unexpected successful handshake")

    task = asyncio.create_task(acquire())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await join_helper_operation(task)
    gc.collect()

    assert len(started) == 1
    assert started[0].returncode == 0
    assert witnesses == [b"r"]
    reader = NativeIdentityReader()
    assert reader.read(started[0].pid).state is IdentityState.ABSENT
    assert len(registrations) == 1
    group = reader.group(registrations[0].process_group_id)
    assert group.state is IdentityState.PRESENT
    assert group.members == ()
    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_recovered_domain_repeated_cancellation_joins_owned_cleanup() -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    started: list[subprocess.Popen[bytes]] = []
    registrations: list[ProcessRegistration] = []
    witnesses: list[bytes] = []

    async def acquire() -> None:
        async with domain(
            "family",
            failure_phase="member_recv",
            entered=entered,
            started=started,
            registrations=registrations,
            witnesses=witnesses,
            cleanup_entered=cleanup_entered,
            cleanup_release=cleanup_release,
        ):
            raise AssertionError("unexpected successful handshake")

    task = asyncio.create_task(acquire())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("original handshake")
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert not task.done()
        assert started[0].poll() is None
        task.cancel("later teardown")
        await turn()
        assert not task.done()
        task.cancel("last teardown")
        await turn()
        assert not task.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value.args == ("original handshake",)
    finally:
        cleanup_release.set()
        await join_helper_operation(task)
    gc.collect()

    assert started[0].returncode == 0
    assert witnesses == [b"r"]
    reader = NativeIdentityReader()
    assert reader.read(started[0].pid).state is IdentityState.ABSENT
    assert len(registrations) == 1
    group = reader.group(registrations[0].process_group_id)
    assert group.state is IdentityState.PRESENT
    assert group.members == ()
    assert set(os.listdir("/dev/fd")) == baseline
