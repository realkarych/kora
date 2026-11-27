import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from unicodedata import category

from pydantic import Field, StrictStr

from kora.ir.scalars import Digest
from kora.plugins.runtime.local.bound_checkout import BoundCheckout
from kora.protocols.boundary import BoundaryModel
from kora.protocols.lifetime_records import Uint32, Uint64
from kora.protocols.time import Clock
from kora.runner.filesystem import (
    DirectoryIdentity,
    FilesystemError,
    identity_of,
    require_local_filesystem,
)

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MAX_PATH_BYTES = 1024
MAX_PATH_PARTS = 16
MAX_VISIT_NAMES = 4096
MAX_OPERATION_NAMES = 16384
Name = Annotated[StrictStr, Field(min_length=1, max_length=MAX_PATH_BYTES)]


class TargetRefusal(StrEnum):
    PATH = "path"
    SPELLING = "spelling"
    CAPACITY = "capacity"
    ROOT = "root"
    BINDING = "binding"
    FRESHNESS = "freshness"
    DEADLINE = "deadline"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"


class WorkspaceTargetError(Exception):
    def __init__(self, reason: TargetRefusal) -> None:
        self.reason = reason
        super().__init__(reason.value)


class PhysicalFileKey(BoundaryModel):
    device: Uint64
    inode: Uint64
    owner_uid: Uint32

    @classmethod
    def observe(cls, metadata: os.stat_result | DirectoryIdentity) -> "PhysicalFileKey":
        if isinstance(metadata, DirectoryIdentity):
            return cls(
                device=metadata.device,
                inode=metadata.inode,
                owner_uid=metadata.owner_uid,
            )
        return cls(
            device=metadata.st_dev, inode=metadata.st_ino, owner_uid=metadata.st_uid
        )


class WorkspaceRootProof(BoundaryModel):
    key: PhysicalFileKey
    lineage: Digest


class WorkspaceTargetProof(BoundaryModel):
    lexical: Name
    resolved: Name
    key: PhysicalFileKey


def _bounded(value: str, limit: int) -> bool:
    try:
        return len(value) <= limit and len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def _pwd_parts(pwd: str) -> list[str]:
    if not pwd.startswith("/") or not _bounded(pwd, 4096):
        raise WorkspaceTargetError(TargetRefusal.ROOT)
    parts = pwd[1:].split("/")
    if len(parts) > 32 or any(
        part in ("", ".", "..") or "\x00" in part or not _bounded(part, 255)
        for part in parts
    ):
        raise WorkspaceTargetError(TargetRefusal.ROOT)
    return parts


def _relative(path: str, pwd: str) -> str:
    if not path.startswith("/"):
        return path
    prefix = pwd + "/"
    if len(path) - len(prefix) > MAX_PATH_BYTES or not path.startswith(prefix):
        raise WorkspaceTargetError(TargetRefusal.PATH)
    return path[len(prefix) :]


def _path_parts(path: str, *, link: bool = False) -> list[str]:
    if (
        not _bounded(path, MAX_PATH_BYTES)
        or path != path.strip()
        or (not link and path.startswith(("@", "~")))
        or any(
            (char.isspace() and char != " ")
            or char == "\ufeff"
            or category(char) == "Cc"
            for char in path
        )
    ):
        raise WorkspaceTargetError(TargetRefusal.PATH)
    parts = path.split("/")
    if len(parts) > MAX_PATH_PARTS or any(
        not part or not _bounded(part, 255) or (not link and part in (".", ".."))
        for part in parts
    ):
        raise WorkspaceTargetError(TargetRefusal.PATH)
    return parts


def _freshness(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _close_ancestry(directories: list[int]) -> None:
    try:
        with ExitStack() as closing:
            while len(directories) > 1:
                closing.callback(os.close, directories.pop())
    except OSError:
        raise WorkspaceTargetError(TargetRefusal.UNAVAILABLE) from None


@dataclass(frozen=True, repr=False, slots=True)
class _OpenedTarget:
    descriptor: int
    proof: WorkspaceTargetProof
    walk: bytes
    freshness: tuple[int, int, int]


class WorkspaceTarget:
    def __init__(
        self,
        checkout: BoundCheckout,
        cutoff_ns: int,
        clock: Clock,
        *,
        check_live: Callable[[], None] | None = None,
    ) -> None:
        self._checkout = checkout
        self._pwd = str(checkout.path)
        self._ancestors = _pwd_parts(self._pwd)
        self._cutoff = cutoff_ns
        self._clock = clock
        self._check_live = check_live
        self._remaining = MAX_OPERATION_NAMES
        self._attempted = False
        self._revalidated = False
        self._closed = False
        self._close_failed = False
        self._action = ""
        self._target: _OpenedTarget | None = None
        self._root = self._observe_root()

    @property
    def root(self) -> WorkspaceRootProof:
        return self._root

    def _check(self) -> None:
        if self._closed:
            raise WorkspaceTargetError(TargetRefusal.CLOSED)
        if self._check_live is not None:
            self._check_live()
        if self._clock.monotonic_ns() >= self._cutoff:
            raise WorkspaceTargetError(TargetRefusal.DEADLINE)

    def _exact(self, parent: int, name: str) -> None:
        self._check()
        if not self._remaining:
            raise WorkspaceTargetError(TargetRefusal.CAPACITY)
        scanner = os.open(".", DIRECTORY, dir_fd=parent)
        try:
            with os.scandir(scanner) as entries:
                for _ in range(min(self._remaining, MAX_VISIT_NAMES)):
                    self._check()
                    entry = next(entries, None)
                    if entry is None:
                        raise WorkspaceTargetError(TargetRefusal.SPELLING)
                    self._remaining -= 1
                    if entry.name == name:
                        return
        finally:
            os.close(scanner)
        raise WorkspaceTargetError(TargetRefusal.CAPACITY)

    def _observe_root(self) -> WorkspaceRootProof:
        self._check()
        anchor = self._checkout.anchor
        try:
            anchor.verify()
            require_local_filesystem(anchor.descriptor)
            parent = os.open("/", DIRECTORY)
            try:
                lineage = hashlib.sha256(self._pwd.encode("utf-8") + b"\0")
                lineage.update(
                    PhysicalFileKey.observe(os.fstat(parent)).model_dump_json().encode()
                )
                for name, expected in zip(
                    self._ancestors, anchor.ancestors, strict=True
                ):
                    self._exact(parent, name)
                    child = os.open(name, DIRECTORY, dir_fd=parent)
                    closing, parent = parent, child
                    os.close(closing)
                    observed = identity_of(parent, name)
                    if observed != expected or observed.owner_uid not in (
                        0,
                        os.getuid(),
                    ):
                        raise WorkspaceTargetError(TargetRefusal.ROOT)
                    lineage.update(b"\0" + observed.model_dump_json().encode())
                root = PhysicalFileKey.observe(os.fstat(parent))
                if root != PhysicalFileKey.observe(anchor.identity):
                    raise WorkspaceTargetError(TargetRefusal.ROOT)
                self._check()
                return WorkspaceRootProof(
                    key=root, lineage=f"sha256:{lineage.hexdigest()}"
                )
            finally:
                os.close(parent)
        except (OSError, FilesystemError, ValueError):
            raise WorkspaceTargetError(TargetRefusal.ROOT) from None

    def _open(self, path: str) -> _OpenedTarget:
        self._check()
        anchor = self._checkout.anchor
        try:
            anchor.verify()
        except (OSError, FilesystemError):
            raise WorkspaceTargetError(TargetRefusal.ROOT) from None
        lexical = _relative(path, self._pwd)
        pending = _path_parts(lexical)
        names: list[str] = []
        directories = [anchor.descriptor]
        links = 0
        walk = hashlib.sha256()
        try:
            while pending:
                self._check()
                name = pending.pop(0)
                if name == ".":
                    continue
                if name == "..":
                    if not names:
                        raise WorkspaceTargetError(TargetRefusal.PATH)
                    os.close(directories.pop())
                    names.pop()
                    continue
                parent = directories[-1]
                self._exact(parent, name)
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                key = PhysicalFileKey.observe(observed)
                if (
                    key.device != self.root.key.device
                    or key.owner_uid != self.root.key.owner_uid
                ):
                    raise WorkspaceTargetError(TargetRefusal.PATH)
                walk.update(repr((name, key.device, key.inode, key.owner_uid)).encode())
                if stat.S_ISLNK(observed.st_mode):
                    links += 1
                    if links > 8 or observed.st_nlink != 1:
                        raise WorkspaceTargetError(TargetRefusal.PATH)
                    link_path = os.readlink(name, dir_fd=parent)
                    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (
                        after.st_mode != observed.st_mode
                        or after.st_nlink != 1
                        or PhysicalFileKey.observe(after) != key
                        or _freshness(after) != _freshness(observed)
                    ):
                        raise WorkspaceTargetError(TargetRefusal.BINDING)
                    walk.update(repr((link_path, _freshness(observed))).encode())
                    if link_path.startswith("/"):
                        link_path = _relative(link_path, self._pwd)
                        _close_ancestry(directories)
                        names.clear()
                    pending = _path_parts(link_path, link=True) + pending
                    if len(names) + len(pending) > MAX_PATH_PARTS or not _bounded(
                        "/".join(names + pending), MAX_PATH_BYTES
                    ):
                        raise WorkspaceTargetError(TargetRefusal.PATH)
                elif pending:
                    child = os.open(name, DIRECTORY, dir_fd=parent)
                    directories.append(child)
                    names.append(name)
                    if (
                        not stat.S_ISDIR(observed.st_mode)
                        or PhysicalFileKey.observe(os.fstat(child)) != key
                    ):
                        raise WorkspaceTargetError(TargetRefusal.BINDING)
                else:
                    return self._open_leaf(
                        directories, lexical, names, name, observed, key, walk.digest()
                    )
            raise WorkspaceTargetError(TargetRefusal.PATH)
        except OSError:
            raise WorkspaceTargetError(TargetRefusal.UNAVAILABLE) from None
        finally:
            _close_ancestry(directories)

    def _open_leaf(
        self,
        directories: list[int],
        lexical: str,
        names: list[str],
        name: str,
        observed: os.stat_result,
        key: PhysicalFileKey,
        walk: bytes,
    ) -> _OpenedTarget:
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise WorkspaceTargetError(TargetRefusal.PATH)
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directories[-1]
        )
        admitted = False
        try:
            actual = os.fstat(descriptor)
            self._match_file(actual, key, _freshness(observed))
            _close_ancestry(directories)
            self._check()
            proof = WorkspaceTargetProof(
                lexical=lexical, resolved="/".join([*names, name]), key=key
            )
            opened = _OpenedTarget(descriptor, proof, walk, _freshness(actual))
            admitted = True
            return opened
        finally:
            if not admitted:
                os.close(descriptor)

    @staticmethod
    def _match_file(
        actual: os.stat_result, key: PhysicalFileKey, freshness: tuple[int, int, int]
    ) -> None:
        if (
            not stat.S_ISREG(actual.st_mode)
            or actual.st_nlink != 1
            or PhysicalFileKey.observe(actual) != key
        ):
            raise WorkspaceTargetError(TargetRefusal.BINDING)
        if _freshness(actual) != freshness:
            raise WorkspaceTargetError(TargetRefusal.FRESHNESS)

    def prepare(
        self, path: str, *, expected: WorkspaceTargetProof | None = None
    ) -> WorkspaceTargetProof:
        if self._attempted:
            raise WorkspaceTargetError(TargetRefusal.CLOSED)
        self._attempted = True
        self._target = self._open(path)
        if expected is not None and self._target.proof != expected:
            self.close()
            raise WorkspaceTargetError(TargetRefusal.BINDING)
        self._action = path
        return self._target.proof

    def revalidate(self) -> int:
        target = self._target
        if target is None or self._revalidated:
            raise WorkspaceTargetError(TargetRefusal.CLOSED)
        self._revalidated = True
        admitted = False
        try:
            if self._observe_root() != self.root:
                raise WorkspaceTargetError(TargetRefusal.ROOT)
            observed = self._open(self._action)
            try:
                if (observed.proof, observed.walk) != (target.proof, target.walk):
                    raise WorkspaceTargetError(TargetRefusal.BINDING)
            finally:
                os.close(observed.descriptor)
            self._match_file(
                os.fstat(target.descriptor), target.proof.key, target.freshness
            )
            self._check()
            admitted = True
            return target.descriptor
        except OSError:
            raise WorkspaceTargetError(TargetRefusal.UNAVAILABLE) from None
        finally:
            if not admitted:
                self.close()

    def close(self) -> None:
        self._closed = True
        target, self._target = self._target, None
        if target is not None:
            try:
                os.close(target.descriptor)
            except OSError:
                self._close_failed = True
        if self._close_failed:
            raise WorkspaceTargetError(TargetRefusal.UNAVAILABLE)
