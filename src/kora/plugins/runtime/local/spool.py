import asyncio
import ctypes
import errno
import os
import re
import secrets
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import StrEnum
from threading import Lock
from typing import Final, Literal

from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import Identifier, ProcessIdentity
from kora.protocols.time import Clock
from kora.runner.filesystem import (
    LIBC,
    DarwinStatfs,
    DirectoryAnchor,
    DirectoryIdentity,
    FileIdentity,
    FilesystemError,
    create_directory,
    file_identity,
    file_status,
    open_private_directory,
    open_private_file,
    verify_private,
)
from kora.runner.identity import (
    PROCESS_SCAN_ENTRIES,
    SCAN_NS,
    IdentityReader,
    IdentityState,
    complete_identity,
)
from kora.runner.native_io import observed
from kora.runner.registry import (
    COMPONENT,
    model_bytes,
    refusal,
    synchronize,
    write_atomically,
)

NONCE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
RECORD_LIMIT: Final[int] = 4096
IGNORE_OWNERSHIP: Final[int] = 0x00200000
SWEEP: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.spool_sweep",
        COMPONENT,
        Level.INFO,
        "a local spool ownership sweep finished",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.FLOAT},
    )
)


class SpoolRecord(BoundaryModel):
    format: Literal["local-spool/1"]
    execution_domain_id: Identifier
    directory: DirectoryIdentity
    process: ProcessIdentity


class SpoolSweepOutcome(StrEnum):
    COMPLETE = "complete"
    UNRESOLVED = "unresolved"
    DEFERRED = "deferred"


class SweepDeferred(Exception):
    pass


class OwnerState(StrEnum):
    LIVE = "live"
    GONE = "gone"
    UNKNOWN = "unknown"


def require_spool_volume(root: DirectoryAnchor) -> None:
    if sys.platform == "darwin":
        status = DarwinStatfs()
        file_status(root.descriptor, status)
        if status.flags & IGNORE_OWNERSHIP:
            raise FilesystemError("spool_ownership_disabled")
    require_private_acl(root.descriptor)


def require_private_acl(descriptor: int) -> None:
    if sys.platform != "darwin":
        return
    get_acl = LIBC.acl_get_fd_np
    get_acl.argtypes = [ctypes.c_int, ctypes.c_int]
    get_acl.restype = ctypes.c_void_p
    get_entry = LIBC.acl_get_entry
    get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    get_entry.restype = ctypes.c_int
    get_tag = LIBC.acl_get_tag_type
    get_tag.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    get_tag.restype = ctypes.c_int
    free = LIBC.acl_free
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(descriptor, 0x100)
    if acl is None:
        if ctypes.get_errno() == errno.ENOENT:
            return
        raise FilesystemError("spool_acl_unavailable")
    try:
        entry = ctypes.c_void_p()
        tag = ctypes.c_int()
        for index in range(170):
            ctypes.set_errno(0)
            if get_entry(acl, 0 if index == 0 else -1, ctypes.byref(entry)) != 0:
                if ctypes.get_errno() == errno.EINVAL:
                    return
                break
            if get_tag(entry, ctypes.byref(tag)) != 0 or tag.value != 2:
                break
        raise FilesystemError("spool_acl_unavailable")
    finally:
        if free(acl) != 0:
            raise FilesystemError("spool_acl_unavailable")


def verify_directory(directory: DirectoryAnchor, device: int) -> None:
    directory.verify()
    if directory.identity.device != device:
        raise FilesystemError("spool_device_mismatch")
    require_private_acl(directory.descriptor)


def record_name(nonce: str) -> str:
    return f"{nonce}.json"


def owner_state(
    owner: ProcessIdentity, current: ProcessIdentity, reader: IdentityReader
) -> OwnerState:
    lineage = owner.identity_key()[:3]
    if not complete_identity(owner) or lineage != current.identity_key()[:3]:
        return OwnerState.UNKNOWN
    observation = reader.read(owner.pid)
    if observation.state is IdentityState.ABSENT:
        return OwnerState.GONE
    process = observation.process
    if (
        observation.state is not IdentityState.PRESENT
        or process is None
        or process.identity.identity_key()[:4] != owner.identity_key()[:4]
    ):
        return OwnerState.UNKNOWN
    if process.dead or process.identity.start_time != owner.start_time:
        return OwnerState.GONE
    return OwnerState.LIVE


@contextmanager
def opened_record(
    parent: DirectoryAnchor, nonce: str, domain: str
) -> Generator[tuple[SpoolRecord, FileIdentity]]:
    name = record_name(nonce)
    descriptor = open_private_file(parent, name, os.O_RDONLY)
    try:
        identity = file_identity(descriptor, name)
        if identity.device != parent.identity.device:
            raise FilesystemError("spool_device_mismatch")
        require_private_acl(descriptor)
        data = bytearray()
        while len(data) <= RECORD_LIMIT:
            chunk = os.read(descriptor, RECORD_LIMIT + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > RECORD_LIMIT:
            raise FilesystemError("spool_record_capacity")
        try:
            record = SpoolRecord.model_validate_json(data)
            canonical = model_bytes(record)
        except ValueError:
            raise FilesystemError("spool_record_malformed") from None
        if (
            bytes(data) != canonical
            or record.execution_domain_id != domain
            or record.directory.name != nonce
            or record.directory.device != parent.identity.device
            or record.directory.owner_uid != os.getuid()
            or not complete_identity(record.process)
        ):
            raise FilesystemError("spool_owner_mismatch")
        yield record, identity
    finally:
        os.close(descriptor)


def current_file(parent: DirectoryAnchor, expected: FileIdentity) -> None:
    descriptor = open_private_file(parent, expected.name, os.O_RDONLY)
    try:
        if file_identity(descriptor, expected.name) != expected:
            raise FilesystemError("spool_record_replaced")
    finally:
        os.close(descriptor)


def remove_spool_directory(
    parent: DirectoryAnchor, expected: DirectoryIdentity, check: Callable[[], None]
) -> None:
    check()
    try:
        directory = parent.open_child(expected)
    except FileNotFoundError:
        return
    try:
        verify_directory(directory, parent.identity.device)
        check()
        with os.scandir(directory.descriptor) as entries:
            first = next(entries, None)
            if first is not None and next(entries, None) is not None:
                raise FilesystemError("spool_residue_capacity")
        if first is not None:
            check()
            if not first.name.startswith("file-") or not NONCE.fullmatch(
                first.name[5:]
            ):
                raise FilesystemError("spool_residue_unknown")
            descriptor = open_private_file(directory, first.name, os.O_RDONLY)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_dev != expected.device or metadata.st_size:
                    raise FilesystemError("spool_residue_unknown")
                require_private_acl(descriptor)
                check()
                current_file(directory, file_identity(descriptor, first.name))
                check()
                os.unlink(first.name, dir_fd=directory.descriptor)
            finally:
                os.close(descriptor)
        check()
        parent.open_child(expected).close()
        check()
        os.rmdir(expected.name, dir_fd=parent.descriptor)
    finally:
        directory.close()


def remove_owner(
    parent: DirectoryAnchor,
    record: SpoolRecord,
    identity: FileIdentity,
    check: Callable[[], None],
) -> None:
    remove_spool_directory(parent, record.directory, check)
    check()
    synchronize(parent.descriptor)
    check()
    current_file(parent, identity)
    check()
    os.unlink(identity.name, dir_fd=parent.descriptor)
    synchronize(parent.descriptor)


def entry_exists(parent: DirectoryAnchor, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def inspect_entry(
    parent: DirectoryAnchor,
    name: str,
    domain: str,
    current: ProcessIdentity,
    reader: IdentityReader,
    check: Callable[[], None],
) -> bool:
    if NONCE.fullmatch(name):
        check()
        if entry_exists(parent, record_name(name)):
            return True
        check()
        return not entry_exists(parent, name)
    nonce, suffix = os.path.splitext(name)
    if suffix != ".json" or not NONCE.fullmatch(nonce):
        return False
    check()
    with opened_record(parent, nonce, domain) as (record, identity):
        check()
        state = owner_state(record.process, current, reader)
        if state is OwnerState.GONE:
            remove_owner(parent, record, identity, check)
        return state is not OwnerState.UNKNOWN


def sweep_spools(
    parent: DirectoryAnchor,
    domain: str,
    process: ProcessIdentity,
    reader: IdentityReader,
    clock: Clock,
    *,
    max_entries: int = PROCESS_SCAN_ENTRIES,
) -> SpoolSweepOutcome:
    deadline = clock.monotonic_ns() + SCAN_NS

    def check() -> None:
        if clock.monotonic_ns() >= deadline:
            raise SweepDeferred

    outcome = SpoolSweepOutcome.COMPLETE
    try:
        check()
        with os.scandir(parent.descriptor) as entries:
            for _ in range(max_entries):
                check()
                entry = next(entries, None)
                if entry is None:
                    return outcome
                try:
                    if not inspect_entry(
                        parent, entry.name, domain, process, reader, check
                    ):
                        outcome = SpoolSweepOutcome.UNRESOLVED
                except (OSError, FilesystemError, ValueError):
                    outcome = SpoolSweepOutcome.UNRESOLVED
    except SweepDeferred:
        return SpoolSweepOutcome.DEFERRED
    except OSError:
        return SpoolSweepOutcome.UNRESOLVED
    return SpoolSweepOutcome.DEFERRED


class SpoolLease:
    def __init__(self, descriptor: int, owner: "SpoolOwner") -> None:
        self.descriptor = descriptor
        self._owner = owner
        self._closing = False
        self.closed = False

    def close(self) -> None:
        self._owner.release(self)

    def claim(self) -> bool:
        if self._closing:
            return False
        self._closing = True
        return True


class SpoolOwner:
    def __init__(
        self,
        parent: DirectoryAnchor,
        directory: DirectoryAnchor,
        record: SpoolRecord,
        outcome: SpoolSweepOutcome,
    ) -> None:
        self.parent = parent
        self.directory = directory
        self.record = record
        self.outcome = outcome
        self._settling = Lock()
        self._leases: set[SpoolLease] = set()
        self._fenced = False

    @classmethod
    def open(
        cls,
        root: DirectoryAnchor,
        domain: str,
        process: ProcessIdentity,
        reader: IdentityReader,
        clock: Clock,
    ) -> "SpoolOwner":
        if not complete_identity(process):
            raise FilesystemError("spool_process_unknown")
        require_spool_volume(root)
        try:
            parent = create_directory(root, "spools")
        except FileExistsError:
            parent = open_private_directory(root, "spools")
        try:
            verify_directory(parent, root.identity.device)
            synchronize(root.descriptor)
            outcome = sweep_spools(parent, domain, process, reader, clock)
            nonce = secrets.token_hex(16)
            directory = create_directory(parent, nonce)
            publication_started = False
            try:
                verify_directory(directory, parent.identity.device)
                record = SpoolRecord(
                    format="local-spool/1",
                    execution_domain_id=domain,
                    directory=directory.identity,
                    process=process,
                )
                encoded = model_bytes(record)
                if len(encoded) > RECORD_LIMIT:
                    raise FilesystemError("spool_record_capacity")
                publication_started = True
                if not write_atomically(
                    parent,
                    record_name(nonce),
                    encoded,
                    secrets.token_hex(16),
                    replace=False,
                ):
                    publication_started = False
                    raise FilesystemError("spool_record_exists")
                return cls(parent, directory, record, outcome)
            except BaseException:
                try:
                    if not publication_started or not entry_exists(
                        parent, record_name(nonce)
                    ):
                        remove_spool_directory(
                            parent, directory.identity, directory.verify
                        )
                        synchronize(parent.descriptor)
                finally:
                    directory.close()
                raise
        except BaseException:
            parent.close()
            raise

    def release(self, lease: SpoolLease) -> None:
        with self._settling:
            if not lease.claim():
                return
            os.close(lease.descriptor)
            self._leases.remove(lease)
            lease.closed = True

    def acquire(self, active: Callable[[], None]) -> SpoolLease:
        active()
        if self._fenced:
            raise FilesystemError("spool_fenced")
        verify_directory(self.directory, self.parent.identity.device)
        self.parent.open_child(self.directory.identity).close()
        name = f"file-{secrets.token_hex(16)}"
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory.descriptor,
        )
        self._fenced = True
        lease = SpoolLease(descriptor, self)
        with self._settling:
            self._leases.add(lease)
        try:
            verify_private(descriptor, directory=False)
            if os.fstat(descriptor).st_dev != self.directory.identity.device:
                raise FilesystemError("spool_device_mismatch")
            require_private_acl(descriptor)
            os.unlink(name, dir_fd=self.directory.descriptor)
            synchronize(self.directory.descriptor)
        except BaseException:
            lease.close()
            raise
        self._fenced = False
        return lease

    def close(self) -> None:
        failure: OSError | None = None
        try:
            for lease in tuple(self._leases):
                try:
                    lease.close()
                except OSError as error:
                    if failure is None:
                        failure = error
            if failure is not None:
                raise failure
            if self._leases:
                raise FilesystemError("spool_lease_unresolved")
            with opened_record(
                self.parent,
                self.directory.identity.name,
                self.record.execution_domain_id,
            ) as (record, identity):
                if record != self.record:
                    raise FilesystemError("spool_owner_replaced")
                remove_owner(self.parent, record, identity, self.directory.verify)
        finally:
            try:
                self.directory.close()
            finally:
                self.parent.close()


async def close_spool(owner: SpoolOwner | None) -> None:
    if owner is not None:
        closing = asyncio.create_task(asyncio.to_thread(owner.close))
        cancelled = await observed(closing)
        try:
            closing.result()
        except (OSError, FilesystemError):
            raise refusal("registry_unavailable") from None
        if cancelled:
            raise asyncio.CancelledError
