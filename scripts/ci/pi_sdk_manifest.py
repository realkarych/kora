from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Literal, TypedDict

MAX_ENTRIES = 20_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_DEPTH = 32
CHUNK_BYTES = 1 << 20
INVENTORY_SECONDS = 15.0


class DirectoryRecord(TypedDict):
    path: str
    kind: Literal["directory"]


class FileRecord(TypedDict):
    path: str
    kind: Literal["file"]
    size: int
    sha256: str


def require_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ValueError("Pi SDK installation inventory exceeded its deadline")


def hash_file(path: Path, size: int, deadline: float) -> str:
    local = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Pi SDK installation has an invalid file")
        remaining = size
        while remaining:
            require_time(deadline)
            chunk = stream.read(min(remaining, CHUNK_BYTES))
            if not chunk:
                raise ValueError("Pi SDK file is shorter than its declared size")
            remaining -= len(chunk)
            local.update(chunk)
        if stream.read(1):
            raise ValueError("Pi SDK file exceeds its declared size")
    return local.hexdigest()


def admit(path: Path, root: Path, record_count: int) -> tuple[str, os.stat_result]:
    relative = path.relative_to(root)
    name = relative.as_posix()
    metadata = path.lstat()
    if (
        record_count >= MAX_ENTRIES
        or len(os.fsencode(name)) > MAX_PATH_BYTES
        or len(relative.parts) > MAX_DEPTH
        or any(not part.isprintable() for part in relative.parts)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise ValueError(f"Pi SDK installation entry refused ({metadata.st_mode:#o})")
    return name, metadata


def inventory(root: Path) -> list[DirectoryRecord | FileRecord]:
    records: list[DirectoryRecord | FileRecord] = []
    opened = [root]
    file_bytes = 0
    deadline = time.monotonic() + INVENTORY_SECONDS
    while opened:
        with os.scandir(opened.pop()) as entries:
            for entry in entries:
                require_time(deadline)
                path = Path(entry.path)
                name, metadata = admit(path, root, len(records))
                if stat.S_ISDIR(metadata.st_mode):
                    records.append({"path": name, "kind": "directory"})
                    opened.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError(
                        "Pi SDK installation contains a link or special file"
                    )
                file_bytes += metadata.st_size
                if file_bytes > MAX_FILE_BYTES:
                    raise ValueError("Pi SDK installation exceeds its file byte limit")
                records.append(
                    {
                        "path": name,
                        "kind": "file",
                        "size": metadata.st_size,
                        "sha256": hash_file(path, metadata.st_size, deadline),
                    }
                )
    return sorted(records, key=lambda record: record["path"])


def write_manifest(stage: Path) -> None:
    encoded = json.dumps(inventory(stage / "installed"), separators=(",", ":")).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Pi SDK installation manifest exceeds its byte limit")
    with (stage / "manifest.json").open("xb") as receipt:
        receipt.write(encoded)


if __name__ == "__main__":
    write_manifest(Path(sys.argv[1]))
