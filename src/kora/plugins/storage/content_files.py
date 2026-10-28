import errno
import os

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.files import FileLease, RootAnchor
from kora.plugins.storage.payload import (
    FileSegment,
    PayloadFiles,
    PrivateFile,
    same_file,
)
from kora.protocols.storage_models import ContentAddress


def address_lock(address: ContentAddress) -> str:
    return f"address-{int(address.payload_digest[7:9], 16) % 64}"


def content_path(address: ContentAddress) -> tuple[str, ...]:
    digest = address.payload_digest[7:]
    return "artifacts", "sha256", digest[:2], digest[2:4], digest


class ContentFiles:
    def __init__(self, root: RootAnchor) -> None:
        self.root = root
        self.files = PayloadFiles(root)

    def locate(self, address: ContentAddress) -> FileSegment | None:
        path = content_path(address)
        try:
            directory = self.root.directory(path[:-1])
        except FileNotFoundError:
            return None
        try:
            try:
                os.stat(path[-1], dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return None
            with FileLease(
                self.root.open_file(path, os.O_RDONLY, multiple_links=True)
            ) as file:
                info = os.fstat(file.fd)
                return FileSegment(
                    path,
                    PrivateFile(
                        version=1,
                        private_identity="content",
                        device=info.st_dev,
                        inode=info.st_ino,
                    ),
                    info.st_size,
                )
        finally:
            os.close(directory)

    def verify(self, segment: FileSegment, expected: str) -> None:
        size, digest = self.files.hash_segments((segment,))
        if digest != expected or size != segment.size:
            refuse("storage_integrity")

    def install(self, witness: FileSegment, address: ContentAddress) -> None:
        destination = content_path(address)
        target = self.root.directory(destination[:-1], create=True)
        try:
            source = self.root.directory(witness.path[:-1])
            try:
                with self.files.open_segment(witness):
                    os.link(
                        witness.path[-1],
                        destination[-1],
                        src_dir_fd=source,
                        dst_dir_fd=target,
                        follow_symlinks=False,
                    )
            finally:
                os.close(source)
        finally:
            os.close(target)

    def synchronize(self, address: ContentAddress, content: bool) -> None:
        if not content:
            self.prune(address)
            return
        path = content_path(address)
        directory = self.root.directory(path[:-1])
        try:
            self.root.synchronize_directory(directory)
            with FileLease(
                self.root.open_file(path, os.O_RDONLY, multiple_links=True)
            ) as file:
                os.fsync(file.fd)
        finally:
            os.close(directory)

    def prune(self, address: ContentAddress) -> None:
        path = content_path(address)
        for depth in (4, 3):
            try:
                child = self.root.directory(path[:depth])
            except FileNotFoundError:
                child = None
            if child is not None:
                try:
                    self.root.synchronize_directory(child)
                finally:
                    os.close(child)
            try:
                parent = self.root.directory(path[: depth - 1])
            except FileNotFoundError:
                continue
            try:
                try:
                    os.rmdir(path[depth - 1], dir_fd=parent)
                except FileNotFoundError:
                    self.root.synchronize_directory(parent)
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY:
                        raise
                    return
                else:
                    self.root.synchronize_directory(parent)
            finally:
                os.close(parent)

    def delete(self, address: ContentAddress, expected: FileSegment) -> None:
        actual = self.locate(address)
        if actual is None:
            return
        if (
            not same_file(actual.identity, expected.identity)
            or actual.size != expected.size
        ):
            refuse("storage_integrity")
        path = content_path(address)
        directory = self.root.directory(path[:-1])
        try:
            os.unlink(path[-1], dir_fd=directory)
        finally:
            os.close(directory)
