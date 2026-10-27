import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.executor.failures import ExecutorError
from kora.executor.guard import PhaseGuard
from kora.executor.scan import InspectionSource, ScanBounds, ScanVerdict, scan
from kora.ir.node import ErrorClass
from kora.protocols.time import SystemClock
from kora.protocols.vcs import InspectionManifest

PREPARED: str = "sha256:" + "0" * 64
REVISION: str = "c" * 40
BASE: str = "b" * 40
LEAKED_VALUE: str = "kora-scanner-secret-0a9b"


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
) -> ScanVerdict:
    return asyncio.run(
        scan(
            inspection,
            PREPARED,
            REVISION,
            BASE,
            source,
            secrets,
            bounds or ScanBounds(chunk_bytes=7),
            PhaseGuard(SystemClock(), None),
            capture.diagnostics.logger(CONFORMANCE_COMPONENT),
        )
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
