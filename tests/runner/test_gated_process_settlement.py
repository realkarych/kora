import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from kora.protocols.lifetime import (
    CleanupOutcome,
    OriginalOutcome,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure, wait_future
from kora.runner.child_wait import (
    BsdInfo,
    DarwinDirectChildProbe,
    KernelChildWatch,
    Readiness,
)
from kora.runner.identity import (
    DARWIN_ZOMBIE,
    PROC_PIDTBSDINFO,
    GroupObservation,
    IdentityObservation,
    IdentityState,
    NativeIdentityReader,
    ProcessSnapshot,
)
from kora.runner.processes import ProcessCondition, ProcessError, start_gated
from tests.support.event_loop import turn

CHILD = "pass"


def launch(script: str) -> ProcessLaunch:
    return ProcessLaunch(
        executable=sys.executable,
        argv=(sys.executable, "-I", "-c", script),
        environment=(),
        working_directory_resource_id="1" * 32,
        stdin=None,
        stdout=ProcessOutput.CAPTURE,
        stderr=ProcessOutput.CAPTURE,
        domain=ProcessDomain.INVOCATION,
    )


class OffsetClock(SystemClock):
    def __init__(self) -> None:
        self.offset = 0
        self._waiters: dict[asyncio.Future[None], int] = {}

    def monotonic_ns(self) -> int:
        return super().monotonic_ns() + self.offset

    async def sleep_until(self, deadline_ns: int) -> None:
        if self.monotonic_ns() >= deadline_ns:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[waiter] = deadline_ns
        real = asyncio.create_task(super().sleep_until(deadline_ns - self.offset))
        try:
            await asyncio.wait((waiter, real), return_when=asyncio.FIRST_COMPLETED)
            if real.done() and not real.cancelled():
                real.result()
        finally:
            self._waiters.pop(waiter, None)
            if not waiter.done():
                waiter.cancel()
            real.cancel()
            await asyncio.gather(real, return_exceptions=True)

    def advance(self, nanoseconds: int) -> None:
        self.offset += nanoseconds
        now = self.monotonic_ns()
        for waiter, deadline in tuple(self._waiters.items()):
            if now >= deadline and not waiter.done():
                waiter.set_result(None)


class LeaderIdentity:
    def __init__(self) -> None:
        self._native = NativeIdentityReader()
        self.mode = "native"
        self._pid: int | None = None
        self._snapshot: ProcessSnapshot | None = None

    def pin(self, registration: ProcessRegistration) -> None:
        self._pid = registration.process.pid
        self._snapshot = ProcessSnapshot(
            registration.process,
            registration.process_group_id,
            registration.session_id,
            False,
        )

    def read(self, pid: int) -> IdentityObservation:
        native = self._native.read(pid)
        snapshot = self._snapshot
        if pid != self._pid or self.mode == "native" or snapshot is None:
            return native
        if self.mode == "live":
            return IdentityObservation(
                IdentityState.PRESENT, replace(snapshot, dead=False)
            )
        if self.mode == "absent":
            return IdentityObservation(IdentityState.ABSENT)
        if self.mode == "unknown":
            return IdentityObservation(IdentityState.UNKNOWN)
        if self.mode == "capacity":
            return IdentityObservation(IdentityState.ABSENT)
        changed = snapshot.identity.model_copy(
            update={"start_time": snapshot.identity.start_time + 1}
        )
        return IdentityObservation(
            IdentityState.PRESENT, replace(snapshot, identity=changed)
        )

    def group(
        self, process_group_id: int, deadline_ns: int | None = None
    ) -> GroupObservation:
        if self.mode == "native" or self._pid is None or self._snapshot is None:
            return self._native.group(process_group_id, deadline_ns)
        if self.mode == "capacity":
            extra_identity = self._snapshot.identity.model_copy(
                update={"pid": 2**31 - 1}
            )
            extra = replace(self._snapshot, identity=extra_identity, dead=True)
            return GroupObservation(
                IdentityState.PRESENT,
                (
                    (self._pid, self.read(self._pid)),
                    (
                        extra_identity.pid,
                        IdentityObservation(IdentityState.PRESENT, extra),
                    ),
                ),
            )
        return GroupObservation(
            IdentityState.PRESENT, ((self._pid, self.read(self._pid)),)
        )


class ScriptedProbe:
    def __init__(self, clock: OffsetClock | None = None) -> None:
        self.result = Readiness.NOT_READY
        self.calls = 0
        self.fail = False
        self.clock = clock
        self.jump = 0
        self.mark_anchor_unknown = False
        self.owner: object | None = None

    def read(self, registration: ProcessRegistration) -> Readiness:
        del registration
        self.calls += 1
        if self.clock is not None and self.jump:
            self.clock.advance(self.jump)
        if self.mark_anchor_unknown:
            anchor = getattr(self.owner, "_anchor", None)
            if anchor is not None:
                anchor.unknown = True
        if self.fail:
            raise OSError(1, "probe")
        return self.result


class ScriptedWatch:
    def __init__(self) -> None:
        self._changed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.closed = False

    @property
    def changed(self) -> asyncio.Future[None]:
        return self._changed

    def notify(self) -> None:
        if not self.closed and not self._changed.done():
            self._changed.set_result(None)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._changed.cancel()


class ScriptedWatches:
    def __init__(self) -> None:
        self.created: list[ScriptedWatch] = []
        self.failures = 0
        self.immediate = 0

    def __call__(self, pid: int) -> ScriptedWatch:
        del pid
        if self.failures:
            self.failures -= 1
            raise OSError(1, "watch")
        watch = ScriptedWatch()
        self.created.append(watch)
        if self.immediate:
            self.immediate -= 1
            watch.notify()
        return watch

    def notify(self) -> None:
        self.created[-1].notify()

    def open_count(self) -> int:
        return sum(not watch.closed for watch in self.created)


async def until(
    condition: Callable[[], bool], task: asyncio.Task[object] | None = None
) -> None:
    started = time.monotonic_ns()
    while not condition():
        if task is not None and task.done() and not task.cancelled():
            task.result()
        if time.monotonic_ns() - started > 5_000_000_000:
            raise AssertionError("condition was not reached")
        await turn()


async def retire(owner: object, deadline: int) -> None:
    from kora.runner.processes import GatedProcess

    assert isinstance(owner, GatedProcess)
    if owner.closed:
        return
    if owner.returncode is None:
        owner.signal(signal.SIGKILL)
        try:
            await owner.wait_exit(deadline)
        except ProcessError:
            pass
        try:
            await owner.drain(deadline)
        except ChannelError:
            pass
        try:
            owner.release(None)
        except ProcessError:
            owner.release(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "absent"])
async def test_gated_settle_waits_for_owned_child_reapability(
    tmp_path: Path, mode: str
) -> None:
    clock = OffsetClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    reader = LeaderIdentity()
    probe = ScriptedProbe(clock)
    watches = ScriptedWatches()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch(CHILD),
        "2" * 32,
        directory,
        deadline,
        clock,
        reader,
        child_probe=probe,
        child_watch=watches,
        native_reap_wait=False,
    )
    os.close(directory)

    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        await owner.drain(deadline)
        reader.pin(registration)
        reader.mode = mode
        probe.result = Readiness.NOT_READY

        settling = asyncio.create_task(owner.settle(deadline))
        await until(
            lambda: probe.calls >= 1 and watches.open_count() == 1,
            settling,
        )

        assert not settling.done()
        assert owner.returncode is None
        held = probe.calls
        await turn()
        assert probe.calls == held
        watches.notify()
        await until(lambda: probe.calls > held and watches.open_count() == 1, settling)
        assert not settling.done()
        assert owner.returncode is None

        reader.mode = "absent"
        probe.result = Readiness.READY
        watches.notify()
        await settling
        assert owner.returncode is None
        status = owner.release(deadline)
    finally:
        await retire(owner, deadline)

    assert status == 0
    assert owner.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(registration.process.pid, os.WNOHANG)


@pytest.mark.asyncio
async def test_gated_settle_rechecks_spurious_and_registration_races(
    tmp_path: Path,
) -> None:
    clock = OffsetClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    reader = LeaderIdentity()
    probe = ScriptedProbe(clock)
    watches = ScriptedWatches()
    watches.immediate = 1
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch(CHILD),
        "3" * 32,
        directory,
        deadline,
        clock,
        reader,
        child_probe=probe,
        child_watch=watches,
        native_reap_wait=False,
    )
    os.close(directory)

    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        reader.pin(registration)
        reader.mode = "absent"
        probe.result = Readiness.NOT_READY

        settling = asyncio.create_task(owner.settle(deadline))
        await until(
            lambda: probe.calls >= 2 and watches.open_count() == 1,
            settling,
        )
        quiet = probe.calls
        await turn()
        watches.notify()
        await until(lambda: probe.calls == quiet + 1, settling)

        assert not settling.done()
        assert owner.returncode is None
        assert watches.created[0].closed
        probe.result = Readiness.READY
        watches.notify()
        await settling
        status = owner.release(deadline)
    finally:
        await retire(owner, deadline)

    assert status == 0
    assert owner.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "probe_unknown",
        "probe_mismatch",
        "probe_error",
        "identity_unknown",
        "identity_mismatch",
        "watch_error",
        "anchor_unknown",
        "capacity",
        "deadline",
    ],
)
async def test_gated_settle_preserves_refusal_and_absolute_deadline(
    tmp_path: Path, failure: str
) -> None:
    clock = OffsetClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    reader = LeaderIdentity()
    probe = ScriptedProbe(clock)
    watches = ScriptedWatches()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch(CHILD),
        "4" * 32,
        directory,
        deadline,
        clock,
        reader,
        child_probe=probe,
        child_watch=watches,
        native_reap_wait=False,
        max_members=1,
    )
    os.close(directory)
    probe.owner = owner

    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        reader.pin(registration)
        reader.mode = "absent"
        probe.result = Readiness.READY
        if failure == "probe_unknown":
            probe.result = Readiness.UNKNOWN
        elif failure == "probe_mismatch":
            probe.result = Readiness.MISMATCH
        elif failure == "probe_error":
            probe.fail = True
        elif failure == "identity_unknown":
            reader.mode = "unknown"
        elif failure == "identity_mismatch":
            reader.mode = "mismatch"
        elif failure == "watch_error":
            watches.failures = 1
            probe.result = Readiness.NOT_READY
        elif failure == "anchor_unknown":
            probe.mark_anchor_unknown = True
            probe.result = Readiness.NOT_READY
        elif failure == "capacity":
            reader.mode = "capacity"
            probe.result = Readiness.NOT_READY
        else:
            probe.jump = 10_000_000_000
            probe.result = Readiness.READY

        with pytest.raises((ProcessError, ChannelError)) as refused:
            await owner.settle(deadline)
        retained = owner.returncode
    finally:
        reader.mode = "native"
        probe.fail = False
        probe.mark_anchor_unknown = False
        probe.result = Readiness.READY
        anchor = getattr(owner, "_anchor", None)
        if anchor is not None:
            anchor.unknown = False
        await retire(owner, deadline)

    assert retained is None
    if failure == "deadline":
        assert isinstance(refused.value, ChannelError)
        assert refused.value.reason is ChannelFailure.DEADLINE
    elif failure in ("probe_mismatch", "identity_mismatch"):
        assert isinstance(refused.value, ProcessError)
        assert refused.value.condition is ProcessCondition.MISMATCH
    else:
        assert isinstance(refused.value, ProcessError)
        assert refused.value.condition is ProcessCondition.UNKNOWN


@pytest.mark.asyncio
async def test_gated_settle_shares_watch_and_survives_interruption(
    tmp_path: Path,
) -> None:
    clock = OffsetClock()
    started = clock.monotonic_ns()
    long = started + 30_000_000_000
    reader = LeaderIdentity()
    probe = ScriptedProbe(clock)
    watches = ScriptedWatches()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch(CHILD),
        "5" * 32,
        directory,
        long,
        clock,
        reader,
        child_probe=probe,
        child_watch=watches,
        native_reap_wait=False,
    )
    os.close(directory)

    first: asyncio.Task[None] | None = None
    second: asyncio.Task[None] | None = None
    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(long)
        await owner.drain(long)
        reader.pin(registration)
        reader.mode = "absent"
        probe.result = Readiness.NOT_READY
        before = len(asyncio.all_tasks())

        first = asyncio.create_task(owner.settle(long))
        second = asyncio.create_task(owner.settle(long))
        await until(lambda: watches.open_count() == 1 and probe.calls >= 1, first)
        waiting = len(asyncio.all_tasks()) - before
        assert watches.open_count() == 1
        assert waiting >= 2

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert watches.open_count() == 1
        assert not first.done()

        restarted = asyncio.create_task(
            owner.settle(clock.monotonic_ns() + 1_000_000_000)
        )
        await until(lambda: probe.calls >= 2, restarted)
        clock.advance(2_000_000_000)
        with pytest.raises(ChannelError) as expired:
            await restarted
        assert expired.value.reason is ChannelFailure.DEADLINE
        assert not first.done()
        assert watches.open_count() == 1

        created = len(watches.created)
        status = owner.release(long)
        await first
        assert status == 0
        assert owner.closed
        assert watches.open_count() == 0
        await owner.settle(long)
        assert len(watches.created) == created
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await retire(owner, long)


@pytest.mark.asyncio
async def test_gated_settle_keeps_domain_and_defers_collection(tmp_path: Path) -> None:
    clock = OffsetClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    reader = LeaderIdentity()
    probe = ScriptedProbe(clock)
    watches = ScriptedWatches()
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    owner = start_gated(
        launch(CHILD),
        "6" * 32,
        directory,
        deadline,
        clock,
        reader,
        child_probe=probe,
        child_watch=watches,
        native_reap_wait=False,
    )
    os.close(directory)

    try:
        registration = await owner.prepare()
        await owner.acknowledge()
        await owner.wait_exit(deadline)
        reader.pin(registration)
        reader.mode = "live"
        probe.result = Readiness.READY
        calls = probe.calls

        settling = asyncio.create_task(owner.settle(deadline))
        await until(lambda: probe.calls >= 1 and watches.open_count() == 0, settling)
        assert not settling.done()
        assert owner.returncode is None
        assert probe.calls == calls + 1

        settling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await settling
        reader.mode = "absent"
        await owner.settle(deadline)
        assert probe.calls == calls + 1
        assert owner.returncode is None
        await owner.drain(deadline)
        status = owner.release(deadline)
    finally:
        await retire(owner, deadline)

    assert status == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(registration.process.pid, os.WNOHANG)


@pytest.mark.asyncio
async def test_kernel_child_watch_reports_exit_without_reaping() -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    baseline = set(os.listdir("/dev/fd"))
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import signal; signal.pause()"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        process_group=0,
    )
    watch: KernelChildWatch | None = None
    try:
        watch = KernelChildWatch(child.pid)
        child.kill()
        await wait_future(watch.changed, deadline, clock)
        collected = os.waitpid(child.pid, os.WNOHANG)
        assert collected[0] == child.pid
        child.returncode = os.waitstatus_to_exitcode(collected[1])
    finally:
        if watch is not None:
            watch.close()
        if child.returncode is None:
            child.kill()
            child.wait(timeout=5)

    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_darwin_probe_classifies_injected_bsd_info() -> None:
    observed = NativeIdentityReader().read(os.getpid())
    assert observed.process is not None
    registration = ProcessRegistration(
        process=observed.process.identity,
        acquisition_operation_id="b" * 32,
        process_group_id=os.getpgid(0),
        session_id=os.getsid(0),
    )
    identity = registration.process
    parent = os.getpid()

    def read_info(
        pid: int, flavor: int, argument: int, buffer: object, size: int
    ) -> int:
        del size
        assert pid == identity.pid
        assert flavor == PROC_PIDTBSDINFO
        assert argument == 1
        info = ctypes.cast(
            cast(ctypes.c_void_p, buffer), ctypes.POINTER(BsdInfo)
        ).contents
        info.pid = identity.pid
        info.parent_pid = parent
        info.process_group_id = registration.process_group_id
        info.start_seconds = identity.start_time // 1_000_000
        info.start_microseconds = identity.start_time % 1_000_000
        info.status = DARWIN_ZOMBIE
        return ctypes.sizeof(BsdInfo)

    ready = DarwinDirectChildProbe(parent_pid=parent, read_info=read_info)
    assert ready.read(registration) is Readiness.READY

    def missing(pid: int, flavor: int, argument: int, buffer: object, size: int) -> int:
        del pid, flavor, argument, buffer, size
        return 0

    unknown = DarwinDirectChildProbe(parent_pid=parent, read_info=missing)
    assert unknown.read(registration) is Readiness.UNKNOWN

    def alive(pid: int, flavor: int, argument: int, buffer: object, size: int) -> int:
        read_info(pid, flavor, argument, buffer, size)
        ctypes.cast(
            cast(ctypes.c_void_p, buffer), ctypes.POINTER(BsdInfo)
        ).contents.status = 2
        return ctypes.sizeof(BsdInfo)

    waiting = DarwinDirectChildProbe(parent_pid=parent, read_info=alive)
    assert waiting.read(registration) is Readiness.NOT_READY

    def foreign_parent(
        pid: int, flavor: int, argument: int, buffer: object, size: int
    ) -> int:
        read_info(pid, flavor, argument, buffer, size)
        ctypes.cast(
            cast(ctypes.c_void_p, buffer), ctypes.POINTER(BsdInfo)
        ).contents.parent_pid = parent + 1
        return ctypes.sizeof(BsdInfo)

    mismatch = DarwinDirectChildProbe(parent_pid=parent, read_info=foreign_parent)
    assert mismatch.read(registration) is Readiness.MISMATCH


@pytest.mark.asyncio
async def test_darwin_probe_reports_reapability_without_consuming_status() -> None:
    observed = NativeIdentityReader().read(os.getpid())
    assert observed.process is not None
    current = ProcessRegistration(
        process=observed.process.identity,
        acquisition_operation_id="c" * 32,
        process_group_id=os.getpgid(0),
        session_id=os.getsid(0),
    )
    if sys.platform != "darwin":
        assert DarwinDirectChildProbe().read(current) is Readiness.UNKNOWN
        return
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import signal; signal.pause()"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        process_group=0,
    )
    watch: KernelChildWatch | None = None
    try:
        reader = NativeIdentityReader()
        observed = reader.read(child.pid)
        assert observed.process is not None
        registration = ProcessRegistration(
            process=observed.process.identity,
            acquisition_operation_id="c" * 32,
            process_group_id=observed.process.process_group_id,
            session_id=observed.process.session_id,
        )
        probe = DarwinDirectChildProbe()
        assert probe.read(registration) is Readiness.NOT_READY
        shifted = registration.model_copy(
            update={
                "process": registration.process.model_copy(
                    update={"start_time": registration.process.start_time + 1}
                )
            }
        )
        assert probe.read(shifted) is Readiness.MISMATCH
        watch = KernelChildWatch(child.pid)
        child.kill()
        await wait_future(watch.changed, deadline, clock)
        assert probe.read(registration) is Readiness.READY
        assert os.waitpid(child.pid, os.WNOHANG)[0] == child.pid
        child.returncode = 0
    finally:
        if watch is not None:
            watch.close()
        if child.returncode is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_command_preserves_complete_during_child_reapability_transition(
    tmp_path: Path,
) -> None:
    from kora.cli import CLI_COMPONENT
    from kora.conformance.capture import captured
    from kora.conformance.command import ConformanceCommand
    from kora.conformance.common import utc_now
    from kora.observability import LoggingSettings

    clock = SystemClock()
    reader = LeaderIdentity()
    probe = ScriptedProbe()
    watches = ScriptedWatches()
    with captured(CLI_COMPONENT, LoggingSettings(), "reap-436", utc_now) as capture:
        command = ConformanceCommand(
            capture.diagnostics,
            clock=clock,
            identities=reader,
            child_probe=probe,
            child_watch=watches,
        )
        try:
            async with asyncio.timeout(20):
                await command.owner.ready()
                tree = await command.owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.TEMPORARY_TREE,
                    )
                )
                acquired_tree = await command.owner.acquire(
                    tree.token, new_operation_id(), TemporaryTreeRequest()
                )
                await command.owner.acknowledge(
                    tree.token, acquired_tree.operation_id, acquired_tree.revision
                )
                child = await command.owner.reserve(
                    ReservationRequest(
                        operation_id=new_operation_id(),
                        kind=ResourceKind.PROCESS_DOMAIN,
                        parent_resource_id=tree.token.resource_id,
                    )
                )
                acquired = await command.owner.acquire(
                    child.token,
                    new_operation_id(),
                    ProcessLaunch(
                        executable=sys.executable,
                        argv=(sys.executable, "-I", "-c", CHILD),
                        environment=(),
                        working_directory_resource_id=tree.token.resource_id,
                        stdin=None,
                        stdout=None,
                        stderr=None,
                        domain=ProcessDomain.INVOCATION,
                    ),
                )
                await command.owner.acknowledge(
                    child.token, acquired.operation_id, acquired.revision
                )
                process = command.owner.records[child.token.resource_id].process
                assert process is not None
                command.process = process
                registration = process.registration
                assert registration is not None
                await process.wait_exit(command.owner.deadline())
                reader.pin(registration)
                reader.mode = "absent"
                probe.result = Readiness.NOT_READY
                final = command.owner.deadline()
                settling = asyncio.create_task(process.settle(final))
                await until(
                    lambda: probe.calls >= 1 and watches.open_count() == 1,
                    settling,
                )
                assert command.outcome is OriginalOutcome.COMPLETE
                probe.result = Readiness.READY
                watches.notify()
                await settling
                await process.drain(final)
                assert process.release(final) == 0
                assert command.outcome is OriginalOutcome.COMPLETE
                command.owner.finalize_command_outcome(OriginalOutcome.COMPLETE)
                report = await command.owner.close(None)
        finally:
            if command.process is not None and not command.process.closed:
                command.process.signal(signal.SIGKILL)
                await command.process.wait_exit(clock.monotonic_ns() + 5_000_000_000)
                command.process.release(None)
            if not command.owner.invocation_cleanup.result.done():
                command.owner.finalize_command_outcome(OriginalOutcome.FAILED)
                await command.owner.close(None)

    assert report.state is CleanupOutcome.CLOSED
    assert report.original_outcome is OriginalOutcome.COMPLETE
    assert report.cleanup_outcome is CleanupOutcome.CLOSED
