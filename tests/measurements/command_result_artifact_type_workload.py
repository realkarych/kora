import dataclasses
import gc
import json
import tracemalloc
from collections.abc import Callable
from functools import partial
from typing import cast

import pytest

from kora.conformance.artifact_type import artifact_type_capture_settings
from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.ir import canonical_command_result, parse_command_result
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.command_result_cases import dense, fixture, stream_bytes, with_stream
from tests.measurements.artifact_type_workload import CYCLES, MEASURED, WARM_UP, measure
from tests.measurements.harness import Report, current_rss_bytes
from tests.plugins.artifact_type.command_result_support import binding


def refused_binding(supplied: BuiltinArtifactTypeBinding) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        CommandResultArtifactType({}, supplied)
    assert raised.value.code == "artifact_type_binding_incompatible"


def shapes() -> dict[str, tuple[JsonObject, JsonObject]]:
    capped = fixture()
    capped["stdout"] = {
        **stream_bytes(b"a" * 4096),
        "size": 67108864,
        "observed_bytes": 67108864,
    }
    crowded = dense()
    late = dense()
    cast("JsonObject", late["stderr"])["truncated"] = True
    return {
        "minimal": (
            fixture(),
            {str(index): None for index in range(20_000)},
        ),
        "tail_4kib": (
            with_stream(b"a" * 4096),
            {**fixture(), "resources": cast("list[JsonValue]", [None] * 10_000)},
        ),
        "capture_cap_descriptor": (
            capped,
            {**fixture(), "repo": "a" * 1_048_577},
        ),
        "dense": (crowded, late),
    }


def measure_shape(
    report: Report,
    provider: CommandResultArtifactType,
    context: ProtocolContext,
    shape: str,
    value: JsonObject,
    invalid: JsonObject,
) -> None:
    canonical = canonical_command_result(parse_command_result(value))
    assert provider.validate(value, context) == []
    assert provider.validate(invalid, context)
    report.environment[f"command_result_artifact_type_{shape}_canonical_bytes"] = len(
        canonical
    )
    actions: dict[str, Callable[[], object]] = {
        "validate": partial(provider.validate, value, context),
        "digest_fields": partial(provider.digest_fields, value, context),
        "summary": partial(provider.summary, value, context),
        "refuse": partial(provider.validate, invalid, context),
    }
    for operation, action in actions.items():
        measure(report, f"command_result_artifact_type_{operation}_{shape}", action)


def cycle(
    provider: CommandResultArtifactType,
    context: ProtocolContext,
    fixed: dict[str, int],
) -> None:
    value = fixture()
    assert provider.validate(value, context) == []
    projection = provider.summary(value, context)
    assert projection == value
    assert projection is not value
    assert provider.digest_fields(value, context) == [""]
    assert provider.validate({"invalid": "private-cost-marker"}, context)
    assert {name: id(value) for name, value in vars(provider).items()} == fixed


def collect(report: Report) -> None:
    supplied = binding()
    provider = CommandResultArtifactType({}, supplied)
    fixed = {name: id(value) for name, value in vars(provider).items()}
    workloads = shapes()
    wide: JsonObject = {f"extra{index}": index for index in range(20_000)}
    deep: JsonObject = {}
    for _ in range(2000):
        deep = {"unexpected": deep}
    report.environment["command_result_artifact_type_fixed_schema_json_bytes"] = len(
        json.dumps(provider.schema).encode()
    )
    report.environment["command_result_artifact_type_fixed_descriptor_json_bytes"] = (
        len(json.dumps(supplied.revision_descriptor).encode())
    )
    report.environment["command_result_artifact_type_measurement_warm_up"] = WARM_UP
    report.environment["command_result_artifact_type_measurement_calls"] = MEASURED
    report.environment["command_result_artifact_type_caller_held_shapes"] = len(
        workloads
    )
    report.environment["command_result_artifact_type_capture_cap_tail_bytes"] = 4096
    report.environment["command_result_artifact_type_caller_held_projections"] = 0
    first_sample = len(report.samples)

    with captured(
        CONFORMANCE_COMPONENT,
        artifact_type_capture_settings(),
        "command-result-artifact-cost",
        utc_now,
    ) as capture:
        context = context_for(capture)
        measure(
            report,
            "command_result_artifact_type_bind",
            lambda: CommandResultArtifactType({}, supplied),
        )
        for shape, (value, invalid) in workloads.items():
            measure_shape(report, provider, context, shape, value, invalid)
        for shape, schema in {"wide": wide, "deep": deep}.items():
            incompatible = dataclasses.replace(supplied, schema=schema)
            measure(
                report,
                f"command_result_artifact_type_binding_refused_{shape}",
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

        report.environment["command_result_artifact_type_cycles"] = CYCLES
        report.environment[
            "command_result_artifact_type_settled_traced_delta_bytes"
        ] = after - before
        report.environment["command_result_artifact_type_cycles_traced_peak_bytes"] = (
            peak
        )
        report.environment["command_result_artifact_type_observer_capture_bytes"] = len(
            capture.reader.data
        )
        report.environment["command_result_artifact_type_observer_capture_records"] = (
            len(records)
        )
        report.environment["command_result_artifact_type_observer_samples"] = (
            len(report.samples) - first_sample
        )
        report.environment["command_result_artifact_type_settled_rss_bytes"] = (
            current_rss_bytes()
        )
        state = capture.diagnostics.snapshot()
        report.environment["command_result_artifact_type_settled_queue_bytes"] = (
            state.queue_bytes
        )
        report.environment["command_result_artifact_type_settled_in_flight_bytes"] = (
            state.in_flight_bytes
        )
        assert state.queue_records == state.queue_bytes == state.in_flight_bytes == 0
        assert_capture_released(capture)
    assert len(report.samples) - first_sample == 19
    assert {name: id(value) for name, value in vars(provider).items()} == fixed
