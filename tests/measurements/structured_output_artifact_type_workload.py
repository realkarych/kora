import dataclasses
import gc
import json
import tracemalloc
from collections.abc import Callable
from functools import partial

import pytest

from kora.conformance.artifact_type import artifact_type_capture_settings
from kora.conformance.capture import captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_json import MAX_JSON_BYTES, encode_snapshot
from kora.plugins.artifact_type.structured_output import StructuredOutputArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.structured_output_cases import exact_string
from tests.measurements.artifact_type_workload import CYCLES, MEASURED, WARM_UP, measure
from tests.measurements.harness import Report, current_rss_bytes
from tests.plugins.artifact_type.structured_output_support import binding


def refused_binding(supplied: BuiltinArtifactTypeBinding) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        StructuredOutputArtifactType({}, supplied)
    assert raised.value.code == "artifact_type_binding_incompatible"


def late_refusal(size: int) -> JsonValue:
    return [exact_string(size - 7), float("inf")]


def shapes() -> dict[int, tuple[JsonValue, JsonValue]]:
    return {
        1024: (exact_string(1024), {"private-cost-marker": float("inf")}),
        65_536: (exact_string(65_536), late_refusal(65_536)),
        MAX_JSON_BYTES - 1: (
            exact_string(MAX_JSON_BYTES - 1),
            late_refusal(MAX_JSON_BYTES - 1),
        ),
        MAX_JSON_BYTES: (
            exact_string(MAX_JSON_BYTES),
            exact_string(MAX_JSON_BYTES + 1),
        ),
    }


def measure_shape(
    report: Report,
    provider: StructuredOutputArtifactType,
    context: ProtocolContext,
    size: int,
    value: JsonValue,
    invalid: JsonValue,
) -> None:
    assert len(encode_snapshot(value)) == size
    assert provider.validate(value, context) == []
    assert provider.validate(invalid, context)
    report.environment[f"structured_output_artifact_type_{size}_canonical_bytes"] = size
    report.environment[f"structured_output_artifact_type_{size}_refusal"] = (
        "canonical_over_limit"
        if size == MAX_JSON_BYTES
        else "late_native_domain"
        if size != 1024
        else "native_domain"
    )
    actions: dict[str, Callable[[], object]] = {
        "validate": partial(provider.validate, value, context),
        "digest_fields": partial(provider.digest_fields, value, context),
        "summary": partial(provider.summary, value, context),
        "refuse": partial(provider.validate, invalid, context),
    }
    for operation, action in actions.items():
        measure(report, f"structured_output_artifact_type_{operation}_{size}", action)


def lifecycle_payload() -> JsonObject:
    value: JsonObject = {"data": [1, 1.0, -0.0, None, "\0"], "padding": ""}
    value["padding"] = exact_string(1024 - len(encode_snapshot(value)) + 2)
    assert len(encode_snapshot(value)) == 1024
    return value


def cycle(
    provider: StructuredOutputArtifactType,
    context: ProtocolContext,
    value: JsonObject,
    invalid: JsonValue,
    expected: bytes,
    fixed_ids: dict[str, int],
    fixed_state: dict[str, JsonObject],
) -> None:
    assert provider.validate(value, context) == []
    projection = provider.summary(value, context)
    assert encode_snapshot(projection) == expected
    assert projection is not value
    assert provider.digest_fields(value, context) == [""]
    assert provider.validate(invalid, context)
    assert {name: id(item) for name, item in vars(provider).items()} == fixed_ids
    assert vars(provider) == fixed_state


def collect(report: Report) -> None:
    supplied = binding()
    provider = StructuredOutputArtifactType({}, supplied)
    fixed_ids = {name: id(item) for name, item in vars(provider).items()}
    fixed_state: dict[str, JsonObject] = {
        "_schema": supplied.schema,
        "_revision_descriptor": supplied.revision_descriptor,
        "config_schema": {"type": "object", "additionalProperties": False},
    }
    assert set(fixed_ids) == set(fixed_state)
    assert vars(provider) == fixed_state
    workloads = shapes()
    lifetime = lifecycle_payload()
    invalid = late_refusal(1024)
    expected = encode_snapshot(lifetime)
    wide: JsonObject = {f"extra{index}": index for index in range(20_000)}
    deep: JsonObject = {}
    for _ in range(2000):
        deep = {"unexpected": deep}
    report.environment["structured_output_artifact_type_fixed_schema_json_bytes"] = len(
        json.dumps(provider.schema).encode()
    )
    report.environment[
        "structured_output_artifact_type_fixed_descriptor_json_bytes"
    ] = len(json.dumps(supplied.revision_descriptor).encode())
    report.environment["structured_output_artifact_type_fixed_config_json_bytes"] = len(
        json.dumps(provider.config_schema).encode()
    )
    report.environment["structured_output_artifact_type_measurement_warm_up"] = WARM_UP
    report.environment["structured_output_artifact_type_measurement_calls"] = MEASURED
    report.environment["structured_output_artifact_type_cpu_observation"] = (
        "process_time under tracemalloc and DEBUG capture includes writer-thread CPU"
    )
    report.environment["structured_output_artifact_type_caller_held_shapes"] = len(
        workloads
    )
    report.environment["structured_output_artifact_type_caller_held_logical_values"] = (
        len(workloads) * 2 + 2
    )
    report.environment[
        "structured_output_artifact_type_caller_held_reference_canonical_bytes"
    ] = len(expected)
    report.environment["structured_output_artifact_type_caller_held_projections"] = 0
    report.environment["structured_output_artifact_type_lifecycle_payload_bytes"] = len(
        expected
    )
    report.environment["structured_output_artifact_type_observation_classification"] = (
        "CPU, traced peak/retained delta and RSS are diagnostics without thresholds"
    )
    first_sample = len(report.samples)

    with captured(
        CONFORMANCE_COMPONENT,
        artifact_type_capture_settings(),
        "structured-output-artifact-cost",
        utc_now,
    ) as capture:
        context = context_for(capture)
        measure(
            report,
            "structured_output_artifact_type_bind",
            lambda: StructuredOutputArtifactType({}, supplied),
        )
        for size, (value, refused) in workloads.items():
            measure_shape(report, provider, context, size, value, refused)
        for shape, schema in {"wide": wide, "deep": deep}.items():
            incompatible = dataclasses.replace(supplied, schema=schema)
            measure(
                report,
                f"structured_output_artifact_type_binding_refused_{shape}",
                lambda incompatible=incompatible: refused_binding(incompatible),
            )
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]

        for _ in range(CYCLES):
            cycle(
                provider, context, lifetime, invalid, expected, fixed_ids, fixed_state
            )
        records = capture.finish()
        gc.collect()
        after, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        report.environment["structured_output_artifact_type_cycles"] = CYCLES
        report.environment[
            "structured_output_artifact_type_settled_traced_delta_bytes"
        ] = after - before
        report.environment[
            "structured_output_artifact_type_cycles_traced_peak_bytes"
        ] = peak
        report.environment["structured_output_artifact_type_observer_capture_bytes"] = (
            len(capture.reader.data)
        )
        report.environment[
            "structured_output_artifact_type_observer_capture_records"
        ] = len(records)
        report.environment["structured_output_artifact_type_observer_samples"] = (
            len(report.samples) - first_sample
        )
        rss = current_rss_bytes()
        report.environment["structured_output_artifact_type_settled_rss_bytes"] = rss
        report.environment["structured_output_artifact_type_settled_rss_available"] = (
            rss is not None
        )
        state = capture.diagnostics.snapshot()
        report.environment["structured_output_artifact_type_settled_queue_bytes"] = (
            state.queue_bytes
        )
        report.environment[
            "structured_output_artifact_type_settled_in_flight_bytes"
        ] = state.in_flight_bytes
        report.environment["structured_output_artifact_type_writer_alive"] = (
            capture.diagnostics.writer_is_alive()
        )
        assert state.queue_records == state.queue_bytes == state.in_flight_bytes == 0
        assert_capture_released(capture)
    assert len(report.samples) - first_sample == 19
    assert {name: id(item) for name, item in vars(provider).items()} == fixed_ids
    assert vars(provider) == fixed_state
