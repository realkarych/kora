import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import ValidationError

from kora.executor.digests import hasher_digest, inspection_digest, sha256_digest
from kora.executor.failures import (
    ExecutorError,
    Phase,
    content_refused,
    fail,
    invalid,
    misconfigured,
)
from kora.executor.guard import PhaseGuard
from kora.ir.node import ErrorClass
from kora.ir.scalars import Digest
from kora.observability import ComponentLogger
from kora.protocols.common import BoundaryModel
from kora.protocols.identity import Count
from kora.protocols.vcs import InspectionContent, InspectionManifest

MIB: Final[int] = 1_048_576
GIB: Final[int] = 1024 * MIB
NANOSECONDS_PER_MINUTE: Final[int] = 60 * 10**9
KEY_PATTERNS: Final[tuple[bytes, ...]] = (
    rb"AKIA[0-9A-Z]{16}",
    rb"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----",
    rb"gh[pousr]_[A-Za-z0-9]{36}",
    rb"github_pat_[A-Za-z0-9_]{82}",
    rb"glpat-[A-Za-z0-9_-]{20}",
    rb"xox[abprs]-[A-Za-z0-9-]{10,48}",
    rb"sk-ant-[A-Za-z0-9_-]{32,128}",
)
PATTERN_SPAN: Final[int] = 160
OFF_LOOP_BYTES: Final[int] = 65_536


@dataclass(frozen=True)
class ScanBounds:
    max_records: int = 100_000
    max_bytes: int = 2 * GIB
    max_file_bytes: int = 256 * MIB
    max_index_bytes: int = 128 * MIB
    max_secrets: int = 256
    time_ns: int = 10 * NANOSECONDS_PER_MINUTE


class InspectionSource(Protocol):
    async def index(self, location: str, limit: int) -> bytes: ...

    def content(self, location: str, limit: int) -> AsyncIterator[bytes]: ...

    async def revision_bytes(self, revision: str, limit: int) -> bytes: ...

    async def reachable(
        self, revision: str, base: str, timeout_ns: int
    ) -> frozenset[str]: ...


class InspectionSources(Protocol):
    def open(
        self, prepared_location: str
    ) -> AbstractAsyncContextManager[InspectionSource]: ...


class ScanVerdict(BoundaryModel):
    prepared_digest: Digest
    inspection_digest: Digest
    records: Count
    scanned_bytes: Count


class Matcher:
    def __init__(self, values: Sequence[str]) -> None:
        self._literals = tuple(
            sorted({value.encode("utf-8") for value in values if value}, key=len)
        )
        self._patterns = re.compile(b"|".join(KEY_PATTERNS))
        self.overlap = max([PATTERN_SPAN, *map(len, self._literals)]) - 1

    def hits(self, data: bytes) -> bool:
        if any(literal in data for literal in self._literals):
            return True
        return self._patterns.search(data) is not None

    async def hits_off_loop(self, data: bytes) -> bool:
        if len(data) < OFF_LOOP_BYTES:
            return self.hits(data)
        return await asyncio.to_thread(self.hits, data)


@dataclass
class ScanState:
    matcher: Matcher
    guard: PhaseGuard
    deadline_ns: int
    bounds: ScanBounds
    scanned: int = 0
    seen: dict[str, tuple[int, str]] = field(default_factory=lambda: {})

    def charge(self, size: int) -> None:
        self.scanned += size
        if self.scanned > self.bounds.max_bytes:
            invalid("scan_bytes_exceeded", Phase.SCAN)
        if self.guard.clock.monotonic_ns() >= self.deadline_ns:
            fail(ErrorClass.TIMEOUT, "scan_timed_out", Phase.SCAN)


def leak(logger: ComponentLogger) -> ExecutorError:
    return content_refused(
        logger, ErrorClass.CREDENTIAL_LEAK, "secret_leak", "content", Phase.SCAN
    )


def parsed_records(index: bytes, bounds: ScanBounds) -> list[InspectionContent]:
    lines = index.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) > bounds.max_records:
        invalid("content_index_oversized", Phase.SCAN)
    records: list[InspectionContent] = []
    for line in lines:
        try:
            records.append(InspectionContent.model_validate_json(line, strict=True))
        except ValidationError:
            invalid("content_index_malformed", Phase.SCAN)
    return records


async def scanned_content(
    source: InspectionSource,
    record: InspectionContent,
    state: ScanState,
    logger: ComponentLogger,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    tail = b""
    async for chunk in source.content(
        record.content_location, state.bounds.max_file_bytes
    ):
        size += len(chunk)
        if size > state.bounds.max_file_bytes:
            invalid("content_oversized", Phase.SCAN)
        state.charge(len(chunk))
        digest.update(chunk)
        window = tail + chunk
        if await state.matcher.hits_off_loop(window):
            raise leak(logger)
        tail = window[-state.matcher.overlap :] if state.matcher.overlap else b""
    return size, hasher_digest(digest)


async def scan_record(
    source: InspectionSource,
    record: InspectionContent,
    state: ScanState,
    logger: ComponentLogger,
) -> None:
    observed = state.seen.get(record.content_location)
    if observed is None:
        observed = await scanned_content(source, record, state, logger)
        state.seen[record.content_location] = observed
    if observed != (record.size_bytes, record.content_digest):
        invalid("content_digest_mismatch", Phase.SCAN)


def check_names(
    records: Sequence[InspectionContent], changed: Sequence[str], state: ScanState
) -> bool:
    paths = [record.path for record in records if record.path is not None]
    names = "\n".join([*paths, *changed])
    return state.matcher.hits(names.encode("utf-8"))


async def scan(
    manifest: InspectionManifest,
    prepared_digest: str,
    revision: str,
    base: str,
    source: InspectionSource,
    secrets: Sequence[str],
    bounds: ScanBounds,
    guard: PhaseGuard,
    logger: ComponentLogger,
    changed: Sequence[str] = (),
) -> ScanVerdict:
    if len(secrets) > bounds.max_secrets:
        misconfigured("scanner_secrets_exceeded", Phase.SCAN)
    deadline_ns = guard.clock.monotonic_ns() + guard.bound_ns(
        bounds.time_ns, Phase.SCAN
    )
    if not manifest.complete:
        invalid("content_index_incomplete", Phase.SCAN)
    if manifest.prepared_digest != prepared_digest or manifest.revision != revision:
        invalid("content_index_unbound", Phase.SCAN)
    index = await source.index(manifest.content_index_location, bounds.max_index_bytes)
    index_digest = sha256_digest(index)
    if index_digest != manifest.content_index_digest:
        invalid("content_index_digest_mismatch", Phase.SCAN)
    records = parsed_records(index, bounds)
    reachable = await source.reachable(
        revision, base, max(1, deadline_ns - guard.clock.monotonic_ns())
    )
    if frozenset(record.content_location for record in records) != reachable:
        invalid("content_index_incomplete", Phase.SCAN)
    state = ScanState(Matcher(secrets), guard, deadline_ns, bounds)
    if check_names(records, changed, state):
        raise leak(logger)
    for record in records:
        await scan_record(source, record, state, logger)
    return ScanVerdict(
        prepared_digest=prepared_digest,
        inspection_digest=inspection_digest(index_digest, manifest),
        records=len(records),
        scanned_bytes=state.scanned,
    )
