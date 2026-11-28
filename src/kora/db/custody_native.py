from __future__ import annotations

import fcntl
import os
import stat
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from enum import Enum
from pathlib import Path
from typing import Final, Literal, Protocol

from kora.db.custody_models import (
    CustodyFileAnchor,
    CustodyProcessIdentity,
    CustodyUnavailable,
    encode_native_identifier,
)

CustodyLockName = Literal[
    "lifetime.lock", "commit.lock", "recovery.lock", "journal-order.lock"
]
CustodyWaitName = Literal["commit.lock", "journal-order.lock"]
LOCK_NAMES: Final[tuple[CustodyLockName, ...]] = (
    "lifetime.lock",
    "commit.lock",
    "recovery.lock",
    "journal-order.lock",
)
FILE_NAMES: Final[tuple[str, ...]] = ("authority.sqlite3", *LOCK_NAMES)
DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS: Final[int] = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
FORK_GUARD: Final[threading.RLock] = threading.RLock()
FORK_CONDITION: Final[threading.Condition] = threading.Condition(FORK_GUARD)
DESCRIPTORS: Final[set[CustodyDescriptor]] = set()
_active_native_jobs: int = 0
_fork_pending: bool = False
_fork_registered: bool = False


class CustodyLatch(Enum):
    OPEN = b"\xa5"
    ARMED = b"\x5a"
    BOOTSTRAP_FROM_OPEN = b"\x3c"


class NativeThreadState(threading.local):
    depth: int

    def __init__(self) -> None:
        self.depth = 0


NATIVE_THREAD_STATE: Final[NativeThreadState] = NativeThreadState()


class CustodyNativePlatform(Protocol):
    def current_process(self) -> CustodyProcessIdentity: ...

    def require_process_absent(self, previous: CustodyProcessIdentity) -> None: ...

    def inspect_filesystem(self, descriptor: int) -> tuple[str, str]: ...

    def require_private_acl(self, descriptor: int) -> None: ...

    def persist_file(self, descriptor: int) -> None: ...

    def persist_directory(self, descriptor: int) -> None: ...


class CustodyDescriptor:
    _descriptor: int | None
    _pid: int
    _close_failed: bool

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._pid = os.getpid()
        self._close_failed = False
        with ExitStack() as cleanup:
            cleanup.callback(os.close, descriptor)
            os.set_inheritable(descriptor, False)
            DESCRIPTORS.add(self)
            cleanup.pop_all()

    @property
    def descriptor(self) -> int:
        if self._descriptor is None or self._pid != os.getpid():
            raise CustodyUnavailable
        return self._descriptor

    def close(self) -> None:
        with FORK_GUARD:
            self._close_native()

    def close_in_child(self) -> None:
        if self._pid == os.getpid():
            raise CustodyUnavailable
        self._close_native()

    def _close_native(self) -> None:
        if self._close_failed:
            raise CustodyUnavailable
        if self._descriptor is not None:
            descriptor = self._descriptor
            self._descriptor = None
            try:
                os.close(descriptor)
            except OSError:
                self._close_failed = True
                raise
            DESCRIPTORS.remove(self)


def before_custody_fork() -> None:
    global _fork_pending
    FORK_CONDITION.acquire()
    _fork_pending = True
    while _active_native_jobs:
        FORK_CONDITION.wait()


def after_custody_fork_parent() -> None:
    global _fork_pending
    _fork_pending = False
    FORK_CONDITION.notify_all()
    FORK_CONDITION.release()


def after_custody_fork_child() -> None:
    global _fork_pending
    try:
        with ExitStack() as cleanup:
            for descriptor in tuple(DESCRIPTORS):
                cleanup.callback(descriptor.close_in_child)
    finally:
        _fork_pending = False
        FORK_CONDITION.release()


def register_custody_fork() -> None:
    from kora.sqlite import _native

    global _fork_registered

    with FORK_CONDITION:
        if not _fork_registered:
            _native.initialize_custody_fork()
            os.register_at_fork(
                before=before_custody_fork,
                after_in_parent=after_custody_fork_parent,
                after_in_child=after_custody_fork_child,
            )
            _fork_registered = True


@contextmanager
def custody_native_job(*, resumed_wait: bool = False) -> Generator[None]:
    global _active_native_jobs
    with FORK_CONDITION:
        if resumed_wait:
            FORK_CONDITION.wait_for(lambda: not _fork_pending)
        if _fork_pending and NATIVE_THREAD_STATE.depth == 0:
            raise CustodyUnavailable
        _active_native_jobs += 1
        NATIVE_THREAD_STATE.depth += 1
    try:
        yield
    finally:
        with FORK_CONDITION:
            NATIVE_THREAD_STATE.depth -= 1
            _active_native_jobs -= 1
            if _active_native_jobs == 0:
                FORK_CONDITION.notify_all()


def require_native_file(
    descriptor: int, *, directory: bool, device: int | None = None
) -> os.stat_result:
    value = os.fstat(descriptor)
    require_native_stat(value, directory=directory, device=device)
    return value


def require_native_stat(
    value: os.stat_result, *, directory: bool, device: int | None = None
) -> None:
    if (
        not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != (0o700 if directory else 0o600)
        or (device is not None and value.st_dev != device)
        or (not directory and value.st_nlink != 1)
    ):
        raise CustodyUnavailable


def file_anchor(value: os.stat_result, path: Path) -> CustodyFileAnchor:
    return CustodyFileAnchor(
        encode_native_identifier(value.st_dev),
        encode_native_identifier(value.st_ino),
        os.fsencode(path),
    )


class CustodyNativeDirectory:
    path: Path
    process: CustodyProcessIdentity
    filesystem: tuple[str, str]
    parent_anchor: CustodyFileAnchor
    root_anchor: CustodyFileAnchor
    file_anchors: dict[str, CustodyFileAnchor]
    _platform: CustodyNativePlatform
    _fresh: bool
    _parent: CustodyDescriptor
    _root: CustodyDescriptor
    _files: dict[str, CustodyDescriptor]

    def __init__(
        self,
        path: Path,
        platform: CustodyNativePlatform,
        *,
        provision: bool,
        _isolated_launch: Callable[[], None] | None = None,
    ) -> None:
        self._isolated_launch = _isolated_launch
        if _isolated_launch is not None:
            if not provision or sys.platform != "linux":
                raise CustodyUnavailable
            _isolated_launch()
        with custody_native_job(), ExitStack() as cleanup:
            parent_path = path.parent.resolve(strict=True)
            canonical = parent_path / path.name
            if path.name in {"", ".", ".."}:
                raise CustodyUnavailable
            self.path = canonical
            self._fresh = provision
            self._platform = platform
            self.process = platform.current_process()
            self._parent = CustodyDescriptor(os.open(parent_path, DIRECTORY_FLAGS))
            cleanup.callback(self._parent.close)
            parent = require_native_file(self._parent.descriptor, directory=True)
            platform.require_private_acl(self._parent.descriptor)
            self.parent_anchor = file_anchor(parent, parent_path)
            if provision:
                os.mkdir(path.name, mode=0o700, dir_fd=self._parent.descriptor)
            self._root = CustodyDescriptor(
                os.open(path.name, DIRECTORY_FLAGS, dir_fd=self._parent.descriptor)
            )
            cleanup.callback(self._root.close)
            root = require_native_file(self._root.descriptor, directory=True)
            platform.require_private_acl(self._root.descriptor)
            self.root_anchor = file_anchor(root, canonical)
            self.filesystem = platform.inspect_filesystem(self._root.descriptor)
            if provision:
                with os.scandir(self._root.descriptor) as entries:
                    if next(entries, None) is not None:
                        raise CustodyUnavailable
            self._files = {}
            self.file_anchors = {}
            journal = "authority.sqlite3"
            if provision:
                with ExitStack() as created:
                    handle = CustodyDescriptor(
                        os.open(
                            journal,
                            FILE_FLAGS | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=self._root.descriptor,
                        )
                    )
                    created.callback(handle.close)
                    require_native_file(
                        handle.descriptor, directory=False, device=root.st_dev
                    )
                    platform.require_private_acl(handle.descriptor)
                    platform.persist_file(handle.descriptor)
            named = os.stat(
                journal, dir_fd=self._root.descriptor, follow_symlinks=False
            )
            require_native_stat(named, directory=False, device=root.st_dev)
            self.file_anchors[journal] = file_anchor(named, canonical / journal)
            for name in LOCK_NAMES:
                flags = FILE_FLAGS | (os.O_CREAT | os.O_EXCL if provision else 0)
                handle = CustodyDescriptor(
                    os.open(name, flags, 0o600, dir_fd=self._root.descriptor)
                )
                cleanup.callback(handle.close)
                native = require_native_file(
                    handle.descriptor, directory=False, device=root.st_dev
                )
                platform.require_private_acl(handle.descriptor)
                self._files[name] = handle
                self.file_anchors[name] = file_anchor(native, canonical / name)
                if provision:
                    if name == "recovery.lock":
                        self._write_latch(handle, CustodyLatch.ARMED)
                    else:
                        platform.persist_file(handle.descriptor)
            if provision:
                platform.persist_directory(self._root.descriptor)
                platform.persist_directory(self._parent.descriptor)
            self.read_latch()
            cleanup.pop_all()

    def require_isolated(self) -> None:
        if self._isolated_launch is None or self.read_latch() is not CustodyLatch.ARMED:
            raise CustodyUnavailable

    def require_current(self) -> None:
        if self._isolated_launch is not None:
            self._isolated_launch()
        with custody_native_job():
            if self._platform.current_process() != self.process:
                raise CustodyUnavailable
            parent = require_native_file(self._parent.descriptor, directory=True)
            root = require_native_file(self._root.descriptor, directory=True)
            named_parent = os.stat(self.path.parent, follow_symlinks=False)
            named_root = os.stat(
                self.path.name, dir_fd=self._parent.descriptor, follow_symlinks=False
            )
            if (
                file_anchor(parent, self.path.parent) != self.parent_anchor
                or file_anchor(named_parent, self.path.parent) != self.parent_anchor
                or file_anchor(root, self.path) != self.root_anchor
                or file_anchor(named_root, self.path) != self.root_anchor
                or self._platform.inspect_filesystem(self._root.descriptor)
                != self.filesystem
            ):
                raise CustodyUnavailable
            self._platform.require_private_acl(self._parent.descriptor)
            self._platform.require_private_acl(self._root.descriptor)
            journal = "authority.sqlite3"
            named = os.stat(
                journal, dir_fd=self._root.descriptor, follow_symlinks=False
            )
            require_native_stat(named, directory=False, device=root.st_dev)
            if file_anchor(named, self.path / journal) != self.file_anchors[journal]:
                raise CustodyUnavailable
            for name, handle in self._files.items():
                self.require_file(name, handle.descriptor)

    def require_file(self, name: str, descriptor: int) -> None:
        native = require_native_file(
            descriptor,
            directory=False,
            device=int.from_bytes(self.root_anchor.device, "big"),
        )
        named = os.stat(name, dir_fd=self._root.descriptor, follow_symlinks=False)
        expected = self.file_anchors[name]
        if (
            file_anchor(native, self.path / name) != expected
            or file_anchor(named, self.path / name) != expected
            or not stat.S_ISREG(named.st_mode)
        ):
            raise CustodyUnavailable
        self._platform.require_private_acl(descriptor)

    def require_fresh_provision(self) -> None:
        if not self._fresh or self.read_latch() is not CustodyLatch.ARMED:
            raise CustodyUnavailable

    def _read_latch(self, handle: CustodyDescriptor) -> CustodyLatch:
        self.require_current()
        self.require_file("recovery.lock", handle.descriptor)
        try:
            return CustodyLatch(os.pread(handle.descriptor, 2, 0))
        except (OSError, ValueError):
            raise CustodyUnavailable from None

    def read_latch(self) -> CustodyLatch:
        with custody_native_job():
            return self._read_latch(self._files["recovery.lock"])

    def require_open_latch(self) -> None:
        if self.read_latch() is not CustodyLatch.OPEN:
            raise CustodyUnavailable

    def _write_latch(self, handle: CustodyDescriptor, state: CustodyLatch) -> None:
        if os.pwrite(handle.descriptor, state.value, 0) != 1:
            raise CustodyUnavailable
        self._platform.persist_file(handle.descriptor)

    def transition_latch(
        self, handle: CustodyDescriptor, previous: CustodyLatch, state: CustodyLatch
    ) -> None:
        with custody_native_job():
            if self._read_latch(handle) is not previous:
                raise CustodyUnavailable
            self._write_latch(handle, state)
            if self._read_latch(handle) is not state:
                raise CustodyUnavailable

    def deny_for_successor(self, handle: CustodyDescriptor) -> CustodyLatch:
        with custody_native_job():
            previous = self._read_latch(handle)
            if previous is CustodyLatch.ARMED:
                return previous
            bootstrap = CustodyLatch.BOOTSTRAP_FROM_OPEN
            self._write_latch(handle, bootstrap)
            if self._read_latch(handle) is not bootstrap:
                raise CustodyUnavailable
            return bootstrap

    def require_process_absent(self, previous: CustodyProcessIdentity) -> None:
        self.require_current()
        self._platform.require_process_absent(previous)

    def _open_lock(self, name: CustodyLockName) -> CustodyDescriptor:
        self.require_current()
        with ExitStack() as cleanup:
            lease = CustodyDescriptor(
                os.open(name, FILE_FLAGS, dir_fd=self._root.descriptor)
            )
            cleanup.callback(lease.close)
            self.require_file(name, lease.descriptor)
            cleanup.pop_all()
            return lease

    def lock(
        self, name: CustodyLockName, *, exclusive: bool = True
    ) -> CustodyDescriptor:
        with custody_native_job(), ExitStack() as cleanup:
            lease = self._open_lock(name)
            cleanup.callback(lease.close)
            fcntl.flock(
                lease.descriptor,
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
            self.require_current()
            self.require_file(name, lease.descriptor)
            cleanup.pop_all()
            return lease

    def open_wait(self, name: CustodyWaitName) -> CustodyNativeWait:
        with custody_native_job():
            return CustodyNativeWait(self, name, self._open_lock(name))

    @contextmanager
    def acquire(
        self, name: CustodyLockName, *, exclusive: bool = True
    ) -> Generator[None]:
        lease = self.lock(name, exclusive=exclusive)
        try:
            yield
        finally:
            lease.close()

    def journal_directory(self) -> int:
        return self._root.descriptor

    def require_private_acl(self, descriptor: int) -> None:
        self._platform.require_private_acl(descriptor)

    def admit_journal_main(
        self, descriptor: int, empty: int, *, initialize: bool
    ) -> bool:
        self.require_current()
        self.require_file("authority.sqlite3", descriptor)
        if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
            raise CustodyUnavailable
        if bool(empty) != initialize:
            raise CustodyUnavailable
        if initialize and (os.fstat(descriptor).st_size or os.pread(descriptor, 1, 0)):
            raise CustodyUnavailable
        names = ("authority.sqlite3-wal", "authority.sqlite3-shm")
        if initialize:
            names = (*names, "authority.sqlite3-journal")
        for name in names:
            try:
                os.stat(name, dir_fd=self._root.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise CustodyUnavailable
        return True

    def admit_rollback_journal(self, descriptor: int) -> bool:
        self.require_current()
        name = "authority.sqlite3-journal"
        value = require_native_file(
            descriptor,
            directory=False,
            device=int.from_bytes(self.root_anchor.device, "big"),
        )
        named = os.stat(name, dir_fd=self._root.descriptor, follow_symlinks=False)
        if file_anchor(value, self.path / name) != file_anchor(named, self.path / name):
            raise CustodyUnavailable
        if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
            raise CustodyUnavailable
        self._platform.require_private_acl(descriptor)
        return True

    def close(self) -> None:
        with custody_native_job(), ExitStack() as cleanup:
            cleanup.callback(self._parent.close)
            cleanup.callback(self._root.close)
            for handle in self._files.values():
                cleanup.callback(handle.close)


class CustodyNativeWait:
    _root: CustodyNativeDirectory
    _name: CustodyWaitName
    _handle: CustodyDescriptor
    _started: bool
    _acquired: bool

    def __init__(
        self,
        root: CustodyNativeDirectory,
        name: CustodyWaitName,
        handle: CustodyDescriptor,
    ) -> None:
        self._root = root
        self._name = name
        self._handle = handle
        self._started = False
        self._acquired = False

    def acquire(self) -> None:
        if self._started or NATIVE_THREAD_STATE.depth != 0:
            raise CustodyUnavailable
        self._started = True
        fcntl.flock(self._handle.descriptor, fcntl.LOCK_EX)
        with custody_native_job(resumed_wait=True):
            self._root.require_current()
            self._root.require_file(self._name, self._handle.descriptor)
            self._acquired = True

    def require_acquired(self) -> None:
        if not self._acquired:
            raise CustodyUnavailable
        with custody_native_job():
            self._root.require_current()
            self._root.require_file(self._name, self._handle.descriptor)

    def require_commit(self, root: CustodyNativeDirectory) -> None:
        if self._root is not root or self._name != "commit.lock":
            raise CustodyUnavailable
        self.require_acquired()

    def close(self) -> None:
        self._handle.close()


class CustodyLifetimeLease:
    _root: CustodyNativeDirectory
    _owner: CustodyDescriptor

    def __init__(self, root: CustodyNativeDirectory) -> None:
        self._root = root
        self._owner = root.lock("lifetime.lock", exclusive=False)

    def require_current(self) -> CustodyNativeDirectory:
        if self._owner.descriptor < 0:
            raise CustodyUnavailable
        self._root.require_current()
        return self._root

    def close(self) -> None:
        self._owner.close()


class CustodyRecoveryLease:
    _root: CustodyNativeDirectory
    _owner: CustodyDescriptor

    def __init__(self, root: CustodyNativeDirectory) -> None:
        self._root = root
        self._owner = root.lock("recovery.lock")

    def require_current(self) -> CustodyNativeDirectory:
        if self._owner.descriptor < 0:
            raise CustodyUnavailable
        self._root.require_current()
        return self._root

    def arm(self) -> None:
        self._root.transition_latch(self._owner, CustodyLatch.OPEN, CustodyLatch.ARMED)

    def deny_for_successor(self, lifetime: CustodyLifetimeLease) -> CustodyLatch:
        if lifetime.require_current() is not self.require_current():
            raise CustodyUnavailable
        return self._root.deny_for_successor(self._owner)

    def arm_successor(self, lifetime: CustodyLifetimeLease) -> None:
        if lifetime.require_current() is not self.require_current():
            raise CustodyUnavailable
        self._root.transition_latch(
            self._owner, CustodyLatch.BOOTSTRAP_FROM_OPEN, CustodyLatch.ARMED
        )

    def reopen(self) -> None:
        self._root.transition_latch(self._owner, CustodyLatch.ARMED, CustodyLatch.OPEN)

    def close(self) -> None:
        self._owner.close()
