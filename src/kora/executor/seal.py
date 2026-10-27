import hashlib
import json
import os
import stat
import threading
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import StrictBool, StrictStr, ValidationError

from kora.executor.digests import Hasher, hasher_digest, input_digest, sha256_digest
from kora.executor.failures import ExecutorError, Phase, unavailable
from kora.executor.files import (
    DIRECTORY_FLAGS,
    descend,
    new_file,
    opened,
    private_directory,
    remove_private_directory,
    write_all,
)
from kora.ir.node import ErrorClass
from kora.protocols.common import BoundaryModel
from kora.protocols.vcs import (
    MAX_COMPONENT_BYTES,
    MAX_PATH_TEXT_BYTES,
    PublicationInput,
)

SEALED_FORMAT: Final[str] = "kora.git.v1"
HEAD_FILE: Final[str] = "head.json"
BUNDLE_FILE: Final[str] = "head.bundle"
MANIFEST_FILE: Final[str] = "manifest.jsonl"
TREE_FILE: Final[str] = "tree.jsonl"
CHUNK_PREFIX: Final[str] = "tree."
CHUNK_SUFFIX: Final[str] = ".bin"
EXPORT_DIRECTORY: Final[str] = ".kora/export"
MIB: Final[int] = 1_048_576
GIB: Final[int] = 1024 * MIB
READ_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
REGULAR_MODE: Final[str] = "100644"
EXECUTABLE_MODE: Final[str] = "100755"
SYMLINK_MODE: Final[str] = "120000"
GITLINK_MODE: Final[str] = "160000"
RESERVED_NAMES: Final[frozenset[str]] = frozenset({".git", ".kora", "git~1", "kora~1"})
HFS_IGNORABLE: Final[frozenset[str]] = frozenset("‌‍‎‏‪‫‬‭‮⁪⁫⁬⁭⁮⁯﻿")
OBJECT_ID_LENGTHS: Final[frozenset[int]] = frozenset({40, 64})
HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")
MAX_HEAD_BYTES: Final[int] = 4096
PATH_REFUSED: Final[str] = "sealed_path_refused"


@dataclass(frozen=True)
class SealBounds:
    max_bundle_bytes: int = 512 * MIB
    max_manifest_bytes: int = 64 * MIB
    max_records: int = 50_000
    max_tree_bytes: int = 2 * GIB
    max_chunk_bytes: int = GIB
    max_file_bytes: int = 256 * MIB


@dataclass(frozen=True)
class ExportResult:
    format: str
    directory: str


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    gitlink: str | None


@dataclass
class SealedWriter:
    directory: int
    bounds: SealBounds
    cancelled: threading.Event
    chunk: int = 0
    chunk_size: int = 0
    total: int = 0
    chunk_descriptor: int = -1
    chunk_digests: list[str] = field(default_factory=lambda: [])
    chunk_hash: Hasher | None = None
    records: list[str] = field(default_factory=lambda: [])


def refused(code: str = PATH_REFUSED) -> ExecutorError:
    return ExecutorError(ErrorClass.ARTIFACT_INVALID, code, Phase.SEAL)


def is_object_id(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) in OBJECT_ID_LENGTHS and set(value) <= HEX
    )


def reserved_component(component: str) -> bool:
    visible = "".join(
        character for character in component if character not in HFS_IGNORABLE
    )
    stream = visible.split(":", 1)[0]
    trimmed = stream.rstrip(". ")
    return unicodedata.normalize("NFC", trimmed).casefold() in RESERVED_NAMES


def path_allowed(path: str) -> bool:
    if (
        not path
        or path.startswith("/")
        or len(path.encode("utf-8")) > (MAX_PATH_TEXT_BYTES)
    ):
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        return False
    components = path.split("/")
    return all(
        component not in ("", ".", "..")
        and len(component.encode("utf-8")) <= MAX_COMPONENT_BYTES
        and not reserved_component(component)
        for component in components
    )


def checked_paths(entries: Sequence[ManifestEntry]) -> None:
    exact: set[str] = set()
    folded: set[str] = set()
    for entry in entries:
        if not path_allowed(entry.path):
            raise refused()
        key = unicodedata.normalize("NFC", entry.path).casefold()
        if entry.path in exact or key in folded:
            raise refused("sealed_path_duplicate")
        exact.add(entry.path)
        folded.add(key)
        if entry.gitlink is not None and not is_object_id(entry.gitlink):
            raise refused("sealed_gitlink_invalid")


@contextmanager
def opened_regular(
    directory: int, name: str, code: str
) -> Generator[tuple[int, os.stat_result]]:
    try:
        descriptor = os.open(name, READ_FLAGS, dir_fd=directory)
    except OSError:
        raise refused(code) from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise refused(code)
        yield descriptor, status
    finally:
        os.close(descriptor)


def read_regular(directory: int, name: str, limit: int, code: str) -> bytes:
    with opened_regular(directory, name, code) as (descriptor, status):
        if status.st_size > limit:
            raise refused(code)
        data = os.read(descriptor, limit + 1)
        if len(data) > limit:
            raise refused(code)
        return data


class ExportedHead(BoundaryModel):
    head: StrictStr
    bundle: StrictBool


class ExportedEntry(BoundaryModel):
    path: StrictStr
    gitlink: StrictStr | None


def manifest_entries(data: bytes, bounds: SealBounds) -> list[ManifestEntry]:
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) > bounds.max_records:
        raise refused("sealed_manifest_oversized")
    entries: list[ManifestEntry] = []
    for line in lines:
        try:
            record = ExportedEntry.model_validate_json(line)
        except ValidationError:
            raise refused("sealed_manifest_malformed") from None
        entries.append(ManifestEntry(record.path, record.gitlink))
    return entries


def head_document(data: bytes) -> tuple[str, bool]:
    try:
        record = ExportedHead.model_validate_json(data)
    except ValidationError:
        raise refused("sealed_head_malformed") from None
    if not is_object_id(record.head):
        raise refused("sealed_head_malformed")
    return record.head, record.bundle


def write_file(directory: int, name: str, data: bytes) -> str:
    descriptor = new_file(directory, name)
    try:
        write_all(descriptor, data)
    finally:
        os.close(descriptor)
    return sha256_digest(data)


def copy_stream(
    source: int,
    target: int,
    limit: int,
    cancelled: threading.Event,
    digests: Sequence[Hasher],
) -> int:
    copied = 0
    while True:
        if cancelled.is_set():
            unavailable("sealing_cancelled", Phase.SEAL)
        block = os.read(source, MIB)
        if not block:
            return copied
        copied += len(block)
        if copied > limit:
            raise refused("sealed_file_oversized")
        for digest in digests:
            digest.update(block)
        write_all(target, block)


def close_chunk(writer: SealedWriter) -> None:
    if writer.chunk_descriptor >= 0 and writer.chunk_hash is not None:
        os.close(writer.chunk_descriptor)
        writer.chunk_digests.append(hasher_digest(writer.chunk_hash))
        writer.chunk_descriptor = -1
        writer.chunk_hash = None


def chunk_for(writer: SealedWriter, size: int) -> int:
    if writer.chunk_descriptor < 0 or (
        writer.chunk_size + size > writer.bounds.max_chunk_bytes
    ):
        if writer.chunk_descriptor >= 0:
            close_chunk(writer)
            writer.chunk += 1
        writer.chunk_descriptor = new_file(
            writer.directory, f"{CHUNK_PREFIX}{writer.chunk}{CHUNK_SUFFIX}"
        )
        writer.chunk_hash = hashlib.sha256()
        writer.chunk_size = 0
    return writer.chunk_descriptor


def record_text(
    path: str,
    mode: str,
    size: int,
    digest: str | None,
    position: tuple[int, int] | None,
    native: str | None,
) -> str:
    return json.dumps(
        {
            "path": path,
            "mode": mode,
            "size": size,
            "sha256": digest,
            "chunk": None if position is None else position[0],
            "offset": None if position is None else position[1],
            "object": native,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def charge(writer: SealedWriter, size: int) -> None:
    writer.total += size
    if size > writer.bounds.max_file_bytes:
        raise refused("sealed_file_oversized")
    if writer.total > writer.bounds.max_tree_bytes:
        raise refused("sealed_tree_oversized")


def copy_into_chunk(
    writer: SealedWriter, source: int | bytes, size: int
) -> tuple[tuple[int, int], str]:
    target = chunk_for(writer, size)
    position = (writer.chunk, writer.chunk_size)
    file_hash = hashlib.sha256()
    hashes: list[Hasher] = [file_hash]
    if writer.chunk_hash is not None:
        hashes.append(writer.chunk_hash)
    if isinstance(source, bytes):
        for digest in hashes:
            digest.update(source)
        write_all(target, source)
        copied = len(source)
    else:
        copied = copy_stream(
            source, target, writer.bounds.max_file_bytes, writer.cancelled, hashes
        )
    if copied != size:
        raise refused("sealed_input_changed")
    writer.chunk_size += copied
    return position, hasher_digest(file_hash)


def seal_symlink(writer: SealedWriter, parent: int, name: str, path: str) -> None:
    target = os.readlink(name.encode("utf-8", "surrogateescape"), dir_fd=parent)
    charge(writer, len(target))
    position, digest = copy_into_chunk(writer, target, len(target))
    writer.records.append(
        record_text(path, SYMLINK_MODE, len(target), digest, position, None)
    )


def seal_regular(
    writer: SealedWriter, parent: int, name: str, path: str, device: int
) -> None:
    with opened_regular(parent, name, PATH_REFUSED) as (source, status):
        if status.st_dev != device:
            raise refused()
        charge(writer, status.st_size)
        mode = EXECUTABLE_MODE if status.st_mode & stat.S_IXUSR else REGULAR_MODE
        position, digest = copy_into_chunk(writer, source, status.st_size)
        writer.records.append(
            record_text(path, mode, status.st_size, digest, position, None)
        )


def seal_entry(
    writer: SealedWriter, root: int, entry: ManifestEntry, device: int
) -> None:
    if entry.gitlink is not None:
        writer.records.append(
            record_text(entry.path, GITLINK_MODE, 0, None, None, entry.gitlink)
        )
        return
    parts = entry.path.split("/")
    try:
        parent = descend(root, parts[:-1], create=False)
    except OSError:
        raise refused() from None
    try:
        status = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if status.st_dev != device:
            raise refused()
        if stat.S_ISLNK(status.st_mode):
            seal_symlink(writer, parent, parts[-1], entry.path)
        elif stat.S_ISREG(status.st_mode):
            seal_regular(writer, parent, parts[-1], entry.path, device)
        else:
            raise refused("sealed_special_file")
    except FileNotFoundError:
        raise refused() from None
    finally:
        os.close(parent)


def write_sealed(
    candidate: Path,
    sealed: Path,
    bounds: SealBounds,
    cancelled: threading.Event,
) -> list[str]:
    with opened(candidate) as root, opened(sealed) as output:
        device = os.fstat(root).st_dev
        try:
            export = descend(root, EXPORT_DIRECTORY.split("/"), create=False)
        except OSError:
            raise refused("sealed_export_missing") from None
        try:
            head, bundle = head_document(
                read_regular(export, HEAD_FILE, MAX_HEAD_BYTES, "sealed_head_malformed")
            )
            manifest = read_regular(
                export, MANIFEST_FILE, bounds.max_manifest_bytes, "sealed_manifest"
            )
            digests = [
                write_file(
                    output,
                    HEAD_FILE,
                    json.dumps({"bundle": bundle, "head": head}).encode("ascii"),
                )
            ]
            if bundle:
                digests.append(copy_bundle(export, output, bounds, cancelled))
        finally:
            os.close(export)
        entries = manifest_entries(manifest, bounds)
        checked_paths(entries)
        writer = SealedWriter(output, bounds, cancelled)
        try:
            for entry in entries:
                seal_entry(writer, root, entry, device)
        finally:
            close_chunk(writer)
        tree = "".join(record + "\n" for record in writer.records).encode("utf-8")
        digests.append(write_file(output, TREE_FILE, tree))
        return [*digests, *writer.chunk_digests]


def copy_bundle(
    export: int, output: int, bounds: SealBounds, cancelled: threading.Event
) -> str:
    with opened_regular(export, BUNDLE_FILE, "sealed_bundle_missing") as (
        source,
        status,
    ):
        if status.st_size > bounds.max_bundle_bytes:
            raise refused("sealed_bundle_oversized")
        target = new_file(output, BUNDLE_FILE)
        digest = hashlib.sha256()
        try:
            copy_stream(source, target, bounds.max_bundle_bytes, cancelled, [digest])
        finally:
            os.close(target)
        return hasher_digest(digest)


def seal_sync(
    attempt_id: str,
    candidate: Path,
    publication: Path,
    bounds: SealBounds,
    cancelled: threading.Event,
) -> PublicationInput:
    sealed = private_directory(publication, "input")
    try:
        digests = write_sealed(candidate, sealed, bounds, cancelled)
        if cancelled.is_set():
            unavailable("sealing_cancelled", Phase.SEAL)
        return PublicationInput(
            attempt_id=attempt_id,
            format=SEALED_FORMAT,
            location=str(sealed),
            digest=input_digest(digests),
        )
    except (ExecutorError, ValidationError, OSError) as error:
        remove_private_directory(sealed)
        if isinstance(error, ExecutorError):
            raise
        unavailable("publication_input_unavailable", Phase.SEAL)


def sealed_names(directory: int, bundle: bool) -> list[str]:
    names = [HEAD_FILE, *([BUNDLE_FILE] if bundle else []), TREE_FILE]
    chunks = sorted(
        int(name[len(CHUNK_PREFIX) : -len(CHUNK_SUFFIX)])
        for name in os.listdir(directory)
        if name.startswith(CHUNK_PREFIX)
        and name.endswith(CHUNK_SUFFIX)
        and name[len(CHUNK_PREFIX) : -len(CHUNK_SUFFIX)].isdigit()
    )
    if chunks != list(range(len(chunks))):
        unavailable("publication_input_unavailable", Phase.SEAL)
    return names + [f"{CHUNK_PREFIX}{index}{CHUNK_SUFFIX}" for index in chunks]


def file_digest(
    directory: int, name: str, limit: int, cancelled: threading.Event
) -> str:
    descriptor = os.open(name, READ_FLAGS, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            unavailable("publication_input_unavailable", Phase.SEAL)
        digest = hashlib.sha256()
        size = 0
        while block := os.read(descriptor, MIB):
            if cancelled.is_set():
                unavailable("publication_input_unavailable", Phase.SEAL)
            size += len(block)
            if size > limit:
                unavailable("publication_input_unavailable", Phase.SEAL)
            digest.update(block)
        return hasher_digest(digest)
    finally:
        os.close(descriptor)


def verify_sync(
    sealed: PublicationInput, publication: Path, cancelled: threading.Event
) -> PublicationInput:
    location = Path(sealed.location)
    if sealed.format != SEALED_FORMAT or location.parent != publication:
        unavailable("publication_input_unavailable", Phase.SEAL)
    try:
        with opened(publication) as parent:
            directory = os.open(location.name, DIRECTORY_FLAGS, dir_fd=parent)
        try:
            _, bundle = head_document(
                read_regular(directory, HEAD_FILE, MAX_HEAD_BYTES, "sealed_head")
            )
            names = sealed_names(directory, bundle)
            digests = [
                file_digest(directory, name, 4 * GIB, cancelled) for name in names
            ]
        finally:
            os.close(directory)
    except (OSError, ExecutorError):
        unavailable("publication_input_unavailable", Phase.SEAL)
    if input_digest(digests) != sealed.digest:
        unavailable("publication_input_unavailable", Phase.SEAL)
    return sealed
