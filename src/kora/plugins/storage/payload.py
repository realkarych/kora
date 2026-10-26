import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.files import FileLease, RootAnchor
from kora.protocols.identity import Count, Identifier
from kora.protocols.storage_models import MAX_CHUNK_BYTES, SealedStage, StorageModel

Result = TypeVar("Result")


def raw_content_identity(content: SealedStage) -> str:
    return "raw:" + content.manifest_digest


class FileRunner(Protocol):
    async def __call__(
        self,
        function: Callable[[], Result],
        control: bool = False,
        on_cancel: Callable[[Result], None] | None = None,
    ) -> Result: ...


class PrivateFile(StorageModel):
    private_identity: Identifier
    device: Count
    inode: Count | None


@dataclass(frozen=True)
class FileSegment:
    path: tuple[str, ...]
    identity: PrivateFile
    size: int


class PayloadFiles:
    def __init__(self, root: RootAnchor) -> None:
        self.root = root

    def path(self, slot: int, semantic: str) -> tuple[str, ...]:
        return "stages", str(slot), semantic

    def create(self, slot: int, semantic: str, expected: PrivateFile) -> FileLease:
        self.root.ensure_directory(("stages", str(slot)))
        try:
            descriptor = self.root.open_file(
                self.path(slot, semantic), os.O_RDWR | os.O_CREAT | os.O_EXCL
            )
        except FileExistsError:
            descriptor = self.root.open_file(self.path(slot, semantic), os.O_RDWR)
        lease = FileLease(descriptor)
        info = os.fstat(descriptor)
        if (expected.inode is None and info.st_size != 0) or (
            expected.inode is not None
            and (info.st_ino != expected.inode or info.st_dev != expected.device)
        ):
            lease.close()
            refuse("storage_integrity")
        return lease

    def identity(self, file: FileLease, private_identity: str) -> PrivateFile:
        info = os.fstat(file.fd)
        return PrivateFile(
            version=1,
            private_identity=private_identity,
            device=info.st_dev,
            inode=info.st_ino,
        )

    def write(self, file: FileLease, chunk: bytes) -> None:
        if len(chunk) > MAX_CHUNK_BYTES:
            refuse("storage_integrity")
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(file.fd, remaining)
            if written <= 0:
                refuse("storage_io")
            remaining = remaining[written:]

    def finish(self, file: FileLease, slot: int) -> None:
        os.fsync(file.fd)
        file.close()
        directory = self.root.directory(("stages", str(slot)))
        try:
            self.root.synchronize_directory(directory)
        finally:
            os.close(directory)

    def open_segment(self, segment: FileSegment) -> FileLease:
        fd = self.root.open_file(segment.path, os.O_RDONLY, multiple_links=True)
        lease = FileLease(fd)
        info = os.fstat(fd)
        if (
            segment.identity.device != info.st_dev
            or segment.identity.inode != info.st_ino
            or segment.size != info.st_size
        ):
            lease.close()
            refuse("storage_integrity")
        return lease

    def hash_segments(self, segments: tuple[FileSegment, ...]) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        for segment in segments:
            with self.open_segment(segment) as source:
                while chunk := os.read(source.fd, MAX_CHUNK_BYTES):
                    digest.update(chunk)
                    total += len(chunk)
        return total, "sha256:" + digest.hexdigest()

    def discard(self, slot: int, semantic: str, expected: PrivateFile) -> None:
        try:
            directory = self.root.directory(("stages", str(slot)))
        except FileNotFoundError:
            return
        try:
            self.unlink_checked(directory, slot, semantic, expected)
            self.root.synchronize_directory(directory)
        finally:
            os.close(directory)

    def unlink_checked(
        self, directory: int, slot: int, semantic: str, expected: PrivateFile
    ) -> None:
        try:
            os.stat(semantic, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        with FileLease(
            self.root.open_file(
                self.path(slot, semantic), os.O_RDONLY, multiple_links=True
            )
        ) as file:
            info = os.fstat(file.fd)
            if (
                info.st_dev != expected.device
                or (expected.inode is not None and info.st_ino != expected.inode)
                or (expected.inode is None and info.st_size != 0)
            ):
                refuse("storage_integrity")
        os.unlink(semantic, dir_fd=directory)

    def discard_stage(
        self, slot: int, files: tuple[tuple[str, PrivateFile], ...]
    ) -> None:
        try:
            directory = self.root.directory(("stages", str(slot)))
        except FileNotFoundError:
            self.remove_stage_directory(slot)
            return
        try:
            for semantic, expected in files:
                self.unlink_checked(directory, slot, semantic, expected)
            self.root.synchronize_directory(directory)
        finally:
            os.close(directory)
        self.remove_stage_directory(slot)

    def remove_stage_directory(self, slot: int) -> None:
        parent = self.root.directory(("stages",))
        try:
            try:
                child = self.root.directory(("stages", str(slot)))
            except FileNotFoundError:
                self.root.synchronize_directory(parent)
                return
            os.close(child)
            os.rmdir(str(slot), dir_fd=parent)
            self.root.synchronize_directory(parent)
        finally:
            os.close(parent)
