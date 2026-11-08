import asyncio
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ParamSpec, SupportsIndex, TypeVar

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.executor.failures import ExecutorError
from kora.executor.guard import PhaseGuard
from kora.executor.scan import (
    InspectionSource,
    Matcher,
    ScanBounds,
    ScanState,
    ScanVerdict,
    check_names,
    index_digest_of,
    parsed_records,
    scan,
)
from kora.ir.node import ErrorClass
from kora.protocols.time import SystemClock
from kora.protocols.vcs import InspectionManifest

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer

PREPARED: str = "sha256:" + "0" * 64
REVISION: str = "c" * 40
BASE: str = "b" * 40
LEAKED_VALUE: str = "kora-scanner-secret-0a9b"
LEAKED_KEY: str = "AKIA" + "Q" * 16
MIB: int = 1_048_576
DEADLINE_SECONDS: float = 30.0
Params = ParamSpec("Params")
Result = TypeVar("Result")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class MemorySource(InspectionSource):
    index_bytes: bytes
    contents: dict[str, bytes]
    reachable_locations: frozenset[str]
    reads: list[str] = field(default_factory=lambda: [])
    chunk: int = 7

    async def index(self, location: str, limit: int) -> bytes:
        self.reads.append("index")
        return self.index_bytes

    async def content(self, location: str, limit: int) -> AsyncIterator[bytes]:
        self.reads.append(location)
        data = self.contents[location]
        for start in range(0, len(data), self.chunk):
            yield data[start : start + self.chunk]

    async def revision_bytes(self, revision: str, limit: int) -> bytes:
        return self.contents[revision]

    async def reachable(
        self, revision: str, base: str, timeout_ns: int
    ) -> frozenset[str]:
        return self.reachable_locations


def record(
    location: str, data: bytes, path: str | None, kind: str
) -> dict[str, object]:
    return {
        "revision": REVISION,
        "path": path,
        "kind": kind,
        "content_location": location,
        "content_digest": digest(data),
        "size_bytes": len(data),
    }


def history() -> tuple[list[dict[str, object]], dict[str, bytes]]:
    contents = {
        REVISION: b"tree t\nauthor a\n\nmessage\n",
        "1" * 40: b"first version\n",
        "2" * 40: b"intermediate version, later removed\n",
    }
    records = [
        record(REVISION, contents[REVISION], None, "revision_metadata"),
        record("1" * 40, contents["1" * 40], "a.txt", "file"),
        record("2" * 40, contents["2" * 40], "a.txt", "file"),
    ]
    return records, contents


def encoded(records: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(item).encode() + b"\n" for item in records)


def manifest(index: bytes, **changes: object) -> InspectionManifest:
    document: dict[str, object] = {
        "prepared_digest": PREPARED,
        "revision": REVISION,
        "content_index_location": "index.jsonl",
        "content_index_digest": digest(index),
        "complete": True,
        **changes,
    }
    return InspectionManifest.model_validate(document)


def scanned(
    capture: Capture,
    source: MemorySource,
    inspection: InspectionManifest,
    secrets: tuple[str, ...] = (),
    bounds: ScanBounds | None = None,
    changed: tuple[str, ...] = (),
) -> ScanVerdict:
    return asyncio.run(scanning(capture, source, inspection, secrets, bounds, changed))


async def scanning(
    capture: Capture,
    source: MemorySource,
    inspection: InspectionManifest,
    secrets: tuple[str, ...] = (),
    bounds: ScanBounds | None = None,
    changed: tuple[str, ...] = (),
) -> ScanVerdict:
    return await scan(
        inspection,
        PREPARED,
        REVISION,
        BASE,
        source,
        secrets,
        bounds or ScanBounds(),
        PhaseGuard(SystemClock(), None),
        capture.diagnostics.logger(CONFORMANCE_COMPONENT),
        changed,
    )


Tamper = Callable[
    [list[dict[str, object]], dict[str, bytes]],
    tuple[bytes, InspectionManifest, frozenset[str]],
]


def truncated(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    index = encoded(records)
    return index[:-10], manifest(index), frozenset(contents)


def missing_intermediate(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    index = encoded(records[:2])
    return index, manifest(index), frozenset(contents)


def corrupt_blob(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    contents["1" * 40] = b"tampered content\n"
    index = encoded(records)
    return index, manifest(index), frozenset(contents)


def corrupt_digest(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    records[1]["content_digest"] = digest(b"other")
    index = encoded(records)
    return index, manifest(index), frozenset(contents)


def false_completeness(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    index = encoded(records)
    return index, manifest(index, complete=False), frozenset(contents)


def malformed_record(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    records[1]["kind"] = "directory"
    index = encoded(records)
    return index, manifest(index), frozenset(contents)


def unbound_revision(
    records: list[dict[str, object]], contents: dict[str, bytes]
) -> tuple[bytes, InspectionManifest, frozenset[str]]:
    index = encoded(records)
    return index, manifest(index, revision="d" * 40), frozenset(contents)


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        (truncated, "content_index_digest_mismatch"),
        (missing_intermediate, "content_index_incomplete"),
        (corrupt_blob, "content_digest_mismatch"),
        (corrupt_digest, "content_digest_mismatch"),
        (false_completeness, "content_index_incomplete"),
        (malformed_record, "content_index_malformed"),
        (unbound_revision, "content_index_unbound"),
    ],
)
def test_scanner_refuses_tampered_index(
    kora_capture: Capture, tamper: Tamper, code: str
) -> None:
    records, contents = history()
    index, inspection, reachable = tamper(records, contents)
    source = MemorySource(index, contents, reachable)

    with pytest.raises(ExecutorError) as raised:
        scanned(kora_capture, source, inspection)

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert raised.value.code == code


def test_a_secret_across_a_chunk_boundary_is_found(kora_capture: Capture) -> None:
    records, contents = history()
    contents["2" * 40] = b"prefix-" + LEAKED_VALUE.encode() + b"-suffix"
    records[2] = record("2" * 40, contents["2" * 40], "a.txt", "file")
    index = encoded(records)
    source = MemorySource(index, contents, frozenset(contents), chunk=5)

    with pytest.raises(ExecutorError) as raised:
        scanned(kora_capture, source, manifest(index), (LEAKED_VALUE,))

    assert raised.value.failure_class is ErrorClass.CREDENTIAL_LEAK


def test_key_patterns_and_names_are_scanned(kora_capture: Capture) -> None:
    records, contents = history()
    records[1]["path"] = f"leaked-{LEAKED_VALUE}.txt"
    index = encoded(records)
    key_contents = dict(contents)
    key_contents["2" * 40] = b"-----BEGIN RSA PRIVATE KEY-----\n"
    key_records = list(records)
    key_records[1] = record("1" * 40, contents["1" * 40], "a.txt", "file")
    key_records[2] = record("2" * 40, key_contents["2" * 40], "id_rsa", "file")
    key_index = encoded(key_records)

    with pytest.raises(ExecutorError) as named:
        scanned(
            kora_capture,
            MemorySource(index, contents, frozenset(contents)),
            manifest(index),
            (LEAKED_VALUE,),
        )
    with pytest.raises(ExecutorError) as keyed:
        scanned(
            kora_capture,
            MemorySource(key_index, key_contents, frozenset(key_contents)),
            manifest(key_index),
        )

    assert named.value.failure_class is ErrorClass.CREDENTIAL_LEAK
    assert keyed.value.failure_class is ErrorClass.CREDENTIAL_LEAK


def test_too_many_secret_values_are_refused_before_scanning(
    kora_capture: Capture,
) -> None:
    records, contents = history()
    index = encoded(records)
    source = MemorySource(index, contents, frozenset(contents))
    values = tuple(f"secret-value-{number:04d}" for number in range(257))

    with pytest.raises(ExecutorError) as raised:
        scanned(kora_capture, source, manifest(index), values)

    assert raised.value.failure_class is ErrorClass.CONFIG_ERROR
    assert source.reads == []


def test_a_clean_history_is_bound_to_both_digests(kora_capture: Capture) -> None:
    records, contents = history()
    index = encoded(records)
    source = MemorySource(index, contents, frozenset(contents))

    verdict = scanned(kora_capture, source, manifest(index), (LEAKED_VALUE,))

    assert verdict.prepared_digest == PREPARED
    assert verdict.records == 3
    assert verdict.scanned_bytes == sum(map(len, contents.values()))


class LoopWitness(ThreadPoolExecutor):
    def __init__(self) -> None:
        super().__init__(max_workers=4)
        self.passes = 0
        self.changed = threading.Condition()

    def submit(
        self,
        fn: Callable[Params, Result],
        /,
        *args: Params.args,
        **kwargs: Params.kwargs,
    ) -> Future[Result]:
        def witnessed() -> Result:
            with self.changed:
                started = self.passes
                assert self.changed.wait_for(
                    lambda: self.passes > started, DEADLINE_SECONDS
                )
            return fn(*args, **kwargs)

        return super().submit(witnessed)

    async def run(self) -> None:
        while True:
            with self.changed:
                self.passes += 1
                self.changed.notify_all()
            await asyncio.sleep(0)


@dataclass
class StampedSource(MemorySource):
    witness: LoopWitness = field(default_factory=LoopWitness)
    stamps: dict[str, int] = field(default_factory=lambda: {})

    async def index(self, location: str, limit: int) -> bytes:
        self.stamps["index"] = self.witness.passes
        return await super().index(location, limit)

    async def reachable(
        self, revision: str, base: str, timeout_ns: int
    ) -> frozenset[str]:
        found = await super().reachable(revision, base, timeout_ns)
        self.stamps["reachable"] = self.witness.passes
        return found

    async def content(self, location: str, limit: int) -> AsyncIterator[bytes]:
        self.stamps.setdefault("content", self.witness.passes)
        async for chunk in super().content(location, limit):
            yield chunk


def stamps_of_a_clean_scan(capture: Capture) -> dict[str, int]:
    records, contents = history()
    index = encoded(records)
    source = StampedSource(index, contents, frozenset(contents))

    async def main() -> None:
        asyncio.get_running_loop().set_default_executor(source.witness)
        ticking = asyncio.create_task(source.witness.run())
        try:
            await scanning(capture, source, manifest(index))
        finally:
            ticking.cancel()
            await asyncio.gather(ticking, return_exceptions=True)

    asyncio.run(main())
    return source.stamps


def test_index_digest_and_parsing_leave_the_loop_running(
    kora_capture: Capture,
) -> None:
    stamps = stamps_of_a_clean_scan(kora_capture)

    assert stamps["reachable"] > stamps["index"]


def test_a_mismatched_index_digest_is_refused_off_the_loop(
    kora_capture: Capture,
) -> None:
    records, contents = history()
    index = encoded(records)
    source = StampedSource(index, contents, frozenset(contents))
    inspection = manifest(index, content_index_digest=digest(b"other"))

    async def main() -> int:
        asyncio.get_running_loop().set_default_executor(source.witness)
        ticking = asyncio.create_task(source.witness.run())
        try:
            with pytest.raises(ExecutorError) as raised:
                await scanning(kora_capture, source, inspection)
            assert raised.value.code == "content_index_digest_mismatch"
            return source.witness.passes
        finally:
            ticking.cancel()
            await asyncio.gather(ticking, return_exceptions=True)

    refused_at = asyncio.run(main())

    assert refused_at > source.stamps["index"]


def test_index_digest_is_unchanged_and_stops_when_cancelled() -> None:
    data = bytes(range(256)) * (3 * MIB // 256) + b"tail-bytes-17"
    cancelled = threading.Event()

    assert index_digest_of(data, cancelled) == digest(data)

    cancelled.set()
    with pytest.raises(ExecutorError) as raised:
        index_digest_of(data, cancelled)

    assert raised.value.code == "scan_cancelled"


def test_name_search_leaves_the_loop_running(kora_capture: Capture) -> None:
    stamps = stamps_of_a_clean_scan(kora_capture)

    assert stamps["content"] > stamps["reachable"]


class Gate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self.workers: list[threading.Thread] = []

    def hold(self) -> None:
        self.workers.append(threading.current_thread())
        self.entered.set()
        assert self.released.wait(DEADLINE_SECONDS)

    async def wait_entered(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, DEADLINE_SECONDS)


class GatedIndex(bytes):
    gate: Gate

    def split(
        self, sep: "ReadableBuffer | None" = None, maxsplit: SupportsIndex = -1
    ) -> list[bytes]:
        self.gate.hold()
        return super().split(sep, maxsplit)


def gated(index: bytes, gate: Gate) -> GatedIndex:
    held = GatedIndex(index)
    held.gate = gate
    return held


@dataclass
class ReachRecordingSource(MemorySource):
    reached: bool = False

    async def reachable(
        self, revision: str, base: str, timeout_ns: int
    ) -> frozenset[str]:
        self.reached = True
        return await super().reachable(revision, base, timeout_ns)


class RecordingMatcher(Matcher):
    def __init__(self, values: tuple[str, ...], gate: Gate | None = None) -> None:
        super().__init__(values)
        self.gate = gate
        self.seen: list[tuple[int, int]] = []

    def hits(self, data: bytes) -> bool:
        self.seen.append((len(data), threading.get_ident()))
        if self.gate is not None and len(data) >= MIB:
            self.gate.hold()
        return super().hits(data)


def names_state(matcher: Matcher) -> ScanState:
    return ScanState(matcher, PhaseGuard(SystemClock(), None), 2**62, ScanBounds())


async def loop_passes(count: int) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


async def cancelled_while_held(task: asyncio.Task[object], gate: Gate) -> None:
    await gate.wait_entered()
    task.cancel()
    await loop_passes(3)
    task.cancel()
    await loop_passes(3)
    assert not task.done()
    gate.released.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, DEADLINE_SECONDS)


def recorded_exceptions(loop: asyncio.AbstractEventLoop) -> list[dict[str, object]]:
    recorded: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _, context: recorded.append(context))
    return recorded


def test_names_are_searched_in_bounded_windows() -> None:
    matcher = RecordingMatcher(())
    changed = tuple(f"{number:06d}-" + "n" * 4000 for number in range(800))

    async def main() -> tuple[int, bool]:
        return threading.get_ident(), await check_names(
            (), changed, names_state(matcher)
        )

    loop_thread, hit = asyncio.run(main())

    assert not hit
    assert len(matcher.seen) > 1
    assert all(size <= MIB + matcher.overlap for size, _ in matcher.seen)
    assert all(thread != loop_thread for size, thread in matcher.seen if size >= 65_536)


@pytest.mark.parametrize(
    ("value", "secrets"),
    [(LEAKED_VALUE, (LEAKED_VALUE,)), (LEAKED_KEY, ())],
    ids=["secret_value", "key_pattern"],
)
def test_a_secret_across_a_name_window_boundary_is_found(
    kora_capture: Capture, value: str, secrets: tuple[str, ...]
) -> None:
    records, contents = history()
    index = encoded(records)
    source = MemorySource(index, contents, frozenset(contents))
    preceding = sum(len(str(item["path"])) + 1 for item in records if item["path"])
    straddling = "x" * (MIB - preceding - len(value) // 2) + value + "-suffix"

    with pytest.raises(ExecutorError) as raised:
        scanned(kora_capture, source, manifest(index), secrets, changed=(straddling,))

    assert raised.value.failure_class is ErrorClass.CREDENTIAL_LEAK
    output = json.dumps(kora_capture.finish())
    assert "secret_leak" in output
    assert value not in output


def test_cancelling_during_parsing_stops_the_scan(kora_capture: Capture) -> None:
    records, contents = history()
    index = encoded(records)
    gate = Gate()
    source = ReachRecordingSource(gated(index, gate), contents, frozenset(contents))

    async def main() -> list[dict[str, object]]:
        recorded = recorded_exceptions(asyncio.get_running_loop())
        task = asyncio.create_task(scanning(kora_capture, source, manifest(index)))
        await cancelled_while_held(task, gate)
        return recorded

    recorded = asyncio.run(main())

    assert not source.reached
    assert not gate.workers[0].is_alive()
    assert recorded == []


def test_cancelling_during_name_search_stops_the_scan() -> None:
    gate = Gate()
    matcher = RecordingMatcher((), gate)

    async def main() -> list[dict[str, object]]:
        recorded = recorded_exceptions(asyncio.get_running_loop())
        task = asyncio.create_task(
            check_names((), ("n" * (2 * MIB),), names_state(matcher))
        )
        await cancelled_while_held(task, gate)
        return recorded

    recorded = asyncio.run(main())

    assert len(matcher.seen) == 1
    assert not gate.workers[0].is_alive()
    assert recorded == []


def test_shutdown_joins_the_scan_worker(kora_capture: Capture) -> None:
    records, contents = history()
    index = encoded(records)
    gate = Gate()
    source = ReachRecordingSource(gated(index, gate), contents, frozenset(contents))

    async def release_on_shutdown() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            gate.released.set()

    pending: list[asyncio.Task[object]] = []

    async def main() -> None:
        pending.append(asyncio.create_task(release_on_shutdown()))
        pending.append(
            asyncio.create_task(scanning(kora_capture, source, manifest(index)))
        )
        await gate.wait_entered()

    asyncio.run(main())

    assert not source.reached
    assert not gate.workers[0].is_alive()


def test_parsing_runs_on_a_worker(kora_capture: Capture) -> None:
    records, contents = history()
    index = encoded(records)
    gate = Gate()
    gate.released.set()
    source = MemorySource(gated(index, gate), contents, frozenset(contents))

    async def main() -> threading.Thread:
        await scanning(kora_capture, source, manifest(index))
        return threading.current_thread()

    loop_thread = asyncio.run(main())

    assert gate.workers
    assert loop_thread not in gate.workers


def test_parsing_stops_when_cancelled() -> None:
    records, _ = history()
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ExecutorError) as raised:
        parsed_records(encoded(records), ScanBounds(), cancelled)

    assert raised.value.failure_class is ErrorClass.INFRASTRUCTURE
    assert raised.value.code == "scan_cancelled"


def test_an_oversized_index_is_refused(kora_capture: Capture) -> None:
    records, contents = history()
    index = encoded(records)
    source = MemorySource(index, contents, frozenset(contents))

    with pytest.raises(ExecutorError) as raised:
        scanned(kora_capture, source, manifest(index), bounds=ScanBounds(max_records=2))

    assert raised.value.failure_class is ErrorClass.ARTIFACT_INVALID
    assert raised.value.code == "content_index_oversized"
