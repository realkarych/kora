import hashlib
import sys
from dataclasses import replace

import pytest

from kora.conformance.common import context_for
from kora.engine.artifact_projection import (
    check_artifact_projection,
    prepare_artifact_projection,
)
from kora.engine.artifact_projection_codec import (
    RECORD_ENTRIES,
    RECORD_VARIABLE_BYTES,
    ProjectionFailure,
    ProjectionRejected,
)
from kora.engine.artifact_projection_source import source_reservation
from kora.protocols.effective_artifact_type import ValidationBinding
from tests.engine.artifact_projection_cases import (
    LIMITS,
    PATH,
    metadata,
    projection_capture,
    projection_graph,
)
from tests.ir.test_standard_schema_source_lifetime import diagnostics_pipe
from tests.observability.conftest import wait_until
from tests.protocols.effective_artifact_cases import ObservedArtifactType, installed
from tests.protocols.test_effective_artifact_lifetime import (
    CompositionObservation,
    diagnostic_context,
)


@pytest.mark.parametrize(
    "resource", ["source_bytes", "variable_bytes", "entries", "work"]
)
def test_projection_refuses_unfunded_preparation_before_installed_validation(
    resource: str,
) -> None:
    checked = projection_graph()
    provider = ObservedArtifactType()
    bindings = installed()
    identity = "kora.structured-output-validation.v1"
    bindings["validation", identity] = ValidationBinding(identity, provider.validate)
    limits = replace(LIMITS, **{resource: 1})

    with projection_capture() as capture, pytest.raises(ProjectionRejected) as refused:
        prepare_artifact_projection(
            checked, PATH, None, bindings, context_for(capture), limits=limits
        )

    assert provider.calls == []
    assert refused.value.reason is ProjectionFailure.CAPACITY


@pytest.mark.parametrize("resource", ["variable_bytes", "entries"])
def test_projection_reserves_malformed_decoder_state_before_parsing(
    resource: str,
) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, None, installed(), context_for(capture), limits=LIMITS
        )
    content = b"invalid"
    owner = replace(
        metadata(prepared),
        content_digest=hashlib.sha256(content).hexdigest(),
        content_bytes=len(content),
    )
    variable, entries, _ = source_reservation(
        len(checked.canonical_contract) + len(checked.canonical_ir), validating=False
    )
    exact = (
        variable + RECORD_VARIABLE_BYTES
        if resource == "variable_bytes"
        else entries + RECORD_ENTRIES
    )
    too_small = replace(LIMITS, **{resource: exact - 1})
    admitted = replace(LIMITS, **{resource: exact})

    with pytest.raises(ProjectionRejected) as capacity:
        check_artifact_projection(checked, PATH, content, owner, limits=too_small)
    with pytest.raises(ProjectionRejected) as malformed:
        check_artifact_projection(checked, PATH, content, owner, limits=admitted)

    assert capacity.value.reason is ProjectionFailure.CAPACITY
    assert malformed.value.reason is ProjectionFailure.ENCODING


def test_projection_drops_temporary_schema_state_on_success_and_refusal() -> None:
    checked = projection_graph({"type": "integer"})
    value = {"private-projection-key": "private-projection-value"}
    baseline = sys.getrefcount(value)
    observer = CompositionObservation()
    previous = sys.getprofile()

    with projection_capture() as capture:
        sys.setprofile(observer.observe)
        try:
            prepared = prepare_artifact_projection(
                checked, PATH, 7, installed(), context_for(capture), limits=LIMITS
            )
            with pytest.raises(ProjectionRejected) as refused:
                prepare_artifact_projection(
                    checked,
                    PATH,
                    value,
                    installed(),
                    context_for(capture),
                    limits=LIMITS,
                )
        finally:
            sys.setprofile(previous)

    assert prepared.encoded()
    assert refused.value.__context__ is None
    assert sys.getrefcount(value) == baseline
    assert observer.sources
    assert all(reference() is None for reference in observer.sources)
    assert all(reference() is None for reference in observer.engines)
    assert all(reference() is None for reference in observer.errors)


def test_projection_keeps_unexpected_trusted_exception_with_the_caller() -> None:
    checked = projection_graph()
    provider = ObservedArtifactType(defect="raise")
    bindings = installed()
    identity = "kora.structured-output-validation.v1"
    bindings["validation", identity] = ValidationBinding(identity, provider.validate)

    with (
        projection_capture() as capture,
        pytest.raises(ValueError, match="private provider failure") as raised,
    ):
        prepare_artifact_projection(
            checked, PATH, None, bindings, context_for(capture), limits=LIMITS
        )

    assert type(raised.value) is ValueError
    assert provider.calls == ["validate"]
    assert raised.value.__traceback__ is not None


@pytest.mark.parametrize("broken", [False, True], ids=["blocked", "broken"])
def test_projection_survives_unavailable_diagnostic_sink(broken: bool) -> None:
    checked = projection_graph()
    with projection_capture() as capture:
        prepared = prepare_artifact_projection(
            checked, PATH, None, installed(), context_for(capture), limits=LIMITS
        )

    with diagnostics_pipe(blocked=not broken) as (diagnostics, pipe):
        if broken:
            pipe.close_read_end()
        else:
            wait_until(lambda: diagnostics.snapshot().in_flight_bytes > 0)
        restored = [
            check_artifact_projection(
                checked, PATH, prepared.encoded(), metadata(prepared), limits=LIMITS
            ).content_digest
            for _ in range(128)
        ]
        if broken:
            wait_until(lambda: diagnostics.snapshot().sink_failed)
        snapshot = diagnostics.snapshot()
        if not broken:
            pipe.reader.start()
        drained = diagnostics.shutdown()

    assert set(restored) == {prepared.content_digest}
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= 64
    assert snapshot.queue_bytes <= 65536
    assert drained
    assert not diagnostics.writer_is_alive()


def test_projection_debug_diagnostics_exclude_content() -> None:
    marker = "private-projection-marker"
    checked = projection_graph({"description": marker})

    with diagnostics_pipe() as (diagnostics, pipe):
        pipe.reader.start()
        prepared = prepare_artifact_projection(
            checked,
            PATH,
            marker,
            installed(),
            diagnostic_context(diagnostics),
            limits=LIMITS,
        )
        with pytest.raises(ProjectionRejected) as refused:
            check_artifact_projection(
                checked, PATH, marker, metadata(prepared), limits=LIMITS
            )
        assert diagnostics.shutdown()
        pipe.close_write_end()
        pipe.reader.wait_closed()
        records = [
            record
            for record in pipe.reader.records()
            if record["event"] == "engine.artifact_projection.completed"
        ]
        output = pipe.reader.data

    assert marker.encode() not in output
    assert marker not in repr(prepared)
    assert marker not in str(refused.value)
    assert [(item["operation"], item["outcome"]) for item in records] == [
        ("prepare", "prepared"),
        ("check", "binding"),
    ]
