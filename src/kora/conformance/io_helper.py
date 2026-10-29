import array
import asyncio
import fcntl
import os
import select
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field, StrictInt, TypeAdapter, ValidationError

from kora.conformance.journal import JournalSnapshot
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.lifetime_records import InvocationId, LifetimeReason, Revision
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import (
    BoundedChannel,
    ChannelError,
    DescriptorReadiness,
    wait_future,
)
from kora.runner.filesystem import (
    DirectoryAnchor,
    DirectoryIdentity,
    FileCapacityError,
    FilesystemError,
    create_directory,
    file_identity,
    identity_of,
    open_private_file,
    read_private_file,
    remove_directory,
    verify_private,
)
from kora.runner.identity import IdentityState, NativeIdentityReader, complete_identity


class BootstrapMutation(BoundaryModel):
    action: Literal["bootstrap"] = "bootstrap"
    invocation_id: InvocationId
    journal: JournalSnapshot
    capacity: Annotated[StrictInt, Field(gt=0, le=32)]


class RecoverMutation(BoundaryModel):
    action: Literal["recover"] = "recover"
    invocation_id: InvocationId
    owner: ProcessIdentity
    expected_root: DirectoryIdentity | None = None


class PublishMutation(BoundaryModel):
    action: Literal["publish"] = "publish"
    directory: DirectoryIdentity
    revision: Revision
    journal: JournalSnapshot


class CreateTreeMutation(BoundaryModel):
    action: Literal["create_tree"] = "create_tree"
    directory: DirectoryIdentity
    name: InvocationId


class OpenTreeMutation(BoundaryModel):
    action: Literal["open_tree"] = "open_tree"
    directory: DirectoryIdentity
    target: DirectoryIdentity


class RemoveTreeMutation(BoundaryModel):
    action: Literal["remove_tree"] = "remove_tree"
    directory: DirectoryIdentity
    target: DirectoryIdentity


class CreateListenersMutation(BoundaryModel):
    action: Literal["create_listeners"] = "create_listeners"
    directory: DirectoryIdentity


Mutation = Annotated[
    BootstrapMutation
    | RecoverMutation
    | PublishMutation
    | CreateTreeMutation
    | OpenTreeMutation
    | RemoveTreeMutation
    | CreateListenersMutation,
    Field(discriminator="action"),
]
MUTATION: TypeAdapter[Mutation] = TypeAdapter(Mutation)


class MutationResult(BoundaryModel):
    reason: LifetimeReason
    directory: DirectoryIdentity | None = None
    root: DirectoryIdentity | None = None
    descriptor_count: Annotated[StrictInt, Field(ge=0, le=3)] = 0
    journal: JournalSnapshot | None = None


class HelperError(Exception):
    def __init__(self, reason: LifetimeReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class ChildWatch(Protocol):
    @property
    def changed(self) -> asyncio.Future[None]: ...

    def close(self) -> None: ...


class ChildReaper(Protocol):
    def reap(self, pid: int) -> int | None: ...

    def watch(self, pid: int) -> ChildWatch: ...


class KernelChildWatch:
    def __init__(self, pid: int) -> None:
        self._loop = asyncio.get_running_loop()
        self._descriptor, self._release = child_change_descriptor(pid)
        self._changed: asyncio.Future[None] = self._loop.create_future()
        try:
            self._loop.add_reader(self._descriptor, self._notify)
        except OSError:
            self._release()
            raise
        self._reading = True
        self._closed = False

    @property
    def changed(self) -> asyncio.Future[None]:
        return self._changed

    def _notify(self) -> None:
        self._stop_reading()
        if not self._changed.done():
            self._changed.set_result(None)

    def _stop_reading(self) -> None:
        if self._reading:
            self._loop.remove_reader(self._descriptor)
            self._reading = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_reading()
        self._release()
        self._changed.cancel()


class KernelChildReaper:
    def reap(self, pid: int) -> int | None:
        reaped, status = os.waitpid(pid, os.WNOHANG)
        return os.waitstatus_to_exitcode(status) if reaped == pid else None

    def watch(self, pid: int) -> ChildWatch:
        return KernelChildWatch(pid)


def child_change_descriptor(pid: int) -> tuple[int, Callable[[], None]]:
    if sys.platform != "darwin":
        descriptor = os.pidfd_open(pid)
        return descriptor, lambda: os.close(descriptor)
    queue = select.kqueue()
    try:
        queue.control(
            [
                select.kevent(
                    signal.SIGCHLD,
                    filter=select.KQ_FILTER_SIGNAL,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                )
            ],
            0,
            0,
        )
    except OSError:
        queue.close()
        raise
    return queue.fileno(), queue.close


class IOHelper:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        endpoint: socket.socket,
        clock: Clock,
        reaper: ChildReaper | None = None,
    ) -> None:
        from kora.runner.processes import ExitAnchor

        self.process = process
        self.anchor = ExitAnchor(process.pid)
        self.channel = BoundedChannel(endpoint, clock)
        self.endpoint = endpoint
        self.clock = clock
        self.reaper = reaper if reaper is not None else KernelChildReaper()
        self._reap_watch: ChildWatch | None = None
        self.identities = NativeIdentityReader(clock=clock)
        observed = self.identities.read(process.pid)
        self.identity: ProcessIdentity | None = (
            observed.process.identity
            if observed.state is IdentityState.PRESENT and observed.process is not None
            else None
        )
        self.closed = False
        self.descriptors: tuple[int, ...] = ()
        self.result: MutationResult | None = None

    async def execute(self, mutation: Mutation, deadline_ns: int) -> MutationResult:
        await self.channel.write_record(MUTATION.dump_json(mutation), deadline_ns)
        result = MutationResult.model_validate_json(
            await self.channel.read_record(deadline_ns, 1048576)
        )
        if result.descriptor_count:
            await self.receive_descriptors(result.descriptor_count, deadline_ns)
            if isinstance(mutation, CreateListenersMutation):
                await self.channel.write_frame(b"listeners_received", deadline_ns)
        await self.reap(deadline_ns)
        if self.process.returncode != 0:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        if result.reason is not LifetimeReason.COMPLETE:
            raise HelperError(result.reason)
        self.result = result
        return result

    async def receive_descriptors(self, count: int, deadline_ns: int) -> None:
        while True:
            try:
                payload, messages, flags, _ = self.endpoint.recvmsg(
                    1, socket.CMSG_SPACE(count * array.array("i").itemsize)
                )
            except BlockingIOError:
                readiness = DescriptorReadiness(self.endpoint.fileno(), writing=False)
                try:
                    await wait_future(readiness.ready, deadline_ns, self.clock)
                finally:
                    readiness.close()
                continue
            received: list[int] = []
            for level, kind, values in messages:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    descriptors = array.array("i")
                    descriptors.frombytes(values)
                    received.extend(descriptors)
            self.descriptors = tuple(received)
            for descriptor in received:
                os.set_inheritable(descriptor, False)
            if payload != b"d" or flags & socket.MSG_CTRUNC or len(received) != count:
                raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
            return

    def signal(self, requested: signal.Signals, deadline_ns: int | None = None) -> bool:
        if requested not in (signal.SIGTERM, signal.SIGKILL):
            raise ValueError("fixed helper signal required")
        if self.anchor.exited:
            return True
        if deadline_ns is not None and self.clock.monotonic_ns() >= deadline_ns:
            return False
        observation = self.identities.read(self.process.pid)
        if (
            (deadline_ns is not None and self.clock.monotonic_ns() >= deadline_ns)
            or self.identity is None
            or not complete_identity(self.identity)
            or observation.state is not IdentityState.PRESENT
            or observation.process is None
            or observation.process.identity != self.identity
        ):
            return False
        try:
            os.kill(self.process.pid, requested)
        except ProcessLookupError:
            return self.anchor.exited
        return True

    async def settle(self, deadline_ns: int) -> bool:
        from kora.runner.processes import ProcessError

        try:
            await self.reap(deadline_ns)
        except (ChannelError, ProcessError):
            return False
        return True

    async def reap(self, deadline_ns: int) -> None:
        await self.anchor.wait(deadline_ns, self.clock)
        while (change := self.reap_change()) is not None:
            await wait_future(change, deadline_ns, self.clock)

    def reap_change(self) -> asyncio.Future[None] | None:
        while not self.release():
            watch = self._reap_watch
            if watch is None or watch.changed.done():
                if watch is not None:
                    watch.close()
                watch = self.reaper.watch(self.process.pid)
                self._reap_watch = watch
            if self.release():
                return None
            if not watch.changed.done():
                return watch.changed
        return None

    def release(self) -> bool:
        if self.closed:
            return True
        if not self.anchor.exited or self.anchor.unknown:
            return False
        returncode = self.reaper.reap(self.process.pid)
        if returncode is None:
            return False
        self.process.returncode = returncode
        if self._reap_watch is not None:
            self._reap_watch.close()
            self._reap_watch = None
        self.channel.close()
        self.anchor.close()
        self.closed = True
        return True

    def take_descriptors(self) -> tuple[int, ...]:
        descriptors = self.descriptors
        self.descriptors = ()
        return descriptors


class IOHelperLauncher(Protocol):
    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper: ...


class InstalledIOHelperLauncher:
    def __init__(
        self,
        *,
        home: Path | None = None,
        state_home: Path | None = None,
        reaper: ChildReaper | None = None,
    ) -> None:
        self.reaper = reaper if reaper is not None else KernelChildReaper()
        self.home = str(home) if home is not None else os.environ["HOME"]
        self.state_home = (
            str(state_home)
            if state_home is not None
            else os.environ.get("XDG_STATE_HOME", "")
        )

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        parent, child = socket.socketpair()
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-m", "kora.conformance.io_helper"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "HOME": self.home,
                    "XDG_STATE_HOME": self.state_home,
                    "KORA_IO_CONTROL_FD": str(child.fileno()),
                    "KORA_IO_DESCRIPTORS": ",".join(
                        str(value) for value in descriptors
                    ),
                },
                close_fds=True,
                pass_fds=(child.fileno(), *descriptors),
                process_group=0,
            )
        except OSError:
            parent.close()
            raise
        finally:
            child.close()
        return IOHelper(process, parent, clock, self.reaper)


def state_directory() -> Path:
    home = Path(os.environ["HOME"])
    if not home.is_absolute():
        raise FilesystemError("absolute_home_required")
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "kora" / "conformance"
    if sys.platform == "linux":
        configured = os.environ.get("XDG_STATE_HOME", "")
        base = Path(configured) if configured else home / ".local" / "state"
        if not base.is_absolute():
            raise FilesystemError("absolute_state_root_required")
        return base / "kora" / "conformance"
    raise FilesystemError("unsupported_platform")


def establish_state_root() -> DirectoryAnchor:
    path = state_directory()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    ancestors: list[DirectoryIdentity] = []
    try:
        for component in path.parts[1:]:
            try:
                following = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                following = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = following
            metadata = os.fstat(descriptor)
            if metadata.st_uid not in (0, os.getuid()):
                raise FilesystemError("foreign_ancestor")
            ancestors.append(identity_of(descriptor, component))
        verify_private(descriptor, directory=True)
        return DirectoryAnchor(
            descriptor, identity_of(descriptor, path.name), tuple(ancestors)
        )
    except (OSError, FilesystemError):
        os.close(descriptor)
        raise


def open_claim(directory: DirectoryAnchor, name: str) -> int:
    descriptor = open_private_file(directory, name, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def publish(directory: DirectoryAnchor, journal: JournalSnapshot) -> None:
    directory.verify()
    observed = NativeIdentityReader().read(os.getpid())
    if observed.process is None or not complete_identity(observed.process.identity):
        raise HelperError(LifetimeReason.IDENTITY_UNKNOWN)
    journal = JournalSnapshot.model_validate(
        {**journal.model_dump(), **{"helpers": (observed.process.identity,)}}
    )
    encoded = journal.model_dump_json().encode("utf-8")
    if len(encoded) > 1048576:
        raise HelperError(LifetimeReason.CAPACITY_EXCEEDED)
    descriptor = os.open(
        "next",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory.descriptor,
    )
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        "next",
        "journal",
        src_dir_fd=directory.descriptor,
        dst_dir_fd=directory.descriptor,
    )
    os.fsync(directory.descriptor)


def bootstrap(mutation: BootstrapMutation) -> tuple[MutationResult, tuple[int, ...]]:
    if mutation.invocation_id != mutation.journal.invocation_id:
        raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
    root = establish_state_root()
    admission = open_claim(root, ".admission")
    try:
        count = 0
        with os.scandir(root.descriptor) as entries:
            for entry in entries:
                if entry.name == ".admission":
                    continue
                count += 1
                if count >= mutation.capacity:
                    raise HelperError(LifetimeReason.CAPACITY_EXCEEDED)
        invocation = create_directory(root, mutation.invocation_id)
        claim = open_claim(invocation, "claim")
        snapshot = JournalSnapshot.model_validate(
            {
                **mutation.journal.model_dump(),
                **{
                    "root": root.identity,
                    "directory": invocation.identity,
                    "ancestors": root.ancestors,
                    "claim": file_identity(claim, "claim"),
                },
            }
        )
        publish(invocation, snapshot)
        snapshot = read_journal(invocation)
        result = MutationResult(
            reason=LifetimeReason.COMPLETE,
            directory=invocation.identity,
            root=root.identity,
            descriptor_count=3,
            journal=snapshot,
        )
        return result, (root.descriptor, invocation.descriptor, claim)
    finally:
        os.close(admission)


def discard_recovery_staging(directory: DirectoryAnchor) -> None:
    directory.verify()
    try:
        descriptor = os.open(
            "next",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory.descriptor,
        )
    except FileNotFoundError:
        return
    try:
        expected = file_identity(descriptor, "next")
        directory.verify()
        current = os.stat("next", dir_fd=directory.descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != expected.device
            or current.st_ino != expected.inode
            or current.st_uid != expected.owner_uid
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
        ):
            raise HelperError(LifetimeReason.IDENTITY_MISMATCH)
        verify_private(descriptor, directory=False)
        os.unlink("next", dir_fd=directory.descriptor)
        os.fsync(directory.descriptor)
    finally:
        os.close(descriptor)


def recover(mutation: RecoverMutation) -> tuple[MutationResult, tuple[int, ...]]:
    root = DirectoryAnchor.open_path(state_directory())
    if mutation.expected_root is not None and root.identity != mutation.expected_root:
        root.close()
        raise HelperError(LifetimeReason.IDENTITY_MISMATCH)
    invocation_fd = os.open(
        mutation.invocation_id,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root.descriptor,
    )
    invocation = DirectoryAnchor(
        invocation_fd, identity_of(invocation_fd, mutation.invocation_id)
    )
    claim = os.open(
        "claim", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=invocation_fd
    )
    verify_private(claim, directory=False)
    fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
    snapshot = read_journal(invocation)
    if (
        snapshot.invocation_id != mutation.invocation_id
        or snapshot.directory != invocation.identity
        or snapshot.root != root.identity
        or snapshot.ancestors != root.ancestors
        or snapshot.claim != file_identity(claim, "claim")
    ):
        raise HelperError(LifetimeReason.IDENTITY_MISMATCH)
    identities = NativeIdentityReader()
    current = identities.read(os.getpid())
    if (
        current.state is not IdentityState.PRESENT
        or current.process is None
        or not complete_identity(current.process.identity)
    ):
        raise HelperError(LifetimeReason.IDENTITY_UNKNOWN)
    scope = current.process.identity
    for identity in (snapshot.owner, *snapshot.helpers):
        if not complete_identity(identity):
            raise HelperError(LifetimeReason.IDENTITY_UNKNOWN)
        if (
            identity.host_id != scope.host_id
            or identity.boot_id != scope.boot_id
            or identity.pid_namespace != scope.pid_namespace
        ):
            raise HelperError(LifetimeReason.IDENTITY_MISMATCH)
        observed = identities.read(identity.pid)
        if observed.state is IdentityState.UNKNOWN:
            raise HelperError(LifetimeReason.IDENTITY_UNKNOWN)
        if observed.process is not None:
            if observed.process.identity != identity:
                raise HelperError(LifetimeReason.IDENTITY_MISMATCH)
            if not observed.process.dead:
                raise HelperError(LifetimeReason.OWNER_BUSY)
    if snapshot.pending_effects:
        raise HelperError(LifetimeReason.EFFECT_UNSETTLED)
    if snapshot.generation >= 2**64 - 1 or snapshot.revision >= 2**64 - 1:
        raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
    discard_recovery_staging(invocation)
    snapshot = JournalSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            **{
                "owner": mutation.owner,
                "generation": snapshot.generation + 1,
                "revision": snapshot.revision + 1,
                "admission_open": False,
            },
        }
    )
    publish(invocation, snapshot)
    snapshot = read_journal(invocation)
    return MutationResult(
        reason=LifetimeReason.COMPLETE,
        directory=invocation.identity,
        root=root.identity,
        descriptor_count=3,
        journal=snapshot,
    ), (root.descriptor, invocation_fd, claim)


def mutate(
    mutation: Mutation, descriptors: tuple[int, ...]
) -> tuple[MutationResult, tuple[int, ...]]:
    if isinstance(mutation, RecoverMutation):
        if descriptors:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        return recover(mutation)
    if isinstance(mutation, BootstrapMutation):
        if descriptors:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        return bootstrap(mutation)
    if len(descriptors) != 2:
        raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
    directory = DirectoryAnchor(descriptors[0], mutation.directory)
    directory.verify()
    verify_private(descriptors[1], directory=False)
    fcntl.flock(descriptors[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
    if isinstance(mutation, PublishMutation):
        previous = read_journal(directory)
        if (
            mutation.revision != mutation.journal.revision
            or mutation.journal.revision <= previous.revision
            or mutation.journal.owner != previous.owner
            or mutation.journal.generation != previous.generation
            or mutation.journal.invocation_id != previous.invocation_id
            or mutation.journal.directory != previous.directory
            or mutation.journal.root != previous.root
            or mutation.journal.ancestors != previous.ancestors
            or mutation.journal.claim != previous.claim
        ):
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        publish(directory, mutation.journal)
        return MutationResult(
            reason=LifetimeReason.COMPLETE, journal=read_journal(directory)
        ), ()
    if isinstance(mutation, CreateTreeMutation):
        child = create_directory(directory, mutation.name)
        return MutationResult(
            reason=LifetimeReason.COMPLETE,
            directory=child.identity,
            descriptor_count=1,
        ), (child.descriptor,)
    if isinstance(mutation, OpenTreeMutation):
        child = directory.open_child(mutation.target)
        return MutationResult(
            reason=LifetimeReason.COMPLETE,
            directory=child.identity,
            descriptor_count=1,
        ), (child.descriptor,)
    if isinstance(mutation, CreateListenersMutation):
        os.fchdir(directory.descriptor)
        with ExitStack() as resources:
            listeners: list[socket.socket] = []
            for name in ("r", "e", "l"):
                listener = resources.enter_context(
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                )
                listener.bind(name)
                listener.listen(1)
                listeners.append(listener)
            return MutationResult(
                reason=LifetimeReason.COMPLETE,
                descriptor_count=3,
            ), tuple(listener.detach() for listener in listeners)
    remove_directory(directory, mutation.target)
    return MutationResult(reason=LifetimeReason.COMPLETE), ()


async def respond_mutation(
    endpoint: socket.socket,
    channel: BoundedChannel,
    mutation: Mutation,
    descriptors: tuple[int, ...],
    deadline: int,
) -> None:
    returned: tuple[int, ...] = ()
    try:
        try:
            result, returned = mutate(mutation, descriptors)
        except BlockingIOError:
            result = MutationResult(reason=LifetimeReason.OWNER_BUSY)
        except HelperError as error:
            result = MutationResult(reason=error.reason)
        except (OSError, FilesystemError, ValidationError):
            result = MutationResult(reason=LifetimeReason.JOURNAL_UNAVAILABLE)
        await channel.write_record(result.model_dump_json().encode(), deadline)
        if returned:
            endpoint.setblocking(True)
            endpoint.sendmsg(
                [b"d"],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", returned))],
            )
            if isinstance(mutation, CreateListenersMutation):
                endpoint.setblocking(False)
                if await channel.read_frame(deadline, 32) != b"listeners_received":
                    raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
    finally:
        releasable = (
            returned[:-1]
            if isinstance(mutation, (BootstrapMutation, RecoverMutation))
            else returned
        )
        for descriptor in releasable:
            os.close(descriptor)


async def serve_mutation(endpoint: socket.socket, descriptors: tuple[int, ...]) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 30_000_000_000
    channel = BoundedChannel(endpoint, clock)
    try:
        mutation = MUTATION.validate_json(await channel.read_record(deadline, 1048576))
        await respond_mutation(endpoint, channel, mutation, descriptors, deadline)
    finally:
        channel.close()


def read_journal(directory: DirectoryAnchor) -> JournalSnapshot:
    directory.verify()
    try:
        encoded = read_private_file(directory, "journal", 1048576)
    except FileCapacityError:
        raise HelperError(LifetimeReason.CAPACITY_EXCEEDED) from None
    return JournalSnapshot.model_validate_json(encoded)


def main() -> int:
    endpoint = socket.socket(fileno=int(os.environ["KORA_IO_CONTROL_FD"]))
    descriptors = tuple(
        int(value) for value in os.environ["KORA_IO_DESCRIPTORS"].split(",") if value
    )
    try:
        asyncio.run(serve_mutation(endpoint, descriptors))
    except (ChannelError, ValidationError, OSError, FilesystemError):
        return 1
    finally:
        endpoint.close()
        for descriptor in descriptors[:-1]:
            os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
