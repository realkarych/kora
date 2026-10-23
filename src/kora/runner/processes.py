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
        self._members: dict[int, tuple[ProcessIdentity, ExitAnchor]] = {}
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
        for pid, (_, previous_anchor) in tuple(self._members.items()):
            if pid not in member_pids and previous_anchor.exited:
                previous_anchor.close()
                del self._members[pid]
        live: list[ProcessIdentity] = []
        dead: dict[int, ProcessIdentity] = {}
        for pid, member in group.members:
            if member.state is IdentityState.ABSENT:
                if pid == registration.process.pid and exited:
                    dead[pid] = registration.process
                    continue
                previous = self._members.get(pid)
                if previous is not None and previous[1].exited:
                    dead[pid] = previous[0]
                    continue
                return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
            process = member.process
            if member.state is IdentityState.UNKNOWN or process is None:
                return ProcessObservation(ProcessCondition.UNKNOWN, (), exited, eof)
            if (
                not complete_identity(process.identity)
                or process.process_group_id != registration.process_group_id
                or process.session_id != registration.session_id
                or process.identity.host_id != registration.process.host_id
                or process.identity.boot_id != registration.process.boot_id
                or process.identity.pid_namespace != registration.process.pid_namespace
            ):
                return ProcessObservation(ProcessCondition.MISMATCH, (), exited, eof)
            if not process.dead:
                live.append(process.identity)
                if pid != registration.process.pid:
                    previous = self._members.get(pid)
                    if previous is not None and previous[0] != process.identity:
                        return ProcessObservation(
                            ProcessCondition.MISMATCH, (), exited, eof
                        )
                    if previous is None:
                        if len(self._members) >= 65536:
                            return ProcessObservation(
                                ProcessCondition.UNKNOWN, (), exited, eof
                            )
                        self._members[pid] = (
                            process.identity,
                            ExitAnchor(pid, direct_child=False),
                        )
            else:
                dead[pid] = process.identity
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
            anchors = [member[1] for member in self._members.values()]
            if self._anchor is not None:
                anchors.append(self._anchor)
            pending = [anchor.readiness for anchor in anchors if not anchor.exited]
            if not pending or any(anchor.unknown for anchor in anchors):
                raise ProcessError(ProcessCondition.UNKNOWN)
            timeout = asyncio.create_task(self._clock.sleep_until(deadline_ns))
            try:
                await asyncio.wait(
                    (*pending, timeout), return_when=asyncio.FIRST_COMPLETED
                )
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
        for _, member_anchor in self._members.values():
            member_anchor.close()
        self._members.clear()
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
