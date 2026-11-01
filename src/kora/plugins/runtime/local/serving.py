import asyncio
import errno
import os
import socket
import stat
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Final

from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.channel import (
    ACCEPT_WORKLOAD,
    ENVELOPE_ERRORS,
    FRAME_PROGRESS_NS,
    MESSAGE_PROGRESS_NS,
    PRE_ATTACH_BYTES,
    AttachRequest,
    FrameType,
    StatusValue,
    decode_section,
    delivery_envelope,
    delivery_error,
    error_envelope,
    operation_unknown,
    parse_header,
    read_request,
    receive_section,
    status_envelope,
    workload_digest,
)
from kora.plugins.runtime.local.spawn import open_socket_directory
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import ProcessIdentity
from kora.protocols.runtime import RuntimeStatus, Workload
from kora.protocols.time import Clock
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    ChannelFailure,
    cancel_task,
)
from kora.runner.filesystem import DirectoryAnchor, FilesystemError
from kora.runner.identity import IdentityReader
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
        self.listener.close()
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
    ) -> None:
        self._bound = bound
        self._path = path
        self._name = name
        self._clock = clock
        self._source = source
        self._log = log
        self._binder = binder
        self._decoder = decoder
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
        self._refusals: Counter[str] = Counter()
        self._accepting: asyncio.Task[None] | None = None
        self._ticking: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None

    @property
    def connections(self) -> int:
        return len(self._connections)

    def start(self) -> None:
        self._accepting = asyncio.create_task(self._accept(self._bound.listener))
        self._ticking = asyncio.create_task(self._tick())

    async def _accept(self, listener: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            endpoint, _ = await loop.sock_accept(listener)
            stamp = self._source.stamp()
            if len(self._connections) >= CONNECTIONS:
                endpoint.close()
                continue
            deadline = self._clock.monotonic_ns() + FIRST_FRAME_NS
            task = asyncio.create_task(self._serve(endpoint, stamp, deadline))
            self._connections.add(task)
            task.add_done_callback(self._connections.discard)

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
                if head.submitter != submitter:
                    raise AuthenticationRefused(Refusal.NOT_SUBMITTER)
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
                await self._attached(channel, submitter)
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
                endpoint.close()
                if observer:
                    self._observers -= 1

    def _running(self) -> Running:
        entry = self._registry.entry(self._key)
        if not isinstance(entry, Running) or entry.runner != self._identity:
            raise AuthenticationRefused(Refusal.HEAD_CHANGED)
        return entry

    def _authorized(self, submitter: Submitter) -> Running:
        entry = self._running()
        if entry.submitter != submitter:
            raise delivery_error("submitter_superseded")
        unchanged(self._identities, submitter.process)
        deadline = entry.deadline
        if (
            entry.gate_closed
            or self._deadline_closed
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

    async def _attached(self, channel: BoundedChannel, submitter: Submitter) -> None:
        while True:
            started = self._clock.monotonic_ns()
            _, payload = await channel.read_typed_frame(
                started + FRAME_PROGRESS_NS, {FrameType.JSON.value: PRE_ATTACH_BYTES}
            )
            request = read_request(payload)
            if request is not None:
                answer = (
                    status_envelope(request.request_id, self._observed())
                    if request.op == "status"
                    else error_envelope(request.request_id, operation_unknown())
                )
                await self._answer(channel, answer)
                continue
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
                            self._workload = decoded
                            self._workload_sha256 = declaration.sha256
                    answer = delivery_envelope(header.request_id, "accepted")
                except KoraProtocolError as error:
                    if error.code not in ENVELOPE_ERRORS:
                        raise
                    answer = error_envelope(header.request_id, error)
                await self._answer(channel, answer)
            finally:
                section = bytearray()
                decoded = None
                if not sink:
                    self._admitted = False

    def _observed(self) -> StatusValue:
        status = RuntimeStatus(
            state="starting",
            exit_code=None,
            phase="preparation",
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
        if self._accepting is not None:
            await cancel_task(self._accepting)
        if self._ticking is not None:
            await cancel_task(self._ticking)
        for connection in tuple(self._connections):
            await cancel_task(connection)
        await self._executor.close()
        self._workload = None
        self._workload_sha256 = None
        self._bound.directory.close()
        self._flush()
