import ctypes
import os
import platform
import stat
import sys
from pathlib import Path
from typing import Annotated, Protocol, cast

from pydantic import Field, StrictStr, field_validator

from kora.protocols.boundary import BoundaryModel
from kora.protocols.lifetime import Uint32, Uint64


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
            verify_private(descriptor, directory=True)
            return cls(descriptor, identity_of(descriptor, path.name), tuple(ancestors))
        except (OSError, FilesystemError):
            os.close(descriptor)
            raise

    def open_child(self, expected: DirectoryIdentity) -> "DirectoryAnchor":
        self.verify()
        descriptor = os.open(
            expected.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self.descriptor,
        )
        try:
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
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.descriptor
    )
    try:
        verify_private(descriptor, directory=True)
        identity = identity_of(descriptor, name)
        os.fsync(parent.descriptor)
        return DirectoryAnchor(descriptor, identity)
    except (OSError, FilesystemError):
        os.close(descriptor)
        raise


def remove_directory(
    parent: DirectoryAnchor, expected: DirectoryIdentity, *, max_entries: int = 65536
) -> None:
    remaining = [max_entries]
    _remove_directory(parent, expected, remaining)


def _remove_directory(
    parent: DirectoryAnchor, expected: DirectoryIdentity, remaining: list[int]
) -> None:
    child = parent.open_child(expected)
    try:
        with os.scandir(child.descriptor) as entries:
            for entry in entries:
                remaining[0] -= 1
                if remaining[0] < 0:
                    raise FilesystemError("tree_capacity")
                metadata = os.stat(
                    entry.name, dir_fd=child.descriptor, follow_symlinks=False
                )
                if metadata.st_uid != os.getuid() or stat.S_ISLNK(metadata.st_mode):
                    raise FilesystemError("foreign_tree_entry")
                if stat.S_ISDIR(metadata.st_mode):
                    nested = DirectoryIdentity(
                        name=entry.name,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        owner_uid=metadata.st_uid,
                    )
                    _remove_directory(child, nested, remaining)
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
        ("head", ctypes.c_byte * 72),
        ("type_name", ctypes.c_char * 16),
        ("rest", ctypes.c_byte * 2080),
    ]
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
