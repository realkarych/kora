import asyncio
import os
import select
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from pydantic import TypeAdapter

from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    OperationId,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
)
from kora.protocols.time import Clock
from kora.runner.channels import (
    BoundedChannel,
    CaptureDrain,
    ChannelError,
    ChannelFailure,
    wait_future,
)
from kora.runner.gate import ExecutionConfig
from kora.runner.identity import (
    IdentityReader,
    IdentityState,
    NativeIdentityReader,
    ProcessSnapshot,
)


class ProcessCondition(StrEnum):
    LIVE = "live"
    SETTLED = "settled"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"
    START_FAILED = "start_failed"


class SignalResult(StrEnum):
    SENT = "sent"
    SETTLED = "settled"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


class ProcessError(Exception):
    def __init__(self, condition: ProcessCondition) -> None:
        self.condition = condition
        super().__init__(condition.value)


@dataclass(frozen=True)
class ProcessObservation:
    condition: ProcessCondition
    live_members: tuple[ProcessIdentity, ...]
    anchor_exited: bool
    capture_eof: bool

    @property
    def settled(self) -> bool:
        return self.condition is ProcessCondition.SETTLED


class QueueHandle(Protocol):
    def fileno(self) -> int: ...

    def close(self) -> None: ...


class ExitAnchor:
    def __init__(self, pid: int, *, direct_child: bool = True) -> None:
        self._pid = pid
        self._direct_child = direct_child
        self._loop = asyncio.get_running_loop()
        self._ready: asyncio.Future[None] = self._loop.create_future()
        self._pidfd: int | None = None
        self._queue: QueueHandle | None = None
        self._descriptor: int | None = None
        self.exited = False
        self.unknown = False
        try:
            if sys.platform == "linux":
                self._pidfd = os.pidfd_open(pid)
                self._descriptor = self._pidfd
            elif sys.platform == "darwin":
                queue = select.kqueue()
                self._queue = queue
                queue.control(
                    [
                        select.kevent(
                            pid,
                            filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                            fflags=select.KQ_NOTE_EXIT,
                        )
                    ],
                    0,
                    0,
                )
                self._descriptor = self._queue.fileno()
            else:
                raise ValueError("unsupported local process platform")
            self._loop.add_reader(self._descriptor, self._observe_exit)
        except OSError:
            self.unknown = True
            self._ready.set_result(None)

    def _observe_exit(self) -> None:
        try:
            if sys.platform == "linux" and self._pidfd is not None:
                if self._direct_child:
                    status = os.waitid(
                        os.P_PIDFD, self._pidfd, os.WEXITED | os.WNOWAIT | os.WNOHANG
                    )
                    if status is None:
                        return
                    self.exited = status.si_pid == self._pid
                    self.unknown = not self.exited
                else:
                    self.exited = True
            elif sys.platform == "darwin" and self._queue is not None:
                events = cast(select.kqueue, self._queue).control(None, 1, 0)
                if not events:
                    return
                self.exited = any(
                    event.ident == self._pid and event.fflags & select.KQ_NOTE_EXIT
                    for event in events
                )
                self.unknown = not self.exited
        except OSError:
            self.unknown = True
        if self._descriptor is not None:
            self._loop.remove_reader(self._descriptor)
        if not self._ready.done():
            self._ready.set_result(None)

    async def wait(self, deadline_ns: int, clock: Clock) -> None:
        if self.unknown:
            raise ProcessError(ProcessCondition.UNKNOWN)
        if self.exited:
            return
        await wait_future(self._ready, deadline_ns, clock)
        if self.unknown:
            raise ProcessError(ProcessCondition.UNKNOWN)

    @property
    def readiness(self) -> asyncio.Future[None]:
        return self._ready

    def close(self) -> None:
        if self._descriptor is not None:
            self._loop.remove_reader(self._descriptor)
            self._descriptor = None
        if self._pidfd is not None:
            os.close(self._pidfd)
            self._pidfd = None
        if self._queue is not None:
            self._queue.close()
            self._queue = None
        self._ready.cancel()


def complete_identity(identity: ProcessIdentity) -> bool:
    return all(
        value is not None
        for value in (identity.host_id, identity.boot_id, identity.pid_namespace)
    )


def matches_domain(process: ProcessSnapshot, registration: ProcessRegistration) -> bool:
    return (
        complete_identity(process.identity)
        and process.process_group_id == registration.process_group_id
        and process.session_id == registration.session_id
        and process.identity.host_id == registration.process.host_id
        and process.identity.boot_id == registration.process.boot_id
        and process.identity.pid_namespace == registration.process.pid_namespace
    )


class MemberWatches:
    def __init__(
        self, clock: Clock, identities: IdentityReader, capacity: int = 65536
    ) -> None:
        if isinstance(capacity, bool) or not 1 <= capacity <= 65536:
            raise ValueError("member watch capacity must be between 1 and 65536")
        self.clock = clock
        self.identities = identities
        self.capacity = capacity
        self.entries: dict[int, tuple[ProcessIdentity, ExitAnchor]] = {}

    def check_deadline(self, deadline_ns: int | None) -> None:
        if deadline_ns is not None and self.clock.monotonic_ns() >= deadline_ns:
            raise ProcessError(ProcessCondition.UNKNOWN)

    def retain(
        self,
        process: ProcessSnapshot,
        registration: ProcessRegistration,
        deadline_ns: int | None,
    ) -> None:
        self.check_deadline(deadline_ns)
        self.validate(process, registration, deadline_ns)
        pid = process.identity.pid
        previous = self.entries.get(pid)
        if previous is not None:
            if previous[0] != process.identity:
                raise ProcessError(ProcessCondition.MISMATCH)
            if previous[1].unknown:
                raise ProcessError(ProcessCondition.UNKNOWN)
            return
        if len(self.entries) >= self.capacity:
            raise ProcessError(ProcessCondition.UNKNOWN)
        anchor = ExitAnchor(pid, direct_child=False)
        try:
            self.check_deadline(deadline_ns)
            observed = self.identities.read(pid)
            self.check_deadline(deadline_ns)
            current = observed.process
            if (
                anchor.unknown
                or observed.state is not IdentityState.PRESENT
                or current is None
            ):
                raise ProcessError(ProcessCondition.UNKNOWN)
            if current.identity != process.identity or not matches_domain(
                current, registration
            ):
                raise ProcessError(ProcessCondition.MISMATCH)
        except ProcessError:
            anchor.close()
            raise
        self.entries[pid] = (process.identity, anchor)

    def validate(
        self,
        process: ProcessSnapshot,
        registration: ProcessRegistration,
        deadline_ns: int | None,
    ) -> None:
        self.check_deadline(deadline_ns)
        previous = self.entries.get(process.identity.pid)
        if not matches_domain(process, registration) or (
            previous is not None and previous[0] != process.identity
        ):
            raise ProcessError(ProcessCondition.MISMATCH)

    def omitted_pending(self, visible: set[int], deadline_ns: int | None) -> bool:
        pending = False
        for pid, (_, anchor) in tuple(self.entries.items()):
            self.check_deadline(deadline_ns)
            if pid not in visible:
                if anchor.unknown or not anchor.exited:
                    pending = True
                else:
                    anchor.close()
                    del self.entries[pid]
        return pending

    def pending(self) -> tuple[asyncio.Future[None], ...]:
        return tuple(
            anchor.readiness
            for _, anchor in self.entries.values()
            if not anchor.exited and not anchor.readiness.done()
        )

    def dispose(self) -> None:
        for _, anchor in self.entries.values():
            anchor.close()
        self.entries.clear()


async def wait_member_change(
    pending: tuple[asyncio.Future[None], ...], deadline_ns: int, clock: Clock
) -> None:
    if not pending:
        raise ProcessError(ProcessCondition.UNKNOWN)
    if clock.monotonic_ns() >= deadline_ns:
        raise ChannelError(ChannelFailure.DEADLINE)
    timeout = asyncio.create_task(clock.sleep_until(deadline_ns))
    try:
        await asyncio.wait((*pending, timeout), return_when=asyncio.FIRST_COMPLETED)
        if timeout.done():
            timeout.result()
            raise ChannelError(ChannelFailure.DEADLINE)
    finally:
        timeout.cancel()
        try:
            await timeout
        except asyncio.CancelledError:
            if not timeout.cancelled():
                raise


@dataclass(frozen=True)
class RecoveryObservation:
    condition: ProcessCondition
    live_members: tuple[ProcessIdentity, ...]
    observed_exits: tuple[ProcessIdentity, ...]
    group_empty: bool

    @property
    def settled(self) -> bool:
        return self.condition is ProcessCondition.SETTLED


class RecoveredProcess:
    def __init__(
        self,
        registration: ProcessRegistration,
        clock: Clock,
        identities: IdentityReader,
        *,
        max_members: int = 65536,
    ) -> None:
        self.registration = registration
        self.clock = clock
        self.identities = identities
        self.members = MemberWatches(clock, identities, max_members)
        self.closed = False
        self.disposed = False

    @property
    def watcher_count(self) -> int:
        return len(self.members.entries)

    def _scope_matches(self) -> bool:
        registration = self.registration
        current = self.identities.read(os.getpid())
        process = current.process
        return (
            current.state is IdentityState.PRESENT
            and process is not None
            and complete_identity(process.identity)
            and complete_identity(registration.process)
            and registration.process.leads_group
            and registration.process_group_id == registration.process.pid
            and registration.process_group_id != os.getpgrp()
            and process.identity.host_id == registration.process.host_id
            and process.identity.boot_id == registration.process.boot_id
            and process.identity.pid_namespace == registration.process.pid_namespace
        )

    def observe(self, deadline_ns: int) -> RecoveryObservation:
        unknown = RecoveryObservation(ProcessCondition.UNKNOWN, (), (), False)
        if self.closed:
            return RecoveryObservation(ProcessCondition.SETTLED, (), (), True)
        if self.disposed or not self._scope_matches():
            return unknown
        previous_count = self.watcher_count
        try:
            self.members.check_deadline(deadline_ns)
            leader = self.identities.read(self.registration.process.pid)
            process = leader.process
            if leader.state is IdentityState.UNKNOWN:
                return unknown
            if process is not None and (
                process.identity != self.registration.process
                or not matches_domain(process, self.registration)
            ):
                return unknown
            if process is not None and not process.dead:
                self.members.retain(process, self.registration, deadline_ns)
            group = self.identities.group(
                self.registration.process_group_id, deadline_ns
            )
            if group.state is not IdentityState.PRESENT:
                return unknown
            if (
                process is not None
                and not process.dead
                and process.identity.pid not in {pid for pid, _ in group.members}
            ):
                return unknown
            omitted = self.members.omitted_pending(
                {pid for pid, _ in group.members}, deadline_ns
            )
            live: list[ProcessIdentity] = []
            for _, member in group.members:
                self.members.check_deadline(deadline_ns)
                candidate = member.process
                if member.state is not IdentityState.PRESENT or candidate is None:
                    return unknown
                self.members.validate(candidate, self.registration, deadline_ns)
                if not candidate.dead:
                    self.members.retain(candidate, self.registration, deadline_ns)
                    live.append(candidate.identity)
            exits = tuple(
                identity
                for identity, anchor in self.members.entries.values()
                if anchor.exited
            )
            if omitted:
                return RecoveryObservation(
                    ProcessCondition.UNKNOWN, tuple(live), exits, False
                )
            if leader.state is IdentityState.ABSENT and not group.members:
                confirmation = self.identities.group(
                    self.registration.process_group_id, deadline_ns
                )
                final_leader = self.identities.read(self.registration.process.pid)
                self.members.check_deadline(deadline_ns)
                if (
                    confirmation.state is IdentityState.PRESENT
                    and not confirmation.members
                    and final_leader.state is IdentityState.ABSENT
                    and all(
                        anchor.exited and not anchor.unknown
                        for _, anchor in self.members.entries.values()
                    )
                ):
                    return RecoveryObservation(
                        ProcessCondition.SETTLED, (), exits, True
                    )
            condition = (
                ProcessCondition.LIVE
                if process is not None and not process.dead
                else ProcessCondition.UNKNOWN
            )
            return RecoveryObservation(condition, tuple(live), exits, False)
        except ProcessError:
            if previous_count == 0 or self.watcher_count > previous_count:
                self.dispose_observation()
            return unknown

    def signal(self, requested: signal.Signals, deadline_ns: int) -> SignalResult:
        if requested not in (signal.SIGTERM, signal.SIGKILL):
            raise ValueError("only TERM and KILL are local cleanup signals")
        observation = self.observe(deadline_ns)
        if observation.settled:
            return SignalResult.SETTLED
        if observation.condition is not ProcessCondition.LIVE:
            return SignalResult.UNKNOWN
        current = self.identities.read(self.registration.process.pid)
        process = current.process
        if (
            self.clock.monotonic_ns() >= deadline_ns
            or current.state is not IdentityState.PRESENT
            or process is None
            or process.dead
            or process.identity != self.registration.process
            or not matches_domain(process, self.registration)
        ):
            return SignalResult.UNKNOWN
        try:
            os.killpg(self.registration.process_group_id, requested)
        except (PermissionError, ProcessLookupError):
            return SignalResult.UNKNOWN
        return SignalResult.SENT

    async def settle(self, deadline_ns: int) -> None:
        while True:
            observation = self.observe(deadline_ns)
            if observation.settled:
                return
            await wait_member_change(self.members.pending(), deadline_ns, self.clock)

    def release(self, deadline_ns: int) -> None:
        if self.closed:
            return
        if not self.observe(deadline_ns).settled:
            raise ProcessError(ProcessCondition.UNKNOWN)
        self.members.dispose()
        self.closed = True

    def dispose_observation(self) -> None:
        self.members.dispose()
        self.disposed = True


class GatedProcess:
    def __init__(
        self,
        launch: ProcessLaunch,
        operation_id: OperationId,
        deadline_ns: int,
        clock: Clock,
        identities: IdentityReader,
        cwd_descriptor: int,
    ) -> None:
        self._launch = launch
        self._operation_id = operation_id
        self._deadline = deadline_ns
        self._clock = clock
        self._identities = identities
        self._process: subprocess.Popen[bytes] | None = None
        self._anchor: ExitAnchor | None = None
        self._watches = MemberWatches(clock, identities)
        self._channel: BoundedChannel | None = None
        self._captures: list[CaptureDrain] = []
        self._registration: ProcessRegistration | None = None
        self._start_failed = False
        self._prepared = False
        self._activated = False
        self._closing = False
        self._released = False
        self._start(cwd_descriptor)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @property
    def closed(self) -> bool:
        return self._released

    @property
    def capture_bytes(self) -> int:
        return sum(capture.bytes_discarded for capture in self._captures)

    def _output_descriptor(self, mode: ProcessOutput | None, owned: ExitStack) -> int:
        if mode is ProcessOutput.CAPTURE:
            reading, writing = os.pipe()
            owned.callback(os.close, writing)
            try:
                capture = CaptureDrain(reading)
            except OSError:
                os.close(reading)
                raise
            self._captures.append(capture)
            return writing
        descriptor = os.open(os.devnull, os.O_WRONLY)
        owned.callback(os.close, descriptor)
        return descriptor

    def _start(self, cwd_descriptor: int) -> None:
        asyncio.get_running_loop()
        try:
            with ExitStack() as child_descriptors:
                parent, child = socket.socketpair()
                self._channel = BoundedChannel(parent, self._clock)
                child_descriptors.callback(child.close)
                gate_deadline = time.monotonic_ns() + max(
                    0, self._deadline - self._clock.monotonic_ns()
                )
                parent.sendall(gate_deadline.to_bytes(8, "big"))
                directory = os.dup(cwd_descriptor)
                child_descriptors.callback(os.close, directory)
                output = self._output_descriptor(self._launch.stdout, child_descriptors)
                errors = self._output_descriptor(self._launch.stderr, child_descriptors)
                self._process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "kora.runner.gate",
                    ],
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    stdout=output,
                    stderr=errors,
                    env={
                        "KORA_GATE_CONTROL_FD": str(child.fileno()),
                        "KORA_GATE_CWD_FD": str(directory),
                    },
                    close_fds=True,
                    pass_fds=(child.fileno(), directory),
                    process_group=(
                        0 if self._launch.domain is ProcessDomain.INVOCATION else None
                    ),
                    start_new_session=self._launch.domain is ProcessDomain.DETACHED,
                )
                self._anchor = ExitAnchor(self._process.pid)
                observed = self._identities.read(self._process.pid)
                process = observed.process
                if observed.state is IdentityState.PRESENT and process is not None:
                    self._registration = ProcessRegistration(
                        process=process.identity,
                        acquisition_operation_id=self._operation_id,
                        process_group_id=process.process_group_id,
                        session_id=process.session_id,
                    )
        except OSError:
            self._start_failed = True
            self.close_gate()
            if self._process is None:
                for capture in self._captures:
                    capture.close()
                self._released = True

    def _registered(self) -> ProcessRegistration:
        if self._start_failed:
            raise ProcessError(ProcessCondition.START_FAILED)
        if self._registration is None:
            raise ProcessError(ProcessCondition.UNKNOWN)
        registration = self._registration
        if not complete_identity(registration.process):
            raise ProcessError(ProcessCondition.UNKNOWN)
        if (
            self._process is None
            or registration.process.pid != self._process.pid
            or registration.process_group_id != registration.process.pid
            or registration.process_group_id == os.getpgrp()
            or not registration.process.leads_group
            or (
                self._launch.domain is ProcessDomain.DETACHED
                and registration.session_id != registration.process.pid
            )
            or (
                self._launch.domain is ProcessDomain.INVOCATION
                and registration.session_id != os.getsid(0)
            )
        ):
            raise ProcessError(ProcessCondition.MISMATCH)
        return registration

    async def prepare(self) -> ProcessRegistration:
        registration = self._registered()
        anchor = self._anchor
        channel = self._channel
        if anchor is None or anchor.unknown or channel is None or self._closing:
            raise ProcessError(ProcessCondition.UNKNOWN)
        if self._prepared:
            return registration
        metadata, payload = ExecutionConfig.from_launch(self._launch).encode()
        await channel.write_frame(metadata, self._deadline)
        await channel.write_record(payload, self._deadline)
        ready = await channel.read_frame(self._deadline)
        if ready != b"ready":
            raise ChannelError(ChannelFailure.MALFORMED)
        observation = self.observe(self._deadline)
        if observation.condition is not ProcessCondition.LIVE:
            raise ProcessError(observation.condition)
        self._prepared = True
        return registration

    async def acknowledge(self) -> None:
        if self._closing or not self._prepared:
            raise ProcessError(ProcessCondition.UNKNOWN)
        if self._activated:
            return
        channel = self._channel
        if channel is None:
            raise ProcessError(ProcessCondition.UNKNOWN)
        observation = self.observe(self._deadline)
        if observation.condition is not ProcessCondition.LIVE:
            raise ProcessError(observation.condition)
        await channel.write_frame(b"activate", self._deadline)
        self._activated = True
        channel.close()

    def close_gate(self) -> None:
        self._closing = True
        if self._channel is not None:
            self._channel.close()

    def _matches(self, process: ProcessSnapshot) -> bool:
        registration = self._registration
        return registration is not None and (
            process.identity == registration.process
            and process.process_group_id == registration.process_group_id
            and process.session_id == registration.session_id
        )

    def _confirm_dead_group(
        self, known_dead: dict[int, ProcessIdentity], deadline_ns: int | None
    ) -> bool:
        registration = self._registration
        if registration is None:
            return False
        confirmation = self._identities.group(
            registration.process_group_id, deadline_ns
        )
        if confirmation.state is not IdentityState.PRESENT:
            return False
        if registration.process.pid not in tuple(
            pid for pid, _ in confirmation.members
        ):
            return False
        for pid, observation in confirmation.members:
            if pid not in known_dead or observation.state is IdentityState.UNKNOWN:
                return False
            process = observation.process
            if process is not None and (
                not process.dead
                or process.identity != known_dead[pid]
                or process.process_group_id != registration.process_group_id
                or process.session_id != registration.session_id
            ):
                return False
        return True

    def observe(self, deadline_ns: int | None = None) -> ProcessObservation:
        anchor = self._anchor
        registration = self._registration
        exited = anchor is not None and anchor.exited
        eof = all(capture.eof for capture in self._captures)
        if registration is None or anchor is None or anchor.unknown:
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        if self._process is None or registration.process.pid != self._process.pid:
            return ProcessObservation(ProcessCondition.MISMATCH, (), exited, eof)
        if not complete_identity(registration.process):
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        leader = self._identities.read(registration.process.pid)
        if leader.state is IdentityState.UNKNOWN:
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        if leader.process is not None and not self._matches(leader.process):
            return ProcessObservation(ProcessCondition.MISMATCH, (), exited, eof)
        if leader.state is IdentityState.ABSENT and not exited:
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        group = self._identities.group(registration.process_group_id, deadline_ns)
        if group.state is not IdentityState.PRESENT:
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        if registration.process.pid not in tuple(pid for pid, _ in group.members):
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        member_pids = {pid for pid, _ in group.members}
        try:
            omitted_pending = self._watches.omitted_pending(member_pids, deadline_ns)
        except ProcessError:
            return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
        live: list[ProcessIdentity] = []
        dead: dict[int, ProcessIdentity] = {}
        for pid, member in group.members:
            if deadline_ns is not None and self._clock.monotonic_ns() >= deadline_ns:
                return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
            if member.state is IdentityState.ABSENT:
                if pid == registration.process.pid and exited:
                    dead[pid] = registration.process
                    continue
                previous = self._watches.entries.get(pid)
                if previous is not None and previous[1].exited:
                    dead[pid] = previous[0]
                    continue
                return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
            process = member.process
            if member.state is IdentityState.UNKNOWN or process is None:
                return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
            try:
                self._watches.validate(process, registration, deadline_ns)
            except ProcessError as error:
                return ProcessObservation(error.condition, (), exited, eof)
            if not process.dead:
                live.append(process.identity)
                if pid != registration.process.pid:
                    try:
                        self._watches.retain(process, registration, deadline_ns)
                    except ProcessError as error:
                        return ProcessObservation(error.condition, (), exited, eof)
            else:
                dead[pid] = process.identity
        if omitted_pending:
            return ProcessObservation(
                ProcessCondition.UNKNOWN, tuple(live), exited, eof
            )
        condition = (
            ProcessCondition.SETTLED if exited and not live else ProcessCondition.LIVE
        )
        if condition is ProcessCondition.SETTLED and not self._confirm_dead_group(
            dead, deadline_ns
        ):
            condition = ProcessCondition.UNKNOWN
        return ProcessObservation(condition, tuple(live), exited, eof)

    def signal(
        self, requested: signal.Signals, deadline_ns: int | None = None
    ) -> SignalResult:
        if requested not in (signal.SIGTERM, signal.SIGKILL):
            raise ValueError("only TERM and KILL are local cleanup signals")
        self.close_gate()
        if self._released:
            return SignalResult.SETTLED
        registration = self._registration
        if registration is None or registration.process_group_id == os.getpgrp():
            return SignalResult.UNKNOWN
        observation = self.observe(deadline_ns)
        if observation.condition is ProcessCondition.MISMATCH:
            return SignalResult.MISMATCH
        if observation.condition is ProcessCondition.UNKNOWN:
            return SignalResult.UNKNOWN
        if observation.settled:
            return SignalResult.SETTLED
        try:
            os.killpg(registration.process_group_id, requested)
        except (PermissionError, ProcessLookupError):
            return (
                SignalResult.SETTLED
                if self.observe(deadline_ns).settled
                else SignalResult.UNKNOWN
            )
        return SignalResult.SENT

    async def wait_exit(self, deadline_ns: int) -> None:
        if self._anchor is None:
            raise ProcessError(ProcessCondition.UNKNOWN)
        await self._anchor.wait(deadline_ns, self._clock)

    async def drain(self, deadline_ns: int) -> None:
        for capture in self._captures:
            await capture.drain(deadline_ns, self._clock)

    async def settle(self, deadline_ns: int) -> None:
        await self.wait_exit(deadline_ns)
        while True:
            observation = self.observe(deadline_ns)
            if observation.settled:
                return
            if observation.condition is ProcessCondition.MISMATCH:
                raise ProcessError(observation.condition)
            if self._clock.monotonic_ns() >= deadline_ns:
                raise ChannelError(ChannelFailure.DEADLINE)
            anchors = [member[1] for member in self._watches.entries.values()]
            if self._anchor is not None:
                anchors.append(self._anchor)
            pending = tuple(
                anchor.readiness
                for anchor in anchors
                if not anchor.exited and not anchor.readiness.done()
            )
            if not pending or any(anchor.unknown for anchor in anchors):
                raise ProcessError(ProcessCondition.UNKNOWN)
            await wait_member_change(pending, deadline_ns, self._clock)

    def release(self, deadline_ns: int | None = None) -> int:
        if self._released:
            if self.returncode is None:
                raise ProcessError(ProcessCondition.START_FAILED)
            return self.returncode
        observation = self.observe(deadline_ns)
        if not observation.settled or not observation.capture_eof:
            raise ProcessError(observation.condition)
        process = self._process
        anchor = self._anchor
        if process is None or anchor is None:
            raise ProcessError(ProcessCondition.UNKNOWN)
        pid, status = os.waitpid(process.pid, os.WNOHANG)
        if pid != process.pid:
            raise ProcessError(ProcessCondition.UNKNOWN)
        process.returncode = os.waitstatus_to_exitcode(status)
        anchor.close()
        self._watches.dispose()
        self.close_gate()
        for capture in self._captures:
            capture.close()
        self._released = True
        return process.returncode


def start_gated(
    launch: ProcessLaunch,
    operation_id: OperationId,
    cwd_descriptor: int,
    acquisition_deadline_ns: int,
    clock: Clock,
    identities: IdentityReader | None = None,
) -> GatedProcess:
    validated_operation = TypeAdapter(OperationId).validate_python(operation_id)
    if (
        isinstance(acquisition_deadline_ns, bool)
        or not 0 <= acquisition_deadline_ns <= 2**63 - 1
    ):
        raise ValueError("acquisition deadline is a bounded monotonic instant")
    owner = GatedProcess(
        launch,
        validated_operation,
        acquisition_deadline_ns,
        clock,
        identities if identities is not None else NativeIdentityReader(clock=clock),
        cwd_descriptor,
    )
    return owner
