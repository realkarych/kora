import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from kora.ir.vcs_ref_json import JsonRefusal, decode_strict_json
from kora.protocols.boundary import BoundaryModel
from kora.protocols.time import Clock
from kora.runner.filesystem import DirectoryAnchor, FilesystemError

MAX_ENTRIES = 20_000
MAX_RECEIPT = 4 * 1024 * 1024
MAX_INSTALLATION = 512 * 1024 * 1024
DIRECTORY = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_DIRECTORY
FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
Stamp = tuple[int, int, int, int, int, int, int, int]


class InstallationRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("pi_installation_refused")


class _Directory(BoundaryModel):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Literal["directory"]


class _File(BoundaryModel):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Literal["file"]
    size: Annotated[int, Field(strict=True, ge=0, le=MAX_INSTALLATION)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


_Entry = Annotated[_Directory | _File, Field(discriminator="kind")]
_Manifest: TypeAdapter[list[_Entry]] = TypeAdapter(
    Annotated[list[_Entry], Field(max_length=MAX_ENTRIES)]
)


def _stamp(metadata: os.stat_result) -> Stamp:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _safe(metadata: os.stat_result, *, directory: bool) -> None:
    if (
        metadata.st_uid != os.getuid()
        or metadata.st_mode
        & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or (
            not stat.S_ISDIR(metadata.st_mode)
            if directory
            else not stat.S_ISREG(metadata.st_mode)
        )
        or (not directory and metadata.st_nlink != 1)
    ):
        raise InstallationRefused()


def _before(cutoff_ns: int, clock: Clock) -> None:
    if clock.monotonic_ns() >= cutoff_ns:
        raise InstallationRefused()


def _entries(root: int) -> dict[str, _Directory | _File]:
    descriptor = os.open("manifest.json", FILE, dir_fd=root)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        _safe(metadata, directory=False)
        if not 0 < metadata.st_size <= MAX_RECEIPT:
            raise InstallationRefused()
        payload = source.read(MAX_RECEIPT + 1)
        if len(payload) != metadata.st_size or _stamp(os.fstat(descriptor)) != _stamp(
            metadata
        ):
            raise InstallationRefused()
    decoded = decode_strict_json(payload, MAX_RECEIPT, 4)
    if isinstance(decoded, JsonRefusal):
        raise InstallationRefused()
    try:
        entries = _Manifest.validate_python(decoded)
    except ValidationError:
        raise InstallationRefused() from None
    names: dict[str, _Directory | _File] = {}
    total = 0
    for entry in entries:
        path = PurePosixPath(entry.path)
        if (
            len(path.parts) > 32
            or not path.parts
            or path.is_absolute()
            or path.as_posix() != entry.path
            or ".." in path.parts
            or entry.path in names
            or any(ord(character) < 32 for character in entry.path)
        ):
            raise InstallationRefused()
        names[entry.path] = entry
        if isinstance(entry, _File):
            total += entry.size
    if total > MAX_INSTALLATION:
        raise InstallationRefused()
    return names


def _verify_file(
    descriptor: int, entry: _File, stamp: Stamp, cutoff_ns: int, clock: Clock
) -> None:
    checksum = hashlib.sha256()
    remaining = entry.size
    while remaining:
        _before(cutoff_ns, clock)
        chunk = os.read(descriptor, min(256 * 1024, remaining))
        if not chunk:
            raise InstallationRefused()
        remaining -= len(chunk)
        checksum.update(chunk)
    if (
        os.read(descriptor, 1)
        or _stamp(os.fstat(descriptor)) != stamp
        or checksum.hexdigest() != entry.sha256
    ):
        raise InstallationRefused()


def _walk(
    directory: int,
    prefix: str,
    remaining: dict[str, _Directory | _File],
    stamps: dict[str, Stamp],
    expected_stamps: dict[str, Stamp] | None,
    cutoff_ns: int,
    clock: Clock,
) -> None:
    with os.scandir(directory) as children:
        for child in children:
            _before(cutoff_ns, clock)
            name = prefix + child.name
            expected = remaining.pop(name, None)
            if expected is None:
                raise InstallationRefused()
            is_directory = isinstance(expected, _Directory)
            descriptor = os.open(
                child.name, DIRECTORY if is_directory else FILE, dir_fd=directory
            )
            try:
                metadata = os.fstat(descriptor)
                _safe(metadata, directory=is_directory)
                stamp = _stamp(metadata)
                if expected_stamps is not None and expected_stamps.get(name) != stamp:
                    raise InstallationRefused()
                if isinstance(expected, _Directory):
                    _walk(
                        descriptor,
                        name + "/",
                        remaining,
                        stamps,
                        expected_stamps,
                        cutoff_ns,
                        clock,
                    )
                elif metadata.st_size != expected.size:
                    raise InstallationRefused()
                elif expected_stamps is None:
                    _verify_file(descriptor, expected, stamp, cutoff_ns, clock)
                if (
                    _stamp(os.stat(child.name, dir_fd=directory, follow_symlinks=False))
                    != stamp
                ):
                    raise InstallationRefused()
                stamps[name] = stamp
            finally:
                os.close(descriptor)


@dataclass(frozen=True, repr=False)
class PiInstallation:
    root: Path
    stamp: Stamp
    entries: dict[str, _Directory | _File]
    stamps: dict[str, Stamp]

    @property
    def node(self) -> Path:
        return self.root / "installed/node"

    @property
    def bridge(self) -> Path:
        return self.root / "installed/bridge/dist/bridge.js"

    def recheck(self, cutoff_ns: int, clock: Clock) -> None:
        if (
            _inspect(self.root, self.entries, self.stamps, cutoff_ns, clock)[0]
            != self.stamp
        ):
            raise InstallationRefused()


def _inspect(
    root: Path,
    entries: dict[str, _Directory | _File] | None,
    expected_stamps: dict[str, Stamp] | None,
    cutoff_ns: int,
    clock: Clock,
) -> tuple[Stamp, dict[str, _Directory | _File], dict[str, Stamp]]:
    anchor: DirectoryAnchor | None = None
    try:
        _before(cutoff_ns, clock)
        anchor = DirectoryAnchor.open_path(root)
        stamp = _stamp(os.fstat(anchor.descriptor))
        for parent in (root / "installed", root, *root.parents):
            if os.path.lexists(parent / "node_modules"):
                raise InstallationRefused()
        captured = _entries(anchor.descriptor) if entries is None else entries
        remaining = captured.copy()
        stamps: dict[str, Stamp] = {}
        descriptor = os.open("installed", DIRECTORY, dir_fd=anchor.descriptor)
        try:
            metadata = os.fstat(descriptor)
            _safe(metadata, directory=True)
            installed = _stamp(metadata)
            _walk(descriptor, "", remaining, stamps, expected_stamps, cutoff_ns, clock)
            if (
                remaining
                or _stamp(
                    os.stat(
                        "installed", dir_fd=anchor.descriptor, follow_symlinks=False
                    )
                )
                != installed
            ):
                raise InstallationRefused()
        finally:
            os.close(descriptor)
        if (
            _stamp(root.stat(follow_symlinks=False)) != stamp
            or "bridge/dist/bridge.js" not in stamps
            or not isinstance(captured.get("node"), _File)
            or not isinstance(captured.get("bridge/dist/bridge.js"), _File)
            or not stamps["node"][2] & stat.S_IXUSR
        ):
            raise InstallationRefused()
        _before(cutoff_ns, clock)
        return stamp, captured, stamps
    except (OSError, FilesystemError, ValueError):
        raise InstallationRefused() from None
    finally:
        if anchor is not None:
            anchor.close()


def verify_pi_installation(root: Path, cutoff_ns: int, clock: Clock) -> PiInstallation:
    stamp, entries, stamps = _inspect(root, None, None, cutoff_ns, clock)
    return PiInstallation(root, stamp, entries, stamps)
