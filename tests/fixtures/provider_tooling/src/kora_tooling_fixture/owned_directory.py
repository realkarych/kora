import os
import shutil
import stat
from pathlib import Path

from kora.runner.filesystem import DirectoryAnchor

Identity = tuple[int, int, int]


def directory_identity(descriptor: int) -> Identity:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino, metadata.st_uid


def dispose_owned_directory(root: Path, identity: Identity) -> None:
    parent = DirectoryAnchor.open_path(root.absolute().parent)
    try:
        descriptor = os.open(
            root.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent.descriptor,
        )
        try:
            assert directory_identity(descriptor) == identity
            links: dict[tuple[int, int], tuple[int, int]] = {}
            remaining = 65536
            for _, directories, files, current in os.fwalk(
                ".", dir_fd=descriptor, follow_symlinks=False
            ):
                for name in (*directories, *files):
                    remaining -= 1
                    assert remaining >= 0
                    metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
                    assert metadata.st_uid == os.getuid()
                    assert stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(
                        metadata.st_mode
                    )
                    if stat.S_ISREG(metadata.st_mode):
                        key = (metadata.st_dev, metadata.st_ino)
                        previous, count = links.get(key, (metadata.st_nlink, 0))
                        assert previous == metadata.st_nlink
                        links[key] = (previous, count + 1)
            assert all(count == expected for expected, count in links.values())
            metadata = os.stat(
                root.name, dir_fd=parent.descriptor, follow_symlinks=False
            )
            assert stat.S_ISDIR(metadata.st_mode)
            assert (metadata.st_dev, metadata.st_ino, metadata.st_uid) == identity
            assert shutil.rmtree.avoids_symlink_attacks
            shutil.rmtree(root.name, dir_fd=parent.descriptor)
            parent.verify()
        finally:
            os.close(descriptor)
    finally:
        parent.close()
