import sys
import weakref
from dataclasses import dataclass, field, fields
from types import CodeType, FrameType, FunctionType
from typing import cast

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.ir.standard_schema_source import Sources
from kora.observability import Level, ProcessDiagnostics
from kora.observability.context import bound_scope
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.effective_artifact_type import (
    EffectiveArtifactType,
    EffectiveValid,
    prepare_effective_artifact_type,
)
from kora.protocols.time import SystemClock
from tests.conformance.artifact_types import payload
from tests.ir.standard_output_cases import output_input
from tests.ir.structured_schema_cases import builtin, contract
from tests.ir.test_standard_schema_lifetime import ReleaseObservation
from tests.ir.test_standard_schema_source_lifetime import diagnostics_pipe
from tests.observability.conftest import wait_until
from tests.protocols.effective_artifact_cases import (
    ObservedArtifactType,
    install_custom,
    installed,
    prepared,
)


def diagnostic_context(diagnostics: ProcessDiagnostics) -> ProtocolContext:
    return ProtocolContext(
        namespace_id="effective-test",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger("artifact_type.structured_output"),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=None,
        clock=SystemClock(),
        activity=NoActivity(),
    )


@dataclass
class CompositionObservation(ReleaseObservation):
    sources: list[weakref.ReferenceType[Sources]] = field(
        default_factory=list[weakref.ReferenceType[Sources]]
    )
    source_return_owners: list[int] = field(default_factory=list[int])
    decision_calls: int = 0
    decision_code: CodeType = cast(
        "FunctionType", object.__getattribute__(EffectiveArtifactType, "_decide")
    ).__code__

    def observe(self, frame: FrameType, event: str, argument: object) -> None:
        super().observe(frame, event, argument)
        if frame.f_code is Sources.__init__.__code__ and event == "return":
            self.sources.append(weakref.ref(cast("Sources", frame.f_locals["self"])))
        if frame.f_code is self.decision_code and event == "return":
            self.decision_calls += 1
            if "source" in frame.f_locals:
                self.source_return_owners.append(
                    sys.getrefcount(frame.f_locals["source"])
                )


def retained_failure(
    operation: EffectiveArtifactType, value: object, ctx: ProtocolContext
) -> ValueError:
    try:
        operation.validate(value, ctx)
    except ValueError as error:
        return error
    raise AssertionError("the provider did not fail")


def test_effective_schema_operation_releases_temporary_state(
    effective_capture: Capture,
) -> None:
    provider = ObservedArtifactType()
    provider_reference = weakref.ref(provider)
    bindings = installed()
    install_custom(bindings, provider)
    candidate = output_input(
        records=[builtin(), contract({"properties": {"left": {"const": "first"}}})]
    )
    operation = prepare_effective_artifact_type(candidate, bindings)
    assert isinstance(operation, EffectiveArtifactType)
    del bindings
    ctx = context_for(effective_capture)
    value = payload()
    baseline = sys.getrefcount(value)
    observation = CompositionObservation()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        for supplied, expected in (
            (value, "valid"),
            ({"left": "second", "right": None}, "invalid"),
            ({"left": [0] * 65537, "right": None}, "refused"),
            (value, "valid"),
        ):
            result = operation.validate(supplied, ctx)
            assert result.outcome == expected
            if isinstance(result, EffectiveValid):
                result.digest_fields(ctx)
                result.summary(ctx)
            del result
            assert all(reference() is None for reference in observation.sources)
            assert all(reference() is None for reference in observation.engines)
            assert all(reference() is None for reference in observation.errors)
            assert all(reference() is None for reference in observation.generators)
        del supplied
        provider.defect = "raise"
        failure = retained_failure(operation, value, ctx)
        retained_count = sys.getrefcount(value)
        del failure
        provider.defect = "none"
        result = operation.validate(value, ctx)
        assert isinstance(result, EffectiveValid)
        del result
    finally:
        sys.setprofile(previous)
    after = sys.getrefcount(value)
    del provider
    held_provider = provider_reference() is not None
    holder_fields = tuple(item.name for item in fields(operation))
    del operation

    assert baseline == after
    assert retained_count > baseline
    assert held_provider
    assert provider_reference() is None
    assert observation.sources
    assert observation.source_return_owners
    assert set(observation.source_return_owners) == {3}
    assert observation.decision_calls == 6
    assert holder_fields == (
        "source_bytes",
        "validations",
        "selection",
        "summarize",
        "accounting",
    )
    assert all(reference() is None for reference in observation.sources)
    assert all(reference() is None for reference in observation.engines)
    assert all(reference() is None for reference in observation.errors)


def test_effective_shape_refusal_precedes_numerical_views(
    effective_capture: Capture,
) -> None:
    operation = prepared()
    observation = CompositionObservation()
    previous = sys.getprofile()

    sys.setprofile(observation.observe)
    try:
        result = operation.validate([0] * 65537, context_for(effective_capture))
    finally:
        sys.setprofile(previous)

    assert result.outcome == "refused"
    assert observation.view_calls == 0
    assert observation.engines == []
    assert observation.source_return_owners == [3]


@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
def test_effective_diagnostics_exclude_content(
    level: Level, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "private_effective_schema_marker"
    candidate = output_input({"description": marker, "type": "number"})

    with diagnostics_pipe(level) as (diagnostics, pipe):
        ctx = diagnostic_context(diagnostics)
        pipe.reader.start()
        with bound_scope(request_id="effective-request"):
            try:
                raise RuntimeError(marker)
            except RuntimeError:
                operation = prepare_effective_artifact_type(candidate, installed())
                assert isinstance(operation, EffectiveArtifactType)
                accepted = operation.validate(0.3, ctx)
                assert isinstance(accepted, EffectiveValid)
                accepted.digest_fields(ctx)
                accepted.summary(ctx)
                rejected = operation.validate(marker, ctx)
                refused = operation.validate([0] * 65537, ctx)
        assert diagnostics.shutdown()
        pipe.close_write_end()
        pipe.reader.wait_closed()
        records = [
            record
            for record in pipe.reader.records()
            if record["event"] == "protocols.effective_artifact_type.completed"
        ]
        output = pipe.reader.data

    assert rejected.outcome == "invalid"
    assert refused.outcome == "refused"
    assert marker.encode() not in output
    assert marker not in repr(operation)
    assert capsys.readouterr().out == ""
    assert len(records) == (6 if level is Level.DEBUG else 0)
    assert all(record["request_id"] == "effective-request" for record in records)
    assert all(
        record["component"] == "protocols.effective_artifact_type" for record in records
    )


@pytest.mark.parametrize("broken", [False, True], ids=["blocked", "broken"])
def test_effective_composition_survives_unavailable_sink(broken: bool) -> None:
    operation = prepared(output_input({"multipleOf": 0.1}))

    with diagnostics_pipe(blocked=not broken) as (diagnostics, pipe):
        ctx = diagnostic_context(diagnostics)
        if broken:
            pipe.close_read_end()
        else:
            wait_until(lambda: diagnostics.snapshot().in_flight_bytes > 0)
        outcomes = [operation.validate(0.3, ctx).outcome for _ in range(256)]
        if broken:
            wait_until(lambda: diagnostics.snapshot().sink_failed)
        snapshot = diagnostics.snapshot()
        if not broken:
            pipe.reader.start()
        drained = diagnostics.shutdown()

    assert set(outcomes) == {"valid"}
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= 64
    assert snapshot.queue_bytes <= 65536
    assert drained
    assert not diagnostics.writer_is_alive()
