import asyncio
import errno
import os
import socket
import stat
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Final, TypeVar

from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.accepted_socket import accept_socket
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    ENVELOPE_ERRORS,
    FRAME_PROGRESS_NS,
    MESSAGE_PROGRESS_NS,
    PRE_ATTACH_BYTES,
    SECTION_FRAME_BYTES,
    AttachRequest,
    FrameType,
    OutputRequest,
    StatusValue,
    TransferRequest,
    decode_section,
    delivery_envelope,
    delivery_error,
    error_envelope,
    operation_unknown,
    output_envelope,
    output_trailer,
    parse_header,
    read_output,
    read_request,
    read_transfer,
    read_transfer_bytes,
    receive_section,
    status_envelope,
    transfer_cutoff,
    workload_digest,
    write_transfer_bytes,
)
from kora.plugins.runtime.local.command import CommandSession, Incoming, OpenedOutput
from kora.plugins.runtime.local.spawn import open_socket_directory
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import ProcessIdentity
from kora.protocols.runtime import RuntimeStatus, TransferItem, Workload
from kora.protocols.time import Clock
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    ChannelFailure,
    cancel_task,
)
from kora.runner.filesystem import DirectoryAnchor, FilesystemError
from kora.runner.identity import IdentityReader, IdentityState
from kora.runner.journal import space_error
from kora.runner.native_io import (
    NativeIONotInvoked,
    NativeIOSubmissionUncertain,
    NativeIOUnresolved,
    observed,
)
from kora.runner.peers import PeerRefused, PeerSource, unchanged
from kora.runner.registry import (
    AttemptKey,
    LockOutcome,
    Registry,
    Running,
    Submitter,
    TransitionExecutor,
    refusal,
    takeover_cell,
)

Result = TypeVar("Result")

UNATTACHED: Final[int] = 8
CONNECTIONS: Final[int] = 16
FIRST_FRAME_NS: Final[int] = 5_000_000_000
REVERIFY_NS: Final[int] = 60_000_000_000
LISTEN_BACKLOG: Final[int] = 16
EXECUTOR_WORKERS: Final[int] = 2
EXECUTOR_JOBS: Final[int] = 18
COMPONENT: Final[str] = CATALOG.register_component("runtime.local.runner")
AUTH_REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.auth_refused",
        COMPONENT,
        Level.WARNING,
        "the attempt runner refused control connections",
        {"reason": FieldKind.STRING, "count": FieldKind.INTEGER},
    )
)
SOCKET_RECHECKED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.socket_rechecked",
        COMPONENT,
        Level.WARNING,
        "the attempt runner found its socket missing or replaced",
        {"outcome": FieldKind.STRING},
    )
)


class Refusal(StrEnum):
    BEFORE_ATTACH = "before_attach"
    MALFORMED = "malformed"
    HANDSHAKE_TIMEOUT = "handshake_timeout"
    NOT_SUBMITTER = "not_submitter"
    HEAD_CHANGED = "head_changed"


class AuthenticationRefused(Exception):
    def __init__(self, reason: Refusal) -> None:
        self.reason = reason
        super().__init__(reason)


class Rebind(StrEnum):
    REBOUND = "rebound"
    REFUSED = "refused"


FRAME_REFUSALS: Final[dict[ChannelFailure, Refusal]] = {
    ChannelFailure.DEADLINE: Refusal.HANDSHAKE_TIMEOUT,
    ChannelFailure.MALFORMED: Refusal.MALFORMED,
    ChannelFailure.CAPACITY: Refusal.MALFORMED,
}


@dataclass(frozen=True)
class BoundSocket:
    listener: socket.socket
    directory: DirectoryAnchor
    entry: tuple[int, int]

    def close(self) -> None:
        try:
            self.listener.close()
        finally:
            self.directory.close()


def socket_entry(directory: DirectoryAnchor, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def bound_socket(
    listener: socket.socket, directory: DirectoryAnchor, name: str
) -> BoundSocket:
    entry = socket_entry(directory, name)
    if entry is None or not stat.S_ISSOCK(entry.st_mode):
        raise FilesystemError("socket_entry_missing")
    listener.setblocking(False)
    return BoundSocket(listener, directory, (entry.st_dev, entry.st_ino))


def unchanged_entry(bound: BoundSocket, path: Path, name: str) -> bool:
    try:
        directory = open_socket_directory(path)
        try:
            current = os.fstat(directory.descriptor)
            previous = os.fstat(bound.directory.descriptor)
            if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                return False
            entry = socket_entry(directory, name)
        finally:
            directory.close()
    except (OSError, FilesystemError):
        return False
    return entry is not None and (entry.st_dev, entry.st_ino) == bound.entry


def rebound(path: Path, name: str) -> BoundSocket:
    directory = open_socket_directory(path)
    try:
        existing = socket_entry(directory, name)
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise FilesystemError("socket_entry_foreign")
            os.unlink(name, dir_fd=directory.descriptor)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path / name))
            listener.listen(LISTEN_BACKLOG)
            return bound_socket(listener, directory, name)
        except BaseException:
            listener.close()
            raise
    except BaseException:
        directory.close()
        raise


def decoded_workload(section: bytearray) -> Workload:
    return decode_section(section, ACCEPT_WORKLOAD.sections[0], Workload)


def recorded_absent(reader: IdentityReader, recorded: ProcessIdentity) -> bool:
    if (
        recorded.host_id is None
        or recorded.boot_id is None
        or recorded.pid_namespace is None
    ):
        return False
    try:
        observation = reader.read(recorded.pid)
    except OSError:
        return False
    if observation.state is IdentityState.ABSENT:
        return True
    process = observation.process
    if observation.state is not IdentityState.PRESENT or process is None:
        return False
    identity = process.identity
    if (
        identity.host_id != recorded.host_id
        or identity.boot_id != recorded.boot_id
        or identity.pid_namespace != recorded.pid_namespace
    ):
        return False
    return process.dead or identity.start_time != recorded.start_time


class ControlServer:
    def __init__(
        self,
        bound: BoundSocket,
        path: Path,
        name: str,
        clock: Clock,
        source: PeerSource,
        log: ComponentLogger,
        registry: Registry,
        key: AttemptKey,
        identity: ProcessIdentity,
        identities: IdentityReader,
        *,
        binder: Callable[[Path, str], BoundSocket] = rebound,
        decoder: Callable[[bytearray], Workload] = decoded_workload,
        command: CommandSession | None = None,
    ) -> None:
        bound.listener.setblocking(False)
        self._bound = bound
        self._path = path
        self._name = name
        self._clock = clock
        self._source = source
        self._log = log
        self._binder = binder
        self._decoder = decoder
        self._command = command
        self._deadline_closed = False
        self._registry = registry
        self._key = key
        self._identity = identity
        self._identities = identities
        self._observers = 0
        self._admitted = False
        self._workload: Workload | None = None
        self._workload_sha256: str | None = None
        self._executor = TransitionExecutor(EXECUTOR_WORKERS, EXECUTOR_JOBS)
        self._connections: set[asyncio.Task[None]] = set()
        self._unresolved_disposal: Callable[[], None] | None = None
        self._transfer_native_settled = False
        self._resources_settled = False
        self._refusals: Counter[str] = Counter()
        self._accepting: asyncio.Task[None] | None = None
        self._ticking: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None

    @property
    def connections(self) -> int:
        return len(self._connections)

    def tracked_connections(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self._connections)

    @property
    def resources_settled(self) -> bool:
        return self._resources_settled

    def start(self) -> None:
        self._accepting = asyncio.create_task(self._accept(self._bound.listener))
        self._ticking = asyncio.create_task(self._tick())

    async def _accept(self, listener: socket.socket) -> None:
        while True:
            owned = await accept_socket(listener, self._source.stamp)
            try:
                if len(self._connections) >= CONNECTIONS:
                    owned.close()
                    continue
                deadline = self._clock.monotonic_ns() + FIRST_FRAME_NS
                task = owned.start(partial(self._serve, deadline=deadline))
                self._connections.add(task)
                task.add_done_callback(self._connections.discard)
            except BaseException:
                owned.close()
                raise

    def _refused(self, reason: str) -> None:
        self._refusals[reason] += 1

    def _flush(self) -> None:
        for reason, count in sorted(self._refusals.items()):
            self._log.emit(AUTH_REFUSED, reason=reason, count=count)
        self._refusals.clear()

    async def _expire(self, endpoint: socket.socket, deadline: int) -> None:
        await self._clock.sleep_until(deadline)
        try:
            endpoint.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            if error.errno != errno.ENOTCONN:
                raise

    async def _serve(self, endpoint: socket.socket, stamp: int, deadline: int) -> None:
        source = self._source
        expiration = asyncio.create_task(self._expire(endpoint, deadline))
        observer = False
        try:
            peer = await self._executor.run(lambda: source.attest(endpoint, stamp))
            entry = await self._executor.run(lambda: self._registry.entry(self._key))
            candidate = (
                isinstance(entry, Running)
                and entry.runner == self._identity
                and entry.submitter.process == peer
            )
            if not candidate:
                if self._observers >= UNATTACHED:
                    return
                self._observers += 1
                observer = True
            channel = BoundedChannel(endpoint, self._clock)
            reservations = {FrameType.JSON.value: PRE_ATTACH_BYTES}
            _, payload = await channel.read_typed_frame(deadline, reservations)
            request = read_request(payload)
            if request is None:
                self._refused(Refusal.MALFORMED)
                return
            if isinstance(request, AttachRequest):
                submitter = Submitter(dispatcher_id=request.dispatcher_id, process=peer)
                head = await self._executor.run(self._running)
                observational = False
                if head.submitter != submitter:
                    recorded = head.submitter.process
                    gone = await self._executor.run(
                        lambda: recorded_absent(self._identities, recorded)
                    )
                    if not gone:
                        raise AuthenticationRefused(Refusal.NOT_SUBMITTER)
                    observational = True
                else:
                    await self._executor.run(lambda: unchanged(self._identities, peer))
                if observer:
                    self._observers -= 1
                    observer = False
                await channel.write_typed_frame(
                    FrameType.JSON,
                    delivery_envelope(request.request_id, "attached"),
                    deadline,
                )
                await cancel_task(expiration)
                await self._attached(channel, submitter, observational)
                return
            if request.op != "status":
                self._refused(Refusal.BEFORE_ATTACH)
                return
            await cancel_task(expiration)
            if not observer:
                if self._observers >= UNATTACHED:
                    return
                self._observers += 1
                observer = True
            answer = status_envelope(request.request_id, self._observed())
            await channel.write_typed_frame(
                FrameType.JSON, answer, self._clock.monotonic_ns() + FRAME_PROGRESS_NS
            )
        except PeerRefused as refused:
            self._refused(refused.reason)
        except AuthenticationRefused as refused:
            self._refused(refused.reason)
        except ChannelError as error:
            refusal = FRAME_REFUSALS.get(error.reason)
            if refusal is not None:
                self._refused(refusal)
        except KoraProtocolError:
            pass
        finally:
            try:
                await cancel_task(expiration)
            finally:
                if observer:
                    self._observers -= 1

    def _running(self) -> Running:
        entry = self._registry.entry(self._key)
        if not isinstance(entry, Running) or entry.runner != self._identity:
            raise AuthenticationRefused(Refusal.HEAD_CHANGED)
        return entry

    def _authorized(self, submitter: Submitter, cutoff: int | None = None) -> Running:
        entry = self._running()
        if entry.submitter != submitter:
            raise delivery_error("submitter_superseded")
        unchanged(self._identities, submitter.process)
        deadline = entry.deadline
        if entry.gate_closed:
            self._deadline_closed = True
            raise delivery_error("deadline_expired")
        if (
            self._deadline_closed
            or (cutoff is not None and self._clock.monotonic_ns() >= cutoff)
            or (
                deadline is not None
                and self._clock.now() >= deadline.at - deadline.clock_uncertainty
            )
        ):
            self._deadline_closed = True
            applied = self._registry.try_transition(
                self._key,
                lambda current: takeover_cell(current, entry, submitter, True),
            )
            if isinstance(applied, LockOutcome):
                raise refusal("registry_busy")
            raise delivery_error("deadline_expired")
        return entry

    async def _answer(self, channel: BoundedChannel, payload: bytes) -> None:
        await channel.write_typed_frame(
            FrameType.JSON, payload, self._clock.monotonic_ns() + FRAME_PROGRESS_NS
        )

    async def _attached(
        self,
        channel: BoundedChannel,
        submitter: Submitter,
        observational: bool,
    ) -> None:
        while True:
            started = self._clock.monotonic_ns()
            _, payload = await channel.read_typed_frame(
                started + FRAME_PROGRESS_NS, {FrameType.JSON.value: PRE_ATTACH_BYTES}
            )
            request = read_request(payload)
            if request is not None:
                if request.op == "status":
                    answer = status_envelope(request.request_id, self._observed())
                elif request.op == "start":
                    answer = await self._start(
                        submitter, request.request_id, observational
                    )
                else:
                    answer = error_envelope(request.request_id, operation_unknown())
                await self._answer(channel, answer)
                continue
            transfer = read_transfer(payload)
            if transfer is not None:
                await self._receive_transfer(
                    channel, submitter, observational, transfer, started
                )
                continue
            output = read_output(payload)
            if output is not None:
                await self._send_output(channel, submitter, observational, output)
                return
            header = parse_header(payload, ACCEPT_WORKLOAD)
            declaration = header.sections[0]
            refusal: KoraProtocolError | None = None
            try:
                await self._executor.run(lambda: self._authorized(submitter))
            except KoraProtocolError as error:
                if error.code not in {"submitter_superseded", "deadline_expired"}:
                    raise
                refusal = error
            repeated = declaration.sha256 == self._workload_sha256
            if refusal is None and self._admitted and not repeated:
                refusal = delivery_error("workload_busy")
            sink = refusal is not None or repeated
            if not sink:
                self._admitted = True
            prelaunch: dict[int, int] | None = None
            try:
                section = await receive_section(
                    channel,
                    declaration,
                    self._clock,
                    started + MESSAGE_PROGRESS_NS,
                    sink=sink,
                )
                if refusal is not None:
                    await self._answer(
                        channel, error_envelope(header.request_id, refusal)
                    )
                    continue
                decoded = (
                    None
                    if repeated
                    else await self._executor.run(partial(self._decoder, section))
                )
                section = bytearray()
                try:
                    entry = await self._executor.run(
                        lambda: self._authorized(submitter)
                    )
                    if decoded is not None:
                        digest = await self._executor.run(
                            partial(workload_digest, decoded)
                        )
                        entry = await self._executor.run(
                            lambda: self._authorized(submitter)
                        )
                        if (
                            decoded.dispatcher_id != submitter.dispatcher_id
                            or decoded.dispatcher_process != submitter.process
                            or digest != entry.workload_digest
                        ):
                            raise delivery_error("workload_conflict")
                        if self._workload is None:
                            command = self._command
                            if (
                                sys.platform == "darwin"
                                and command is not None
                                and decoded.kind == "command"
                            ):
                                prelaunch = {}
                                await self._executor.run(
                                    partial(
                                        command.capture_prelaunch,
                                        prelaunch,
                                        decoded,
                                        started + MESSAGE_PROGRESS_NS,
                                    )
                                )
                                await self._executor.run(
                                    lambda: self._authorized(submitter)
                                )
                            if command is not None:
                                command.note(submitter, prelaunch)
                            self._workload = decoded
                            self._workload_sha256 = declaration.sha256
                    answer = delivery_envelope(header.request_id, "accepted")
                except KoraProtocolError as error:
                    if error.code not in ENVELOPE_ERRORS:
                        raise
                    answer = error_envelope(header.request_id, error)
                prelaunch = None
                await self._answer(channel, answer)
            finally:
                prelaunch = None
                section = bytearray()
                decoded = None
                if not sink:
                    self._admitted = False

    def _transfer_answer(self, request_id: str, error: KoraProtocolError) -> bytes:
        if error.code not in ENVELOPE_ERRORS:
            raise error
        return error_envelope(request_id, error)

    async def _receive_transfer(
        self,
        channel: BoundedChannel,
        submitter: Submitter,
        observational: bool,
        request: TransferRequest,
        started: int,
    ) -> None:
        item = request.item
        command = self._command
        cutoff: int | None = None
        deadline = started + MESSAGE_PROGRESS_NS
        claimed = False

        def authorize() -> None:
            if self._closing is not None:
                raise delivery_error("transfer_refused")
            self._authorized(submitter, cutoff)
            if self._clock.monotonic_ns() >= deadline:
                raise ChannelError(ChannelFailure.DEADLINE)

        try:
            try:
                if command is None:
                    raise delivery_error("transfer_refused")
                if observational:
                    raise delivery_error("execution_unauthorized")
                entry = await self._transfer_native(lambda: self._authorized(submitter))
                cutoff = transfer_cutoff(self._clock, entry.deadline)
                if cutoff is not None:
                    deadline = min(deadline, cutoff)
                decision = command.decide_transfer(submitter, observational, item)
                claimed = True
            except KoraProtocolError as error:
                if error.code == "deadline_expired" or self._deadline_closed:
                    try:
                        await self._answer(
                            channel, self._transfer_answer(request.request_id, error)
                        )
                    finally:
                        channel.close()
                    return
                await read_transfer_bytes(channel, self._clock, item.size, deadline)
                await self._answer(
                    channel, self._transfer_answer(request.request_id, error)
                )
                return
            try:
                if decision == "write":
                    await self._store_transfer(
                        channel, command, item, deadline, authorize
                    )
                else:
                    digest = await read_transfer_bytes(
                        channel, self._clock, item.size, deadline
                    )

                    def complete() -> None:
                        authorize()
                        command.complete_seen(decision, item, digest)

                    await self._transfer_native(complete)
                await self._transfer_native(authorize)
            except KoraProtocolError as error:
                await self._answer(
                    channel, self._transfer_answer(request.request_id, error)
                )
                return
            await channel.write_typed_frame(
                FrameType.JSON,
                delivery_envelope(request.request_id, "transferred"),
                min(deadline, self._clock.monotonic_ns() + FRAME_PROGRESS_NS),
            )
        except NativeIONotInvoked:
            channel.close()
        except NativeIOUnresolved:
            claimed = False
            channel.close()
        except ChannelError:
            if cutoff is not None and self._clock.monotonic_ns() >= cutoff:
                await self._transfer_native(lambda: self._authorized(submitter, cutoff))
            raise
        finally:
            if command is not None and claimed:
                command.end_transfer()

    async def _send_output(
        self,
        channel: BoundedChannel,
        submitter: Submitter,
        observational: bool,
        request: OutputRequest,
    ) -> None:
        started_ns = self._clock.monotonic_ns()
        command = self._command
        opened: OpenedOutput | None = None
        outcome: str | None = None
        owned = True

        def dispose() -> None:
            nonlocal opened
            if opened is not None:
                current, opened = opened, None
                os.close(current.descriptor)

        try:
            try:
                if command is None:
                    raise delivery_error("transfer_refused")
                if observational:
                    raise delivery_error("execution_unauthorized")
                session = command
                deadline = started_ns + MESSAGE_PROGRESS_NS
                cutoff: int | None = None
                captured = False

                def active() -> None:
                    if self._closing is not None:
                        raise delivery_error("transfer_refused")
                    if self._clock.monotonic_ns() >= deadline:
                        raise ChannelError(ChannelFailure.DEADLINE)

                def authorize() -> None:
                    nonlocal cutoff, captured
                    if not captured:
                        entry = self._running()
                        cutoff = transfer_cutoff(self._clock, entry.deadline)
                        captured = True
                    self._authorized(submitter, cutoff)
                    active()

                def open_file() -> OpenedOutput:
                    nonlocal opened
                    authorize()
                    opened = session.open_output(submitter, request.name)
                    return opened

                current = await self._transfer_native(open_file, dispose)
                await self._transfer_native(
                    lambda: session.confirm_output(current, active),
                    dispose,
                )
                await self._transfer_native(authorize, dispose)
                await channel.write_typed_frame(
                    FrameType.JSON,
                    output_envelope(request.request_id, current.size, current.sha256),
                    min(deadline, self._clock.monotonic_ns() + FRAME_PROGRESS_NS),
                )
                offset = 0
                while offset < current.size:
                    length = min(SECTION_FRAME_BYTES, current.size - offset)
                    position = offset

                    def read(position: int = position, length: int = length) -> bytes:
                        authorize()
                        piece = session.read_output(current, position, length)
                        authorize()
                        return piece

                    chunk = await self._transfer_native(read, dispose)
                    await channel.write_typed_frame(
                        FrameType.BYTES,
                        chunk,
                        min(deadline, self._clock.monotonic_ns() + FRAME_PROGRESS_NS),
                    )
                    offset += len(chunk)

                def finish() -> None:
                    authorize()
                    session.read_output(current, current.size, 0)
                    dispose()

                await self._transfer_native(finish, dispose)
                await channel.write_typed_frame(
                    FrameType.JSON,
                    output_trailer(request.request_id, current.sha256),
                    min(deadline, self._clock.monotonic_ns() + FRAME_PROGRESS_NS),
                )
                outcome = "transferred"
            except KoraProtocolError as error:
                if error.code == "registry_busy":
                    error = delivery_error("workload_busy")
                outcome = "integrity" if error.code == "integrity_error" else "refused"
                await self._answer(
                    channel, self._transfer_answer(request.request_id, error)
                )
        except NativeIOSubmissionUncertain:
            owned = False
            channel.close()
        except NativeIOUnresolved:
            channel.close()
        finally:
            if owned:
                dispose()
            if command is not None and outcome is not None:
                command.note_output(outcome, started_ns)

    async def _transfer_native(
        self,
        operation: Callable[[], Result],
        dispose: Callable[[], None] | None = None,
    ) -> Result:
        def settled() -> None:
            self._transfer_native_settled = True
            if dispose is not None:
                dispose()

        return await self._executor.run(operation, after_uncertain_submission=settled)

    def _transfer_io(
        self, command: CommandSession, operation: Callable[[], Result]
    ) -> Result:
        try:
            return operation()
        except FilesystemError:
            raise delivery_error("transfer_refused") from None
        except OSError as error:
            if space_error(error):
                command.fence_transfer()
                raise delivery_error("journal_unavailable") from None
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise delivery_error("transfer_refused") from None
            raise

    async def _store_transfer(
        self,
        channel: BoundedChannel,
        command: CommandSession,
        item: TransferItem,
        deadline: int,
        authorize: Callable[[], None],
    ) -> None:
        incoming: Incoming | None = None
        caller_owned = True

        def open_incoming() -> None:
            nonlocal incoming
            authorize()
            incoming = self._transfer_io(command, command.open_incoming)

        def dispose() -> None:
            nonlocal incoming
            if incoming is not None:
                owned, incoming = incoming, None
                self._transfer_io(command, lambda: command.discard_incoming(owned))

        async def run(operation: Callable[[], None]) -> None:
            await self._transfer_native(operation, dispose)

        try:
            try:
                await run(open_incoming)
            except KoraProtocolError:
                await read_transfer_bytes(channel, self._clock, item.size, deadline)
                raise
            opened = incoming
            if opened is None:
                raise RuntimeError("missing incoming descriptor")

            async def write(piece: memoryview) -> None:
                def write_piece() -> None:
                    if self._clock.monotonic_ns() >= deadline:
                        raise ChannelError(ChannelFailure.DEADLINE)
                    write_transfer_bytes(opened.descriptor, piece)

                await run(partial(self._transfer_io, command, write_piece))

            digest = await read_transfer_bytes(
                channel, self._clock, item.size, deadline, write
            )
            await run(
                lambda: self._transfer_io(
                    command,
                    lambda: command.finish_incoming(opened, item, digest, authorize),
                )
            )
        except NativeIOSubmissionUncertain:
            caller_owned = False
            raise
        finally:
            if caller_owned and incoming is not None:
                try:
                    await run(dispose)
                except (NativeIONotInvoked, KoraProtocolError) as error:
                    if incoming is None or (
                        isinstance(error, KoraProtocolError)
                        and error.code not in {"registry_busy", "runtime_closed"}
                    ):
                        raise
                    try:
                        cleanup = asyncio.get_running_loop().run_in_executor(
                            None, dispose
                        )
                    except (RuntimeError, MemoryError):
                        self._unresolved_disposal = dispose
                        raise NativeIOUnresolved from None
                    cancelled = await observed(cleanup)
                    cleanup.result()
                    if cancelled:
                        raise asyncio.CancelledError from None

    async def _start(
        self, submitter: Submitter, request_id: str, observational: bool
    ) -> bytes:
        command = self._command
        if command is None:
            return error_envelope(
                request_id, delivery_error("workload_kind_unavailable")
            )
        try:
            await self._executor.run(
                lambda: command.begin(
                    submitter,
                    observational,
                    self._workload,
                    lambda: self._authorized(submitter),
                )
            )
        except KoraProtocolError as error:
            if error.code not in ENVELOPE_ERRORS:
                raise
            return error_envelope(request_id, error)
        finally:
            command.attach_drains()
        return delivery_envelope(request_id, "started")

    def _observed(self) -> StatusValue:
        command = self._command
        if command is None:
            state, phase, code = "starting", "preparation", None
        else:
            state, phase, code = command.status()
        status = RuntimeStatus(
            state=state,
            exit_code=code,
            phase=phase,
            observed_at=self._clock.now(),
        )
        return StatusValue(status=status, holds_workload=self._workload is not None)

    async def _tick(self) -> None:
        while True:
            await self._clock.sleep_until(self._clock.monotonic_ns() + REVERIFY_NS)
            self._flush()
            await self._reverify()

    async def _reverify(self) -> None:
        bound = self._bound
        name = self._name
        path = self._path
        if await self._executor.run(lambda: unchanged_entry(bound, path, name)):
            return
        try:
            fresh = await self._executor.run(
                lambda: self._binder(path, name), BoundSocket.close
            )
        except (OSError, FilesystemError):
            self._log.emit(SOCKET_RECHECKED, outcome=Rebind.REFUSED.value)
            return
        if self._closing is not None:
            fresh.close()
            return
        await self._swap(fresh)
        self._log.emit(SOCKET_RECHECKED, outcome=Rebind.REBOUND.value)

    async def _swap(self, fresh: BoundSocket) -> None:
        accepting = self._accepting
        old = self._bound
        self._bound = fresh
        self._accepting = asyncio.create_task(self._accept(fresh.listener))
        try:
            if accepting is not None:
                await asyncio.shield(cancel_task(accepting))
        finally:
            old.close()

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._release())
        cancelled = False
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError:
                if self._closing.cancelled():
                    raise
                cancelled = True
        self._closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _release(self) -> None:
        self._bound.listener.close()
        if self._command is not None:
            self._command.close_journal()
        tasks = [*self._connections]
        if self._accepting is not None:
            tasks.append(self._accepting)
        if self._ticking is not None:
            tasks.append(self._ticking)
        try:
            for task in tasks:
                task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    raise outcome
        finally:
            settled = False
            workers_joined = False
            try:
                await self._executor.close()
            except NativeIOUnresolved:
                workers_joined = True
                raise
            else:
                settled = True
                workers_joined = True
            finally:
                try:
                    if self._command is not None and (
                        workers_joined or self._transfer_native_settled
                    ):
                        await self._command.aclose()
                finally:
                    if (
                        settled or self._transfer_native_settled
                    ) and self._unresolved_disposal is None:
                        self._resources_settled = True
                        self._workload = None
                        self._workload_sha256 = None
                        self._bound.directory.close()
                        self._flush()
        if self._unresolved_disposal is not None:
            raise NativeIOUnresolved
