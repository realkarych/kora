import os
import sys
import weakref
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from types import FrameType
from typing import Protocol, cast
from urllib.parse import urlsplit

import pytest

from kora.conformance.capture import Pipe
from kora.ir.standard_schema_policy import SchemaDomain, SourceAccounting
from kora.ir.standard_schema_source import Sources, unique_strings
from kora.ir.standard_schema_support import (
    check_graph,
    check_standard_schema_source,
)
from kora.observability import (
    Level,
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
    current_diagnostics,
)
from kora.observability.context import bound_scope
from tests.ir.standard_schema_cases import binding, decide, source
from tests.ir.structured_schema_cases import HostileHeader
from tests.observability.conftest import wait_until
from tests.observability.test_logging_loss_is_visible_without_the_sink import filled
from tests.support.observability import FakeClock


class CachedParser(Protocol):
    def cache_info(self) -> tuple[int, int, int | None, int]: ...


@contextmanager
def diagnostics_pipe(
    level: Level = Level.DEBUG,
    *,
    blocked: bool = False,
) -> Generator[tuple[ProcessDiagnostics, Pipe]]:
    pipe = Pipe.open()
    try:
        if blocked:
            filled(pipe.write_end)
            os.set_blocking(pipe.write_end, False)
        diagnostics = configure_process(
            "ir",
            LoggingSettings(level=level, queue_records=64, queue_bytes=65536),
            "schema-source-test",
            FakeClock().now,
            pipe.write_end,
        )
        try:
            yield diagnostics, pipe
        finally:
            diagnostics.shutdown()
            assert not diagnostics.writer_is_alive()
            assert current_diagnostics() is None
    finally:
        pipe.release()


@pytest.mark.parametrize("outcome", ["success", "schema", "policy", "capacity"])
def test_standard_schema_source_releases_owned_state(outcome: str) -> None:
    marker = "".join(["private", "_schema", "_content"])
    leaf = {"description": marker}
    schema: dict[str, object] = {
        "$defs": {marker: leaf},
        "$ref": "#/$defs/" + marker,
        "required": [marker, "different"],
    }
    policy = binding()
    if outcome == "schema":
        schema["$dynamicRef"] = "#/$defs/missing"
    elif outcome == "policy":
        policy["policy"] = marker
    elif outcome == "capacity":
        schema["annotation"] = "x" * 65537
    candidate = source(schema, metadata=marker)
    before = (
        sys.getrefcount(candidate),
        sys.getrefcount(schema),
        sys.getrefcount(leaf),
        sys.getrefcount(marker),
    )
    cache_before = cast("CachedParser", urlsplit).cache_info()

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, policy)
    after = (
        sys.getrefcount(candidate),
        sys.getrefcount(schema),
        sys.getrefcount(leaf),
        sys.getrefcount(marker),
    )

    assert before == after
    assert cast("CachedParser", urlsplit).cache_info() == cache_before
    assert result.outcome == (
        "source_checks_completed" if outcome == "success" else "refused"
    )
    assert marker not in repr(result)
    assert [field.name for field in fields(result)] == [
        "outcome",
        "reason",
        "accounting",
    ]
    assert all(
        type(getattr(result.accounting, field.name)) is int
        for field in fields(result.accounting)
    )


def test_standard_schema_source_refusal_drops_exception_frames_without_collection() -> (
    None
):
    original = HostileHeader()
    retained = weakref.ref(original)
    candidate = source(True, metadata=original)

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())
    del candidate
    del original

    assert result.reason == "unsupported_schema"
    assert retained() is None


@dataclass
class OwnershipObservation:
    radix_entries: int = 0
    position_entries: int = 0
    reference_entries: int = 0
    document_entries: int = 0
    graph_observed: bool = False

    def observe(self, frame: FrameType, event: str, argument: object) -> None:
        if frame.f_code is unique_strings.__code__:
            strings = frame.f_locals.get("strings")
            pending = frame.f_locals.get("pending")
            if type(strings) is list and type(pending) is list:
                self.radix_entries = max(
                    self.radix_entries,
                    len(cast("list[object]", strings))
                    + 2 * len(cast("list[object]", pending)),
                )
        elif frame.f_code is check_graph.__code__ and "ready" in frame.f_locals:
            sources = frame.f_locals["sources"]
            parents = cast("list[list[int]]", frame.f_locals["parents"])
            ready = cast("list[int]", frame.f_locals["ready"])
            assert isinstance(sources, Sources)
            positions = len(sources.sites)
            references = len(sources.references)
            edges = sum(len(site.edges) for site in sources.sites)
            parent_entries = sum(len(items) for items in parents)
            assert parent_entries == edges
            self.position_entries = max(
                self.position_entries,
                (
                    2 * positions
                    + 2 * len(sources.index)
                    + 2 * (edges - references)
                    + len(parents)
                    + parent_entries
                    - references
                    + len(ready)
                ),
            )
            self.reference_entries = 2 * references + 2 * references + references
            self.document_entries = len(sources.documents) + len(sources.named)
            self.graph_observed = True


def test_standard_schema_source_scratch_and_graph_owners_fit_their_allocations() -> (
    None
):
    schema = {
        "$defs": {f"leaf{index}": True for index in range(64)},
        "allOf": [
            {"$ref": f"#/$defs/leaf{index}", "$dynamicRef": f"#/$defs/leaf{index}"}
            for index in range(64)
        ],
        "required": [f"prefix{index:04d}" for index in range(128)],
    }
    observation = OwnershipObservation()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        result = decide(schema)
    finally:
        sys.setprofile(previous)

    assert result.outcome == "source_checks_completed"
    assert observation.graph_observed
    assert 128 < observation.radix_entries <= 2 * 128
    assert 0 < observation.position_entries <= 12 * result.accounting.schema_positions
    assert 0 < observation.reference_entries <= 8 * result.accounting.reference_edges
    assert observation.document_entries <= 4 * result.accounting.schema_documents


@pytest.mark.parametrize(
    "reason",
    ["none", "unsupported_policy", "unsupported_schema", "evaluation_exhausted"],
)
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
def test_standard_schema_source_diagnostics_exclude_content(
    reason: str, level: Level, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "private_schema_source_marker"
    candidate = source({"description": marker}, metadata={marker: marker})
    policy = binding()
    if reason == "unsupported_policy":
        policy["policy"] = marker
    elif reason == "unsupported_schema":
        candidate = source({"$ref": "https://example.invalid/" + marker})
    elif reason == "evaluation_exhausted":
        candidate = source(True, metadata=marker * 65536)

    with diagnostics_pipe(level) as (diagnostics, pipe):
        pipe.reader.start()
        with bound_scope(request_id="source-check-request"):
            try:
                raise RuntimeError(marker)
            except RuntimeError:
                result = check_standard_schema_source(
                    candidate, SchemaDomain.OUTPUT, policy
                )
        assert diagnostics.shutdown()
        pipe.close_write_end()
        pipe.reader.wait_closed()
        records = [
            record
            for record in pipe.reader.records()
            if record["event"] == "ir.standard_schema_source.completed"
        ]
        output = pipe.reader.data

    assert result.reason == reason
    assert marker.encode() not in output
    assert marker not in repr(result)
    assert capsys.readouterr().out == ""
    assert len(records) == (1 if level is Level.DEBUG else 0)
    if records:
        record = records[0]
        assert record["component"] == "ir"
        assert record["level"] == "DEBUG"
        assert record["request_id"] == "source-check-request"
        assert record["outcome"] == result.outcome
        assert record["reason"] == reason
        for field in fields(SourceAccounting):
            assert record[field.name] == getattr(result.accounting, field.name)
        if reason != "none":
            assert result.accounting == SourceAccounting()


@pytest.mark.parametrize("broken", [False, True], ids=["blocked", "broken"])
def test_standard_schema_source_survives_unavailable_sink(broken: bool) -> None:
    schema = {"description": "private_schema_source_marker"}
    expected = decide(schema)

    with diagnostics_pipe(blocked=not broken) as (diagnostics, pipe):
        if broken:
            pipe.close_read_end()
        else:
            wait_until(lambda: diagnostics.snapshot().in_flight_bytes > 0)
        matches = all(decide(schema) == expected for _ in range(256))
        if broken:
            wait_until(lambda: diagnostics.snapshot().sink_failed)
        snapshot = diagnostics.snapshot()
        if not broken:
            pipe.reader.start()
        drained = diagnostics.shutdown()

    assert matches
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= 64
    assert snapshot.queue_bytes <= 65536
    assert drained
    assert not diagnostics.writer_is_alive()
