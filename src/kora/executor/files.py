import os
import secrets
import shutil
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from kora.executor.failures import Phase, misconfigured, unavailable
from kora.protocols.vcs import PlanFile

DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
NEW_FILE_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
PRIVATE_MODE: Final[int] = 0o700
NAME_BYTES: Final[int] = 8


@contextmanager
def opened(path: Path) -> Generator[int]:
    descriptor = os.open(path, DIRECTORY_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def new_file(directory: int, name: str) -> int:
    return os.open(name, NEW_FILE_FLAGS, 0o600, dir_fd=directory)


def path_parts(relative: str) -> list[str]:
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("the path is not a normal relative path")
    return parts


def descend(directory: int, parts: Sequence[str], create: bool) -> int:
    current = os.dup(directory)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, PRIVATE_MODE, dir_fd=current)
                except FileExistsError:
                    pass
            following = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except OSError:
        os.close(current)
        raise
    return current


def write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def write_new_file(directory: int, relative: str, content: bytes, mode: int) -> None:
    parts = path_parts(relative)
    parent = descend(directory, parts[:-1], create=True)
    try:
        descriptor = new_file(parent, parts[-1])
    finally:
        os.close(parent)
    try:
        write_all(descriptor, content)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def write_plan_files(
    files: Sequence[PlanFile],
    control: Path | None,
    publication: Path | None,
    phase: Phase,
) -> None:
    for plan_file in files:
        root = control if plan_file.area == "control" else publication
        if root is None:
            misconfigured("plan_file_area_refused", phase)
        with opened(root) as directory:
            try:
                write_new_file(
                    directory, plan_file.path, plan_file.content, plan_file.mode
                )
            except (OSError, ValueError):
                unavailable("plan_file_refused", phase)


def private_directory(root: Path, prefix: str) -> Path:
    with opened(root) as directory:
        name = f"{prefix}-{secrets.token_hex(NAME_BYTES)}"
        os.mkdir(name, PRIVATE_MODE, dir_fd=directory)
    return root / name


def remove_private_directory(path: Path) -> None:
    with opened(path.parent) as directory:
        try:
            shutil.rmtree(path.name, dir_fd=directory)
        except FileNotFoundError:
            return


@contextmanager
def scratch_directory(root: Path, prefix: str) -> Generator[Path]:
    path = private_directory(root, prefix)
    try:
        yield path
    finally:
        remove_private_directory(path)
