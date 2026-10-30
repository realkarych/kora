import dataclasses
import gc
import json
import time
import tracemalloc
from collections.abc import Callable
from functools import partial
from typing import cast

import pytest

from kora.conformance.artifact_type import artifact_type_capture_settings
from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from tests.ir.vcs_ref_cases import exact_size, wire
from tests.measurements.harness import Report, Sample, current_rss_bytes
from tests.plugins.artifact_type.support import binding, payload

WARM_UP: int = 5
MEASURED: int = 30
CYCLES: int = 100


def refused_binding(supplied: BuiltinArtifactTypeBinding) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        VcsRefArtifactType({}, supplied)
    assert raised.value.code == "artifact_type_binding_incompatible"


def measure(report: Report, name: str, action: Callable[[], object]) -> None:
    for _ in range(WARM_UP):
        action()
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    started = time.process_time()
    for _ in range(MEASURED):
        action()
    seconds = time.process_time() - started
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report.samples.append(
        Sample(
            name,
            0,
            MEASURED,
            seconds,
            {
                "traced_retained_delta_bytes": retained - before,
                "traced_peak_bytes": peak,
                "rss_bytes": current_rss_bytes(),
            },
        )
    )


def measure_shape(
    report: Report, provider: VcsRefArtifactType, context: ProtocolContext, size: int
) -> None:
    value = cast("JsonValue", exact_size(size))
    invalid = cast("JsonObject", exact_size(size))
    invalid["commit"] = None
    assert len(wire(value)) == size
    assert provider.validate(value, context) == []
    assert provider.validate(invalid, context)
    actions: dict[str, Callable[[], object]] = {
        "validate": partial(provider.validate, value, context),
        "summary": partial(provider.summary, value, context),
        "digest_fields": partial(provider.digest_fields, value, context),
        "refuse": partial(provider.validate, invalid, context),
    }
    for operation, action in actions.items():
        measure(report, f"artifact_type_{operation}_{size}", action)


def cycle(
    provider: VcsRefArtifactType, context: ProtocolContext, fixed: dict[str, int]
) -> None:
    value = payload()
    assert provider.validate(value, context) == []
    summary = provider.summary(value, context)
    assert summary == value
    assert summary is not value
    assert provider.digest_fields(value, context) == [""]
    assert provider.validate({"invalid": "private-cost-marker"}, context)
    assert {name: id(value) for name, value in vars(provider).items()} == fixed


def collect(report: Report) -> None:
    supplied = binding()
    provider = VcsRefArtifactType({}, supplied)
    fixed = {name: id(value) for name, value in vars(provider).items()}
    report.environment["artifact_type_fixed_schema_json_bytes"] = len(
        json.dumps(provider.schema).encode()
    )
    report.environment["artifact_type_fixed_descriptor_json_bytes"] = len(
        json.dumps(supplied.revision_descriptor).encode()
    )
    report.environment["artifact_type_measurement_warm_up"] = WARM_UP
    report.environment["artifact_type_measurement_calls"] = MEASURED
    with captured(
        CONFORMANCE_COMPONENT,
        artifact_type_capture_settings(),
        "artifact-cost",
        utc_now,
    ) as capture:
        context = context_for(capture)
        measure(report, "artifact_type_bind", lambda: VcsRefArtifactType({}, supplied))
        for size in (1024, 65_536, 1_048_575, 1_048_576):
            measure_shape(report, provider, context, size)
        wide: JsonObject = {f"extra{index}": index for index in range(20_000)}
        deep: JsonObject = {}
        for _ in range(2000):
            deep = {"unexpected": deep}
        for shape, schema in {"wide": wide, "deep": deep}.items():
            incompatible = dataclasses.replace(supplied, schema=schema)
            measure(
                report,
                f"artifact_type_binding_refused_{shape}",
                lambda incompatible=incompatible: refused_binding(incompatible),
            )
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(CYCLES):
            cycle(provider, context, fixed)
        records = capture.finish()
        gc.collect()
        after, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report.environment["artifact_type_cycles"] = CYCLES
        report.environment["artifact_type_settled_traced_delta_bytes"] = after - before
        report.environment["artifact_type_cycles_traced_peak_bytes"] = peak
        report.environment["artifact_type_observer_capture_bytes"] = len(
            capture.reader.data
        )
        report.environment["artifact_type_observer_capture_records"] = len(records)
        report.environment["artifact_type_observer_samples"] = len(report.samples)
        report.environment["artifact_type_settled_rss_bytes"] = current_rss_bytes()
        assert not capture.diagnostics.writer_is_alive()
    assert {name: id(value) for name, value in vars(provider).items()} == fixed
