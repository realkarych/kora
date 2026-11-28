import asyncio
import os
import re
import signal
import stat
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn

from kora.plugins.runtime.local.bound_checkout import BoundCheckout, BoundCheckoutError
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.vcs.git.environment import git_environment, trusted_options
from kora.protocols.errors import KoraProtocolError
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessRegistration,
    new_operation_id,
)
from kora.protocols.time import Clock
from kora.runner.channels import ChannelError
from kora.runner.filesystem import (
    FilesystemError,
    identity_of,
    remove_directory,
)
from kora.runner.processes import GatedProcess, ProcessError, start_gated

FIXTURE_TIMEOUT_NS: Final[int] = 30_000_000_000
_CLEANUP_NS: Final[int] = 5_000_000_000
_TRANSFER_LIMIT: Final[int] = 65_536
_HEADER_LIMIT: Final[int] = 128
_CANDIDATE_ENTRIES: Final[int] = 256
_OBJECT_TYPES: Final[tuple[str, ...]] = ("commit", "tree", "blob", "blob")
_SEED_FILES: Final[tuple[str, ...]] = ("seed.txt", "script.json")


class SeedCheckoutPhase(StrEnum):
    PREPARED = "prepared"
    ACTIVATED = "activated"
    SOURCE_SETTLED = "source_settled"


class SeedCheckoutFailure(StrEnum):
    BINDING = "testing_seed_binding_invalid"
    TRANSFER = "testing_seed_transfer_invalid"
    CLEANUP = "testing_seed_cleanup_unresolved"


SeedSourceObserver = Callable[[SeedCheckoutPhase, ProcessRegistration], Awaitable[None]]


def _refuse(reason: SeedCheckoutFailure) -> NoReturn:
    raise KoraProtocolError(
        reason.value, "exact testing checkout refused", False, {}
    ) from None


def _require_current(current: Callable[[], bool]) -> None:
    if not current():
        _refuse(SeedCheckoutFailure.BINDING)


@contextmanager
def _directory(parent: int, name: str) -> Generator[int]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
    )
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_dev != os.fstat(parent).st_dev:
            raise FilesystemError("seed_store_redirected")
        yield descriptor
    finally:
        os.close(descriptor)


def _require_file(metadata: os.stat_result, limit: int | None = None) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or (limit is not None and metadata.st_size > limit)
    ):
        raise FilesystemError("invalid_seed_file")


def _read_file(parent: int, name: str, limit: int) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent
    )
    with os.fdopen(descriptor, "rb") as source:
        _require_file(os.fstat(source.fileno()), limit)
        result = source.read(limit + 1)
        if len(result) > limit:
            raise FilesystemError("invalid_seed_file")
        return result


def _require_absent(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise FilesystemError("seed_store_redirected")


def _require_regular_object(objects: int, oid: str, *, required: bool = False) -> None:
    try:
        with _directory(objects, oid[:2]) as fanout:
            metadata = os.stat(oid[2:], dir_fd=fanout, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise
        return
    _require_file(metadata)


async def _read_objects(
    reader: asyncio.StreamReader, object_ids: tuple[str, ...]
) -> tuple[bytes, ...]:
    remaining = _TRANSFER_LIMIT
    objects: list[bytes] = []
    for object_id, kind in zip(object_ids, _OBJECT_TYPES, strict=True):
        header = await reader.readuntil(b"\n")
        prefix = f"{object_id} {kind} ".encode("ascii")
        if not header.startswith(prefix):
            _refuse(SeedCheckoutFailure.TRANSFER)
        length = header[len(prefix) : -1]
        if re.fullmatch(rb"0|[1-9][0-9]{0,4}", length) is None:
            _refuse(SeedCheckoutFailure.TRANSFER)
        size = int(length)
        remaining -= len(header) + size + 1
        if remaining < 0:
            _refuse(SeedCheckoutFailure.TRANSFER)
        objects.append(await reader.readexactly(size))
        if await reader.readexactly(1) != b"\n":
            _refuse(SeedCheckoutFailure.TRANSFER)
    if await reader.read(1):
        _refuse(SeedCheckoutFailure.TRANSFER)
    return tuple(objects)


async def _send(gate: GatedProcess, data: bytes) -> asyncio.StreamReader:
    reader, writer = gate.borrow_stdio()
    writer.write(data)
    await writer.drain()
    writer.write_eof()
    return reader


class SeedCheckout:
    def __init__(
        self,
        root: BoundCheckout,
        source: BoundCheckout,
        git: str,
        object_format: str,
        object_ids: tuple[str, ...],
        clock: Clock,
    ) -> None:
        width = {"sha1": 40, "sha256": 64}.get(object_format)
        if (
            width is None
            or len(object_ids) != len(_OBJECT_TYPES)
            or len(set(object_ids)) != len(_OBJECT_TYPES)
            or any(
                len(oid) != width or re.fullmatch(r"[0-9a-f]+", oid) is None
                for oid in object_ids
            )
        ):
            _refuse(SeedCheckoutFailure.BINDING)
        self.root = root
        self.source = source
        self.git = git
        self.object_format = object_format
        self.object_ids = object_ids
        self.argv = (git, "--git-dir=.", "cat-file", "--batch")
        self._clock = clock
        try:
            descriptor = source.anchor.descriptor
            with _directory(descriptor, "objects") as objects:
                self._objects = identity_of(objects, "objects")
                for oid in object_ids:
                    _require_regular_object(objects, oid, required=True)
            self._config = _read_file(descriptor, "config", 4096)
        except (FilesystemError, OSError):
            _refuse(SeedCheckoutFailure.BINDING)
        self._wait_owner = AdoptedReaper()
        self._process: GatedProcess | None = None
        self._activation_attempted = False
        self._candidate: BoundCheckout | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._active = False
        self._closing = False

    def environment(self) -> dict[str, str]:
        options = (
            *trusted_options(str(self.root.path / "hooks"), ()),
            ("core.warnAmbiguousRefs", "false"),
        )
        return {
            **git_environment(self.git, str(self.root.path / "home"), options),
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }

    def _verify_source(self, current: Callable[[], bool], cutoff: int) -> None:
        _require_current(current)
        try:
            self.source.verify(self.source.path, cutoff, self._clock)
            descriptor = self.source.anchor.descriptor
            if _read_file(descriptor, "config", 4096) != self._config:
                _refuse(SeedCheckoutFailure.BINDING)
            _require_file(
                os.stat("HEAD", dir_fd=descriptor, follow_symlinks=False), 256
            )
            _require_absent(descriptor, "commondir")
            with _directory(descriptor, "objects") as objects:
                if identity_of(objects, "objects") != self._objects:
                    _refuse(SeedCheckoutFailure.BINDING)
                with _directory(objects, "info") as info:
                    _require_absent(info, "alternates")
                    _require_absent(info, "http-alternates")
                with _directory(objects, "pack") as pack, os.scandir(pack) as entries:
                    if next(entries, None) is not None:
                        _refuse(SeedCheckoutFailure.BINDING)
                for oid in self.object_ids:
                    _require_regular_object(objects, oid)
        except (BoundCheckoutError, FilesystemError, OSError):
            _refuse(SeedCheckoutFailure.BINDING)

    @asynccontextmanager
    async def prepare(
        self,
        path: Path,
        current: Callable[[], bool],
        observe_source: SeedSourceObserver | None,
    ) -> AsyncGenerator[BoundCheckout]:
        if (
            self._active
            or self._closing
            or self._process is not None
            or self._candidate is not None
        ):
            _refuse(SeedCheckoutFailure.BINDING)
        self._active = True
        cutoff = self._clock.monotonic_ns() + FIXTURE_TIMEOUT_NS
        try:
            try:
                async with asyncio.timeout(FIXTURE_TIMEOUT_NS / 1_000_000_000):
                    candidate = await self._prepare(
                        path, current, cutoff, observe_source
                    )
            except (
                BoundCheckoutError,
                ChannelError,
                FilesystemError,
                ProcessError,
                OSError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                TimeoutError,
            ):
                _refuse(SeedCheckoutFailure.TRANSFER)
            yield candidate
        finally:
            try:
                await self._release()
            finally:
                self._active = False

    async def _prepare(
        self,
        path: Path,
        current: Callable[[], bool],
        cutoff: int,
        observe_source: SeedSourceObserver | None,
    ) -> BoundCheckout:
        self._verify_source(current, cutoff)
        gate = self._start(self.source, self.argv, cutoff)
        registration = await gate.prepare()
        if observe_source is not None:
            await observe_source(SeedCheckoutPhase.PREPARED, registration)
        self._verify_source(current, cutoff)
        self._activation_attempted = True
        await gate.acknowledge()
        if observe_source is not None:
            await observe_source(SeedCheckoutPhase.ACTIVATED, registration)
        self._verify_source(current, cutoff)
        reader = await _send(gate, ("\n".join(self.object_ids) + "\n").encode("ascii"))
        objects = await _read_objects(reader, self.object_ids)
        await self._settle(gate, cutoff)
        self._verify_source(current, cutoff)
        if observe_source is not None:
            await observe_source(SeedCheckoutPhase.SOURCE_SETTLED, registration)
            self._verify_source(current, cutoff)
        self.root.verify(self.root.path, cutoff, self._clock)
        candidate = self.root.create_child(path)
        self._candidate = candidate
        await self._checkout(candidate, objects, cutoff, current)
        self._verify_source(current, cutoff)
        return candidate

    def _start(
        self, directory: BoundCheckout, argv: tuple[str, ...], cutoff: int
    ) -> GatedProcess:
        directory.verify(directory.path, cutoff, self._clock)
        operation = new_operation_id()
        self._activation_attempted = False
        self._process = start_gated(
            ProcessLaunch(
                executable=self.git,
                argv=argv,
                environment=tuple(
                    EnvironmentVariable(name=key, value=value)
                    for key, value in self.environment().items()
                ),
                working_directory_resource_id=operation,
                stdin=None,
                stdout=None,
                stderr=None,
                domain=ProcessDomain.INVOCATION,
            ),
            operation,
            directory.anchor.descriptor,
            cutoff,
            self._clock,
            stdio_limit=_HEADER_LIMIT,
            wait_owner=self._wait_owner,
        )
        return self._process

    async def _settle(self, gate: GatedProcess, cutoff: int) -> None:
        await gate.settle(cutoff)
        await gate.drain(cutoff)
        status = gate.release(cutoff)
        if not gate.closed or gate.gate_close_failed:
            _refuse(SeedCheckoutFailure.CLEANUP)
        self._process = None
        if status != 0:
            _refuse(SeedCheckoutFailure.TRANSFER)

    async def _command(
        self,
        candidate: BoundCheckout,
        cutoff: int,
        current: Callable[[], bool],
        *arguments: str,
        data: bytes = b"",
    ) -> bytes:
        _require_current(current)
        gate = self._start(candidate, (self.git, *arguments), cutoff)
        await gate.prepare()
        candidate.verify(candidate.path, cutoff, self._clock)
        _require_current(current)
        self._activation_attempted = True
        await gate.acknowledge()
        _require_current(current)
        reader = await _send(gate, data)
        output = bytearray()
        while chunk := await reader.read(4097 - len(output)):
            output.extend(chunk)
            if len(output) > 4096:
                _refuse(SeedCheckoutFailure.TRANSFER)
        await self._settle(gate, cutoff)
        return bytes(output)

    async def _checkout(
        self,
        candidate: BoundCheckout,
        objects: tuple[bytes, ...],
        cutoff: int,
        current: Callable[[], bool],
    ) -> None:
        await self._command(
            candidate,
            cutoff,
            current,
            "init",
            "--template=",
            "--shared=0600",
            f"--object-format={self.object_format}",
        )
        for oid, kind, body in zip(
            self.object_ids, _OBJECT_TYPES, objects, strict=True
        ):
            observed = await self._command(
                candidate,
                cutoff,
                current,
                "hash-object",
                "-w",
                "-t",
                kind,
                "--stdin",
                "--no-filters",
                data=body,
            )
            if observed != f"{oid}\n".encode("ascii"):
                _refuse(SeedCheckoutFailure.TRANSFER)
        inventory = await self._command(
            candidate,
            cutoff,
            current,
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname) %(objecttype)",
        )
        expected = sorted(
            f"{oid} {kind}".encode("ascii")
            for oid, kind in zip(self.object_ids, _OBJECT_TYPES, strict=True)
        )
        if sorted(inventory.splitlines()) != expected:
            _refuse(SeedCheckoutFailure.TRANSFER)
        commit = self.object_ids[0]
        await self._command(candidate, cutoff, current, "checkout", "--detach", commit)
        candidate.verify(candidate.path, cutoff, self._clock)
        descriptor = candidate.anchor.descriptor
        with _directory(descriptor, ".git") as repository:
            if _read_file(repository, "HEAD", 65) != f"{commit}\n".encode("ascii"):
                _refuse(SeedCheckoutFailure.TRANSFER)
        for name, body in zip(_SEED_FILES, objects[2:], strict=True):
            if _read_file(descriptor, name, len(body)) != body:
                _refuse(SeedCheckoutFailure.TRANSFER)

    async def _release(self) -> None:
        if self._cleanup is None or self._cleanup.done():
            self._cleanup = asyncio.create_task(self._release_owned())
        await asyncio.shield(self._cleanup)

    async def _release_owned(self) -> None:
        cutoff = self._clock.monotonic_ns() + _CLEANUP_NS
        try:
            gate = self._process
            if gate is not None:
                if not self._activation_attempted:
                    await gate.release_unactivated(cutoff)
                elif not gate.closed:
                    gate.close_gate()
                    gate.signal(signal.SIGKILL, cutoff)
                    await gate.settle(cutoff)
                    await gate.drain(cutoff)
                    gate.release(cutoff)
                if not gate.closed or gate.gate_close_failed:
                    _refuse(SeedCheckoutFailure.CLEANUP)
                self._process = None
            candidate = self._candidate
            if candidate is not None:
                candidate.verify(candidate.path, cutoff, self._clock)
                remove_directory(
                    self.root.anchor,
                    candidate.anchor.identity,
                    max_entries=_CANDIDATE_ENTRIES,
                )
                candidate.close()
                self._candidate = None
        except (
            BoundCheckoutError,
            ChannelError,
            FilesystemError,
            OSError,
            ProcessError,
        ):
            _refuse(SeedCheckoutFailure.CLEANUP)

    async def close(self) -> None:
        self._closing = True
        if self._active:
            _refuse(SeedCheckoutFailure.CLEANUP)
        await self._release()
        try:
            self.source.close()
        finally:
            self.root.close()
