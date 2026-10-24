import errno
import fcntl
import os
import stat
import sys
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from pydantic import StrictBool, StrictStr

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.native import filesystem, synchronize_file, validate_acl
from kora.protocols.identity import Count
from kora.protocols.storage_models import StorageIdentity, StorageLimits, StorageModel

DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS: Final[int] = os.O_NOFOLLOW | os.O_NONBLOCK


def random_identity() -> str:
    return uuid.uuid4().hex


class RootManifest(StorageModel):
    identity: StorageIdentity
    device: Count
    inode: Count
    owner: Count
    filesystem: StrictStr
    filesystem_id: StrictStr
    canonical_path: StrictStr
    limits: StorageLimits
    files_initialized: StrictBool
    control_initialized: StrictBool
    control_inode: Count | None


class FileLease:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def private_file(fd: int, device: int, multiple_links: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_dev != device
        or (not multiple_links and info.st_nlink != 1)
        or info.st_nlink < 1
    ):
        refuse("storage_path")
    validate_acl(fd)
    return info


def trusted_directory(fd: int, previous: os.stat_result | None) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
        refuse("storage_path")
    if previous is not None and previous.st_mode & 0o022:
        if previous.st_uid != 0 or not previous.st_mode & stat.S_ISVTX:
            refuse("storage_path")
    if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
        refuse("storage_path")
    validate_acl(fd)
    return info


def open_anchored_root(path: Path) -> tuple[Path, int]:
    raw = os.fsencode(path)
    if len(raw) > 4096 or len(path.parts) > 128:
        refuse("storage_path")
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError):
        refuse("storage_path")
    if len(os.fsencode(canonical)) > 4096 or len(canonical.parts) > 128:
        refuse("storage_path")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        previous = trusted_directory(descriptor, None)
        for index, component in enumerate(canonical.parts[1:]):
            try:
                os.stat("manifest.json", dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                refuse("storage_identity")
            final = index == len(canonical.parts) - 2
            if final:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            previous = trusted_directory(descriptor, previous)
        if previous.st_uid != os.geteuid() or stat.S_IMODE(previous.st_mode) != 0o700:
            refuse("storage_path")
        return canonical, descriptor
    except BaseException:
        os.close(descriptor)
        raise


class RootAnchor:
    def __init__(
        self,
        path: Path,
        fd: int,
        manifest: RootManifest,
        new_identity: Callable[[], str],
    ) -> None:
        self.path = path
        self.fd = fd
        self.manifest = manifest
        self.identity = manifest.identity
        self.new_identity = new_identity
        self.closed = False
        self._lifetime: FileLease | None = None
        self._lock_inodes: dict[str, int] = {}

    @classmethod
    def open(
        cls,
        path: Path,
        limits: StorageLimits,
        new_identity: Callable[[], str] = random_identity,
    ) -> Self:
        try:
            canonical, descriptor = open_anchored_root(path)
            with ExitStack() as rollback:
                rollback.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                filesystem_name, filesystem_id = filesystem(descriptor)
                try:
                    manifest_fd = os.open(
                        "manifest.json", os.O_RDONLY | FILE_FLAGS, dir_fd=descriptor
                    )
                except FileNotFoundError:
                    if os.listdir(descriptor):
                        refuse("storage_identity")
                    manifest = RootManifest(
                        version=1,
                        identity=StorageIdentity(version=1, store_id=new_identity()),
                        device=info.st_dev,
                        inode=info.st_ino,
                        owner=info.st_uid,
                        filesystem=filesystem_name,
                        filesystem_id=filesystem_id,
                        canonical_path=str(canonical),
                        limits=limits,
                        files_initialized=False,
                        control_initialized=False,
                        control_inode=None,
                    )
                    manifest_fd = os.open(
                        "manifest.json",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                        0o600,
                        dir_fd=descriptor,
                    )
                    with os.fdopen(manifest_fd, "wb") as target:
                        target.write(manifest.model_dump_json().encode())
                        target.flush()
                        synchronize_file(target.fileno())
                    os.fsync(descriptor)
                else:
                    with os.fdopen(manifest_fd, "rb") as source:
                        private_file(source.fileno(), info.st_dev)
                        encoded = source.read(16385)
                    if len(encoded) > 16384:
                        refuse("storage_identity")
                    manifest = RootManifest.model_validate_json(encoded)
                if (
                    manifest.version != 1
                    or manifest.device != info.st_dev
                    or manifest.inode != info.st_ino
                    or manifest.owner != info.st_uid
                    or manifest.filesystem != filesystem_name
                    or manifest.filesystem_id != filesystem_id
                    or manifest.canonical_path != str(canonical)
                    or manifest.limits != limits
                ):
                    refuse("storage_identity")
                root = cls(canonical, descriptor, manifest, new_identity)
                root.initialize_layout()
                rollback.callback(root.close_lifetime)
                rollback.pop_all()
                return root
        except (OSError, ValueError):
            refuse("storage_path")

    def initialize_layout(self) -> None:
        creating = not self.manifest.files_initialized
        if creating:
            self.ensure_directory(("locks",))
            for name in ("root", "lifetime"):
                os.close(self.open_file(("locks", name), os.O_RDWR | os.O_CREAT))
        gate = self.try_lock("root")
        if gate is None:
            refuse("storage_busy")
        with gate:
            exclusive = self.try_lock("lifetime") if creating else None
            if creating and exclusive is None:
                refuse("storage_busy")
            with ExitStack() as ownership:
                if exclusive is not None:
                    ownership.enter_context(exclusive)
                self.create_layout(creating)
                if creating:
                    updated = self.manifest.model_copy(
                        update={"files_initialized": True}
                    )
                    self.replace_manifest(updated)
            self._lifetime = self.try_lock("lifetime", shared=True)
            if self._lifetime is None:
                refuse("storage_busy")

    def create_layout(self, creating: bool) -> None:
        for directory in (("locks",), ("stages",), ("artifacts", "sha256")):
            os.close(self.directory(directory, create=creating))
        names = ["root", "lifetime"]
        names.extend(
            f"stage-{index}" for index in range(self.manifest.limits.stage_slots)
        )
        names.extend(
            f"delete-{index}" for index in range(self.manifest.limits.deletion_slots)
        )
        names.extend(
            f"data-{index}" for index in range(self.manifest.limits.data_work_slots)
        )
        names.extend(
            f"reader-{index}" for index in range(self.manifest.limits.reader_slots)
        )
        names.extend(f"control-{index}" for index in range(2))
        names.extend(f"address-{index}" for index in range(64))
        locks = self.directory(("locks",))
        try:
            for name in names:
                with FileLease(
                    os.open(
                        name,
                        os.O_RDWR | FILE_FLAGS | (os.O_CREAT if creating else 0),
                        0o600,
                        dir_fd=locks,
                    )
                ) as file:
                    private_file(file.fd, self.manifest.device)
                    self._lock_inodes[name] = os.fstat(file.fd).st_ino
            os.fsync(locks)
        finally:
            os.close(locks)

    def replace_manifest(self, manifest: RootManifest) -> None:
        encoded = manifest.model_dump_json().encode()
        try:
            pending = self.open_file(
                ("manifest.pending",), os.O_WRONLY | os.O_CREAT | os.O_EXCL
            )
        except FileExistsError:
            pending = self.open_file(("manifest.pending",), os.O_RDWR)
            existing = os.read(pending, 16385)
            if not encoded.startswith(existing):
                os.close(pending)
                refuse("storage_identity")
            os.lseek(pending, 0, os.SEEK_SET)
            os.ftruncate(pending, 0)
        with os.fdopen(pending, "wb") as target:
            target.write(encoded)
            target.flush()
            synchronize_file(target.fileno())
        os.replace(
            "manifest.pending", "manifest.json", src_dir_fd=self.fd, dst_dir_fd=self.fd
        )
        self.synchronize_directory(self.fd)
        self.manifest = manifest

    def synchronize_directory(self, descriptor: int) -> None:
        os.fsync(descriptor)
        if sys.platform == "darwin":
            with FileLease(self.open_file(("manifest.json",), os.O_RDONLY)) as marker:
                synchronize_file(marker.fd)

    def refresh_manifest(self) -> None:
        with os.fdopen(self.open_file(("manifest.json",), os.O_RDONLY), "rb") as source:
            encoded = source.read(16385)
        if len(encoded) > 16384:
            refuse("storage_identity")
        try:
            actual = RootManifest.model_validate_json(encoded)
        except ValueError:
            refuse("storage_identity")
        transitions = {"files_initialized", "control_initialized", "control_inode"}
        if actual.model_dump(exclude=transitions) != self.manifest.model_dump(
            exclude=transitions
        ):
            refuse("storage_identity")
        if (
            (self.manifest.files_initialized and not actual.files_initialized)
            or (self.manifest.control_initialized and not actual.control_initialized)
            or (
                self.manifest.control_inode is not None
                and actual.control_inode != self.manifest.control_inode
            )
        ):
            refuse("storage_identity")
        self.manifest = actual

    def validate_anchor(self) -> None:
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except OSError:
            refuse("storage_identity")
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino, info.st_uid)
            != (self.manifest.device, self.manifest.inode, self.manifest.owner)
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            refuse("storage_identity")

    def directory(self, components: tuple[str, ...], create: bool = False) -> int:
        fd = os.open(".", DIRECTORY_FLAGS, dir_fd=self.fd)
        try:
            for component in components:
                if not component or component in {".", ".."} or "/" in component:
                    refuse("storage_path")
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    self.synchronize_directory(fd)
                child = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if (
                    info.st_dev != self.manifest.device
                    or info.st_uid != self.manifest.owner
                    or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    refuse("storage_path")
                validate_acl(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def ensure_directory(self, components: tuple[str, ...]) -> None:
        os.close(self.directory(components, create=True))

    def open_file(
        self, components: tuple[str, ...], flags: int, multiple_links: bool = False
    ) -> int:
        if not components or components[-1] in {"", ".", ".."} or "/" in components[-1]:
            refuse("storage_path")
        try:
            directory = self.directory(components[:-1])
            try:
                fd = os.open(
                    components[-1], flags | FILE_FLAGS, 0o600, dir_fd=directory
                )
            finally:
                os.close(directory)
            try:
                private_file(fd, self.manifest.device, multiple_links)
                return fd
            except BaseException:
                os.close(fd)
                raise
        except OSError as error:
            if error.errno in {
                errno.ELOOP,
                errno.ENOTDIR,
                errno.EACCES,
                errno.EPERM,
                errno.ENOENT,
            }:
                refuse("storage_path")
            raise

    def try_lock(self, name: str, shared: bool = False) -> FileLease | None:
        descriptor = self.open_file(("locks", name), os.O_RDWR)
        try:
            expected = self._lock_inodes.get(name)
            if expected is not None and os.fstat(descriptor).st_ino != expected:
                refuse("storage_identity")
            fcntl.flock(
                descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            os.close(descriptor)
            return None
        except BaseException:
            os.close(descriptor)
            raise
        return FileLease(descriptor)

    def close_lifetime(self) -> None:
        if self._lifetime is not None:
            self._lifetime.close()
            self._lifetime = None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_lifetime()
            os.close(self.fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
