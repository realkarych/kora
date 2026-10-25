import asyncio
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from kora.executor.failures import Phase, invalid
from kora.executor.files import DIRECTORY_FLAGS
from kora.executor.launcher import (
    DEFAULT_KILL_GRACE_NS,
    LaunchSpec,
    StepLauncher,
    StreamingChild,
)
from kora.executor.scan import InspectionSource, InspectionSources
from kora.plugins.vcs.git.environment import git_environment, publisher_options
from kora.plugins.vcs.git.plans import REPOSITORY, is_object_id
from kora.plugins.vcs.git.tools import GitTools
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.errors import KoraProtocolError

MIB: Final[int] = 1_048_576
HIGH_WATER: Final[int] = 4 * MIB
MAX_HEADER: Final[int] = 256
MAX_LISTING: Final[int] = 64 * MIB
LISTING_TIMEOUT_NS: Final[int] = 600 * NANOSECONDS_PER_SECOND
NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,255}")
READ_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
KEPT_TYPES: Final[frozenset[bytes]] = frozenset({b"commit", b"blob"})


def unavailable() -> KoraProtocolError:
    return KoraProtocolError(
        "vcs_inspection_unavailable", "the inspection source is unavailable", True, {}
    )


class GitInspectionSource(InspectionSource):
    def __init__(
        self,
        tools: GitTools,
        work: Path,
        launcher: StepLauncher,
        batch: StreamingChild,
    ) -> None:
        self._tools = tools
        self._work = work
        self._launcher = launcher
        self._batch = batch
        self._lock = asyncio.Lock()

    async def index(self, location: str, limit: int) -> bytes:
        if NAME.fullmatch(location) is None or location in (".", ".."):
            invalid("content_index_location", Phase.SCAN)
        directory = os.open(self._work, DIRECTORY_FLAGS)
        try:
            descriptor = os.open(location, READ_FLAGS, dir_fd=directory)
        except OSError:
            invalid("content_index_missing", Phase.SCAN)
        finally:
            os.close(directory)
        chunks: list[bytes] = []
        size = 0
        try:
            while block := os.read(descriptor, MIB):
                size += len(block)
                if size > limit:
                    invalid("content_index_oversized", Phase.SCAN)
                chunks.append(block)
        finally:
            os.close(descriptor)
        return b"".join(chunks)

    async def header(self, location: str) -> tuple[bytes, int]:
        if not is_object_id(location):
            invalid("content_location_invalid", Phase.SCAN)
        try:
            self._batch.write(location.encode("ascii") + b"\n")
            line = await self._batch.stdout.read_line(MAX_HEADER)
        except (OSError, EOFError):
            raise unavailable() from None
        fields = line.split()
        if len(fields) == 2 and fields[1] == b"missing":
            invalid("content_missing", Phase.SCAN)
        if len(fields) != 3 or fields[0] != location.encode("ascii"):
            raise unavailable()
        return fields[1], int(fields[2])

    async def content(self, location: str, limit: int) -> AsyncIterator[bytes]:
        async with self._lock:
            kind, size = await self.header(location)
            if kind not in KEPT_TYPES or size > limit:
                await self.skip(size)
                invalid("content_unsupported", Phase.SCAN)
            remaining = size
            while remaining:
                try:
                    block = await self._batch.stdout.read_some(min(MIB, remaining))
                except EOFError:
                    raise unavailable() from None
                remaining -= len(block)
                yield block
            await self.skip(0)

    async def skip(self, size: int) -> None:
        remaining = size + 1
        while remaining:
            try:
                block = await self._batch.stdout.read_some(min(MIB, remaining))
            except EOFError:
                raise unavailable() from None
            remaining -= len(block)

    async def revision_bytes(self, revision: str, limit: int) -> bytes:
        chunks = [chunk async for chunk in self.content(revision, limit)]
        return b"".join(chunks)

    async def listed(
        self, arguments: tuple[str, ...], stdin: bytes | None, timeout_ns: int
    ) -> bytes:
        home = str(self._work / "home")
        spec = LaunchSpec(
            argv=(self._tools.git, f"--git-dir={REPOSITORY}", *arguments),
            cwd=self._work,
            env=git_environment(
                self._tools.git, home, publisher_options(home, (str(self._work),))
            ),
            stdin=stdin,
            timeout_ns=min(timeout_ns, LISTING_TIMEOUT_NS),
            stdout_limit=MAX_LISTING,
            stderr_limit=4096,
        )
        outcome = await self._launcher.run(spec)
        if not outcome.succeeded or outcome.stdout_overflow:
            raise unavailable()
        return outcome.stdout

    async def reachable(
        self, revision: str, base: str, timeout_ns: int
    ) -> frozenset[str]:
        if not (is_object_id(revision) and is_object_id(base)):
            invalid("content_location_invalid", Phase.SCAN)
        objects = await self.listed(
            ("rev-list", "--objects", "--no-object-names", revision, f"^{base}", "--"),
            None,
            timeout_ns,
        )
        typed = await self.listed(
            ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
            objects,
            timeout_ns,
        )
        kept: set[str] = set()
        for line in typed.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] in KEPT_TYPES:
                kept.add(fields[0].decode("ascii"))
        return frozenset(kept)


class GitInspectionSources(InspectionSources):
    def __init__(self, tools: GitTools, launcher: StepLauncher) -> None:
        self._tools = tools
        self._launcher = launcher

    @asynccontextmanager
    async def open(self, prepared_location: str) -> AsyncGenerator[InspectionSource]:
        work = Path(prepared_location)
        home = str(work / "home")
        batch = StreamingChild(
            asyncio.get_running_loop(),
            (self._tools.git, f"--git-dir={REPOSITORY}", "cat-file", "--batch"),
            work,
            git_environment(
                self._tools.git, home, publisher_options(home, (str(work),))
            ),
            HIGH_WATER,
        )
        try:
            yield GitInspectionSource(self._tools, work, self._launcher, batch)
        finally:
            try:
                await batch.stop(DEFAULT_KILL_GRACE_NS)
            finally:
                batch.kill_now()
