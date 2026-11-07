import ctypes
import errno
import hashlib
import os
import platform
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Protocol, cast

from pydantic import Field, StrictStr, field_validator

from kora.protocols.boundary import BoundaryModel
from kora.protocols.lifetime_records import Uint32, Uint64


class FilesystemError(Exception):
    pass


class FileCapacityError(FilesystemError):
    pass


class DirectoryIdentity(BoundaryModel):
    name: Annotated[
        StrictStr, Field(min_length=1, max_length=255, pattern=r"^[^/\x00]+$")
    ]
    device: Uint64
    inode: Uint64
    owner_uid: Uint32

    @field_validator("name")
    @classmethod
    def relative_name(cls, value: str) -> str:
        if value in (".", ".."):
            raise ValueError("relative entry name required")
        return value


class FileIdentity(DirectoryIdentity):
    pass


def file_identity(descriptor: int, name: str) -> FileIdentity:
    verify_private(descriptor, directory=False)
    metadata = os.fstat(descriptor)
    return FileIdentity(
        name=name,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
    )


def identity_of(descriptor: int, name: str) -> DirectoryIdentity:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise FilesystemError("directory_required")
    return DirectoryIdentity(
        name=name,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
    )


def verify_private(descriptor: int, *, directory: bool) -> None:
    metadata = os.fstat(descriptor)
    expected = 0o700 if directory else 0o600
    valid_kind = (
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    )
    if (
        not valid_kind
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != expected
    ):
        raise FilesystemError("private_identity_required")
    if not directory and metadata.st_nlink != 1:
        raise FilesystemError("exclusive_file_required")


def open_ancestry(path: Path) -> tuple[int, tuple[DirectoryIdentity, ...]]:
    if not path.is_absolute() or ".." in path.parts:
        raise FilesystemError("absolute_directory_required")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    ancestors: list[DirectoryIdentity] = []
    try:
        for component in path.parts[1:]:
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
        return descriptor, tuple(ancestors)
    except (OSError, FilesystemError):
        os.close(descriptor)
        raise


def open_created_directory(path: Path) -> "DirectoryAnchor":
    if path.name in ("", ".", ".."):
        raise FilesystemError("invalid_relative_name")
    parent, _ = open_ancestry(path.parent)
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    finally:
        os.close(parent)
    return open_local_directory(path)


class DirectoryAnchor:
    def __init__(
        self,
        descriptor: int,
        identity: DirectoryIdentity,
        ancestors: tuple[DirectoryIdentity, ...] = (),
    ) -> None:
        self.descriptor = descriptor
        self.identity = identity
        self.closed = False
        self.ancestors = ancestors

    @classmethod
    def open_path(cls, path: Path) -> "DirectoryAnchor":
        descriptor, ancestors = open_ancestry(path)
        try:
            verify_private(descriptor, directory=True)
            return cls(descriptor, identity_of(descriptor, path.name), ancestors)
        except (OSError, FilesystemError):
            os.close(descriptor)
            raise

    def open_child(
        self, expected: DirectoryIdentity, *, restore_owner_write: bool = False
    ) -> "DirectoryAnchor":
        self.verify()
        descriptor = os.open(
            expected.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self.descriptor,
        )
        try:
            if restore_owner_write:
                if (
                    expected.device != self.identity.device
                    or expected.owner_uid != os.getuid()
                    or identity_of(descriptor, expected.name) != expected
                ):
                    raise FilesystemError("identity_mismatch")
                if stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o500:
                    os.fchmod(descriptor, 0o700)
            verify_private(descriptor, directory=True)
            if identity_of(descriptor, expected.name) != expected:
                raise FilesystemError("identity_mismatch")
            return DirectoryAnchor(descriptor, expected)
        except (OSError, FilesystemError):
            os.close(descriptor)
            raise

    def verify(self) -> None:
        if (
            self.closed
            or identity_of(self.descriptor, self.identity.name) != self.identity
        ):
            raise FilesystemError("identity_mismatch")
        verify_private(self.descriptor, directory=True)

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


def create_directory(parent: DirectoryAnchor, name: str) -> DirectoryAnchor:
    if name in (".", "..") or "/" in name or not name:
        raise FilesystemError("invalid_relative_name")
    parent.verify()
    os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent.descriptor,
        )
    except OSError:
        os.rmdir(name, dir_fd=parent.descriptor)
        raise
    try:
        verify_private(descriptor, directory=True)
        identity = identity_of(descriptor, name)
        os.fsync(parent.descriptor)
    except (OSError, FilesystemError):
        try:
            os.close(descriptor)
        finally:
            os.rmdir(name, dir_fd=parent.descriptor)
        raise
    return DirectoryAnchor(descriptor, identity)


def remove_directory(
    parent: DirectoryAnchor,
    expected: DirectoryIdentity,
    *,
    max_entries: int = 65536,
    restore_owner_write: bool = False,
) -> None:
    remaining = [max_entries]
    _remove_directory(parent, expected, remaining, restore_owner_write)


def _remove_directory(
    parent: DirectoryAnchor,
    expected: DirectoryIdentity,
    remaining: list[int],
    restore_owner_write: bool,
) -> None:
    child = parent.open_child(expected, restore_owner_write=restore_owner_write)
    try:
        with os.scandir(child.descriptor) as entries:
            for entry in entries:
                remaining[0] -= 1
                if remaining[0] < 0:
                    raise FilesystemError("tree_capacity")
                metadata = os.stat(
                    entry.name, dir_fd=child.descriptor, follow_symlinks=False
                )
                if (
                    metadata.st_uid != os.getuid()
                    or stat.S_ISLNK(metadata.st_mode)
                    or (restore_owner_write and metadata.st_dev != expected.device)
                ):
                    raise FilesystemError("foreign_tree_entry")
                if stat.S_ISDIR(metadata.st_mode):
                    nested = DirectoryIdentity(
                        name=entry.name,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        owner_uid=metadata.st_uid,
                    )
                    _remove_directory(child, nested, remaining, restore_owner_write)
                elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    os.unlink(entry.name, dir_fd=child.descriptor)
                else:
                    raise FilesystemError("unsupported_tree_entry")
        confirming = parent.open_child(expected)
        confirming.close()
        os.rmdir(expected.name, dir_fd=parent.descriptor)
        os.fsync(parent.descriptor)
    finally:
        child.close()


LINUX_REFUSED_FILESYSTEMS: dict[int, str] = {
    0x6969: "nfs",
    0x517B: "smb",
    0xFF534D42: "cifs",
    0xFE534D42: "smb2",
    0x65735546: "fuse",
    0x73757245: "coda",
    0x5346414F: "afs",
    0x6B414653: "afs",
    0x01021997: "9p",
    0x00C36400: "ceph",
    0x564C: "ncp",
    0x0BD00BD0: "lustre",
    0x01161970: "gfs2",
    0x7461636F: "ocfs2",
    0x47504653: "gpfs",
    0x6E667364: "nfsd",
    0x20030528: "orangefs",
    0x19830326: "beegfs",
}
DARWIN_REFUSED_FILESYSTEMS: frozenset[str] = frozenset(
    {"nfs", "smbfs", "afpfs", "webdav", "cifs", "ftp", "macfuse", "osxfuse", "fusefs"}
)
RENAME_NOREPLACE: int = 1
RENAME_EXCL: int = 4


class LinuxStatfs(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_long), ("rest", ctypes.c_byte * 256)]
    kind: int


class DarwinStatfs(ctypes.Structure):
    _fields_ = [
        ("head", ctypes.c_byte * 64),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("type_name", ctypes.c_char * 16),
        ("rest", ctypes.c_byte * 2080),
    ]
    flags: int
    type_name: bytes


class FileStatus(Protocol):
    def __call__(self, descriptor: int, result: object) -> int: ...


class RenameAt(Protocol):
    def __call__(
        self, source_fd: int, source: bytes, target_fd: int, target: bytes, flags: int
    ) -> int: ...


LIBC: ctypes.CDLL = ctypes.CDLL(None, use_errno=True)


def file_status(descriptor: int, result: ctypes.Structure) -> None:
    symbol = "fstatfs"
    if sys.platform == "darwin" and platform.machine() == "x86_64":
        symbol = "fstatfs$INODE64"
    status = cast(FileStatus, LIBC[symbol])
    if status(descriptor, ctypes.byref(result)) != 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))


def filesystem_kind(descriptor: int) -> int | str:
    if sys.platform == "darwin":
        darwin = DarwinStatfs()
        file_status(descriptor, darwin)
        try:
            return darwin.type_name.decode("ascii")
        except UnicodeDecodeError:
            raise FilesystemError("filesystem_refused") from None
    linux = LinuxStatfs()
    file_status(descriptor, linux)
    return linux.kind & 0xFFFFFFFF


def refused_filesystem(kind: int | str) -> bool:
    if isinstance(kind, str):
        return kind in DARWIN_REFUSED_FILESYSTEMS
    return kind in LINUX_REFUSED_FILESYSTEMS


def require_local_filesystem(descriptor: int) -> None:
    if refused_filesystem(filesystem_kind(descriptor)):
        raise FilesystemError("filesystem_refused")


def open_local_directory(path: Path) -> DirectoryAnchor:
    anchor = DirectoryAnchor.open_path(path)
    try:
        require_local_filesystem(anchor.descriptor)
        return anchor
    except (OSError, FilesystemError):
        anchor.close()
        raise


def rename_no_replace(directory: DirectoryAnchor, source: str, target: str) -> None:
    function = "renameatx_np" if sys.platform == "darwin" else "renameat2"
    flags = RENAME_EXCL if sys.platform == "darwin" else RENAME_NOREPLACE
    rename = cast(RenameAt, getattr(LIBC, function))
    descriptor = directory.descriptor
    if rename(descriptor, source.encode(), descriptor, target.encode(), flags) != 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))


def open_private_directory(parent: DirectoryAnchor, name: str) -> DirectoryAnchor:
    parent.verify()
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.descriptor
    )
    try:
        verify_private(descriptor, directory=True)
        return DirectoryAnchor(descriptor, identity_of(descriptor, name))
    except (OSError, FilesystemError):
        os.close(descriptor)
        raise


CREATE_ATTEMPTS: int = 16


def open_file_at(directory: DirectoryAnchor, name: str, flags: int) -> int:
    retries = CREATE_ATTEMPTS - 1 if flags & os.O_CREAT else 0
    for _ in range(retries):
        try:
            return os.open(name, flags, 0o600, dir_fd=directory.descriptor)
        except FileNotFoundError:
            continue
    return os.open(name, flags, 0o600, dir_fd=directory.descriptor)


def open_private_file(directory: DirectoryAnchor, name: str, flags: int) -> int:
    descriptor = open_file_at(directory, name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        verify_private(descriptor, directory=False)
        return descriptor
    except (OSError, FilesystemError):
        os.close(descriptor)
        raise


MAX_SEAL_ENTRIES: Final[int] = 1000
SEAL_COPY_BYTES: Final[int] = 65536
RESOLVE_NO_SYMLINKS: Final[int] = 0x04
RESOLVE_BENEATH: Final[int] = 0x08
OPENAT2: Final[int] = 437


class SealCapacityError(FilesystemError):
    pass


@dataclass(frozen=True)
class SnapshotLimits:
    file_bytes: int
    attempt_bytes: int


SNAPSHOT_LIMITS: Final[SnapshotLimits] = SnapshotLimits(1 << 30, 4 << 30)


@dataclass
class SnapshotBudget:
    file_bytes: int
    remaining: int

    def store(self, source: int, destination: int, name: str) -> str | None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            target = os.open(name, flags, 0o600, dir_fd=destination)
        except OSError as error:
            if _blocks_snapshot(error):
                raise
            return None
        try:
            try:
                os.fchmod(target, 0o600)
                copied = copy_sealed_payload(
                    source, target, self.file_bytes, self.remaining
                )
                if copied is not None:
                    os.fsync(target)
            except OSError as error:
                if _blocks_snapshot(error):
                    raise
                copied = None
        finally:
            os.close(target)
        if copied is None:
            _discard_name(destination, name)
            return None
        digest, size = copied
        self.remaining -= size
        return digest


@dataclass(frozen=True)
class SealedFile:
    name: str
    sha256: str


@dataclass(frozen=True)
class SealedTree:
    files: tuple[SealedFile, ...]
    refused: tuple[str, ...]


class OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]
    flags: int
    mode: int
    resolve: int


OPEN_BENEATH = ctypes.CFUNCTYPE(
    ctypes.c_long,
    ctypes.c_long,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.POINTER(OpenHow),
    ctypes.c_size_t,
    use_errno=True,
)(("syscall", LIBC))


def snapshot_regular_tree(
    source: DirectoryAnchor,
    snapshot: DirectoryAnchor,
    limits: SnapshotLimits = SNAPSHOT_LIMITS,
) -> SealedTree:
    device = os.fstat(source.descriptor).st_dev
    files: list[SealedFile] = []
    refused: list[str] = []
    _collect(
        source.descriptor,
        source.descriptor,
        snapshot.descriptor,
        device,
        "",
        files,
        refused,
        0,
        SnapshotBudget(limits.file_bytes, limits.attempt_bytes),
    )
    os.fsync(snapshot.descriptor)
    files.sort(key=lambda item: item.name)
    refused.sort()
    return SealedTree(tuple(files), tuple(refused))


def freeze_private_tree(root: DirectoryAnchor) -> None:
    root.verify()
    _freeze(root.descriptor, root.identity.device)
    os.fchmod(root.descriptor, 0o500)
    os.fsync(root.descriptor)


def _collect(
    root: int,
    parent: int,
    destination: int,
    device: int,
    prefix: str,
    files: list[SealedFile],
    refused: list[str],
    count: int,
    budget: SnapshotBudget,
) -> int:
    for name in _bounded_entry_names(parent, count):
        count = _count(count)
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise FilesystemError("snapshot_name_encoding") from None
        relative = name if prefix == "" else f"{prefix}/{name}"
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            if _blocks_snapshot(error):
                raise
            refused.append(relative)
            continue
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_dev != device:
            refused.append(relative)
            continue
        if stat.S_ISDIR(metadata.st_mode):
            count = _copy_directory(
                root,
                parent,
                destination,
                device,
                name,
                relative,
                files,
                refused,
                count,
                budget,
            )
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            refused.append(relative)
            continue
        digest = _copy_file(root, parent, destination, name, relative, device, budget)
        if digest is None:
            refused.append(relative)
            continue
        files.append(SealedFile(relative, digest))
    return count


def _copy_directory(
    root: int,
    parent: int,
    destination: int,
    device: int,
    name: str,
    relative: str,
    files: list[SealedFile],
    refused: list[str],
    count: int,
    budget: SnapshotBudget,
) -> int:
    child = _open_child_directory(parent, name)
    if child is None:
        refused.append(relative)
        return count
    try:
        try:
            copied = _make_directory(destination, name)
        except OSError as error:
            if _blocks_snapshot(error):
                raise
            count = _refuse_visible(child, relative, refused, count)
            refused.append(relative)
            return count
        try:
            count = _collect(
                root, child, copied, device, relative, files, refused, count, budget
            )
            os.fsync(copied)
        finally:
            os.close(copied)
    finally:
        os.close(child)
    return count


def _blocks_snapshot(error: OSError) -> bool:
    return error.errno in (errno.EIO, errno.ENOSPC, errno.EDQUOT)


def _refuse_visible(parent: int, prefix: str, refused: list[str], count: int) -> int:
    for name in _bounded_entry_names(parent, count):
        count = _count(count)
        relative = name if prefix == "" else f"{prefix}/{name}"
        refused.append(relative)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            if _blocks_snapshot(error):
                raise
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            continue
        child = _open_child_directory(parent, name)
        if child is None:
            continue
        try:
            count = _refuse_visible(child, relative, refused, count)
        finally:
            os.close(child)
    return count


def _count(count: int) -> int:
    if count >= MAX_SEAL_ENTRIES:
        raise SealCapacityError("seal_capacity")
    return count + 1


def _bounded_entry_names(parent: int, count: int) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(parent) as entries:
        for entry in entries:
            if count + len(names) >= MAX_SEAL_ENTRIES:
                raise SealCapacityError("seal_capacity")
            names.append(entry.name)
    names.sort()
    return tuple(names)


def _entry_names(parent: int) -> tuple[str, ...]:
    with os.scandir(parent) as entries:
        return tuple(sorted(entry.name for entry in entries))


def _open_child_directory(parent: int, name: str) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=parent)
    except OSError as error:
        if _blocks_snapshot(error):
            raise
        return None


def _make_directory(parent: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=parent)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except OSError:
        os.rmdir(name, dir_fd=parent)
        raise
    try:
        os.fchmod(descriptor, 0o700)
    except OSError:
        os.close(descriptor)
        os.rmdir(name, dir_fd=parent)
        raise
    return descriptor


def _copy_file(
    root: int,
    parent: int,
    destination: int,
    name: str,
    relative: str,
    device: int,
    budget: SnapshotBudget,
) -> str | None:
    try:
        if sys.platform == "linux":
            source = _open_beneath(root, relative)
        else:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            source = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        if _blocks_snapshot(error):
            raise
        return None
    try:
        try:
            metadata = os.fstat(source)
        except OSError as error:
            if _blocks_snapshot(error):
                raise
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_dev != device
            or metadata.st_size > budget.file_bytes
        ):
            return None
        return budget.store(source, destination, name)
    finally:
        os.close(source)


def copy_sealed_payload(
    source: int, target: int, file_bytes: int, remaining: int
) -> tuple[str, int] | None:
    hasher = hashlib.sha256()
    size = 0
    while chunk := os.read(source, min(SEAL_COPY_BYTES, file_bytes - size + 1)):
        size += len(chunk)
        if size > file_bytes:
            return None
        if size <= remaining:
            _write_all(target, chunk)
            hasher.update(chunk)
    if size > remaining:
        raise SealCapacityError("seal_capacity")
    return f"sha256:{hasher.hexdigest()}", size


def _write_all(descriptor: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        view = view[written:]


def _discard_name(parent: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        return


def _freeze(directory: int, device: int) -> None:
    for name in _entry_names(directory):
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        nested = stat.S_ISDIR(metadata.st_mode)
        if metadata.st_dev != device or (
            not nested and not stat.S_ISREG(metadata.st_mode)
        ):
            raise FilesystemError("unsupported_tree_entry")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if nested:
            flags |= os.O_DIRECTORY
        descriptor = os.open(name, flags, dir_fd=directory)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (device, metadata.st_ino):
                raise FilesystemError("identity_mismatch")
            verify_private(descriptor, directory=nested)
            if nested:
                _freeze(descriptor, device)
            os.fchmod(descriptor, 0o500 if nested else 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _open_beneath(root: int, relative: str) -> int:
    how = OpenHow(
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        0,
        RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS,
    )
    descriptor = int(
        OPEN_BENEATH(
            OPENAT2, root, os.fsencode(relative), ctypes.byref(how), ctypes.sizeof(how)
        )
    )
    if descriptor < 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))
    return descriptor


def read_private_file(directory: DirectoryAnchor, name: str, limit: int) -> bytes:
    descriptor = open_private_file(directory, name, os.O_RDONLY)
    try:
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not chunk:
                return bytes(result)
            result.extend(chunk)
        raise FileCapacityError("file_capacity")
    finally:
        os.close(descriptor)
