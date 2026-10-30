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
from kora.ir import (
    canonical_agent_session,
    parse_agent_session,
    summarize_agent_session,
)
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.artifact_type.agent_session import AgentSessionArtifactType
from kora.protocols.artifact_type import BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.ir.agent_session_cases import dense, fixture, metadata
from tests.measurements.artifact_type_workload import CYCLES, MEASURED, WARM_UP, measure
from tests.measurements.harness import Report, current_rss_bytes
from tests.plugins.artifact_type.agent_session_support import (
    binding,
    late_refusal,
    transcript_descriptor,
)


def refused_binding(supplied: BuiltinArtifactTypeBinding) -> None:
    with pytest.raises(KoraProtocolError) as raised:
        AgentSessionArtifactType({}, supplied)
    assert raised.value.code == "artifact_type_binding_incompatible"


def shapes() -> dict[str, tuple[JsonObject, JsonObject]]:
    crowded = dense()
    crowded["transcript"] = transcript_descriptor()
    truncated = fixture()
    truncated["transcript"] = transcript_descriptor(truncated=True)
    too_many_models = fixture()
    metadata(too_many_models)["usage"] = cast("list[JsonValue]", [None] * 65)
    too_many_denials = fixture()
    cast("JsonObject", too_many_denials["denials"])["items"] = cast(
        "list[JsonValue]", [None] * 1001
    )
    return {
        "minimal_unavailable": (
            fixture("unavailable-no-usage"),
            {str(index): None for index in range(20_000)},
        ),
        "captured_empty": (fixture(), too_many_models),
        "dense_complete_descriptor": (crowded, late_refusal(crowded)),
        "truncated_descriptor": (truncated, too_many_denials),
    }


def measure_shape(
    report: Report,
    provider: AgentSessionArtifactType,
    context: ProtocolContext,
    shape: str,
    value: JsonObject,
    invalid: JsonObject,
) -> None:
    canonical = canonical_agent_session(parse_agent_session(value))
    assert provider.validate(value, context) == []
    assert provider.validate(invalid, context)
    prefix = f"agent_session_artifact_type_{shape}"
    report.environment[f"{prefix}_canonical_bytes"] = len(canonical)
    report.environment[f"{prefix}_usage_models"] = len(
        cast("list[JsonValue]", metadata(value)["usage"])
    )
    report.environment[f"{prefix}_denial_items"] = len(
        cast("list[JsonValue]", cast("JsonObject", value["denials"])["items"])
    )
    transcript = cast("JsonObject", value["transcript"])
    retained = transcript.get("retained_size")
    source = transcript.get("source_size")
    assert retained is None or type(retained) is int
    assert source is None or type(source) is int
    report.environment[f"{prefix}_retained_transcript_descriptor_bytes"] = retained
    report.environment[f"{prefix}_source_transcript_descriptor_bytes"] = source
    actions: dict[str, Callable[[], object]] = {
        "validate": partial(provider.validate, value, context),
        "digest_fields": partial(provider.digest_fields, value, context),
        "summary": partial(provider.summary, value, context),
        "refuse": partial(provider.validate, invalid, context),
    }
    for operation, action in actions.items():
        measure(report, f"agent_session_artifact_type_{operation}_{shape}", action)


def cycle(
    provider: AgentSessionArtifactType,
    context: ProtocolContext,
    value: JsonObject,
    invalid: JsonObject,
    expected_summary: JsonObject,
    fixed_ids: dict[str, int],
    fixed_state: dict[str, JsonObject],
) -> None:
    assert provider.validate(value, context) == []
    projection = provider.summary(value, context)
    assert projection == expected_summary
    assert projection is not value
    assert provider.digest_fields(value, context) == [""]
    assert provider.validate(invalid, context)
    assert {name: id(value) for name, value in vars(provider).items()} == fixed_ids
    assert vars(provider) == fixed_state


def collect(report: Report) -> None:
    supplied = binding()
    provider = AgentSessionArtifactType({}, supplied)
    fixed_ids = {name: id(value) for name, value in vars(provider).items()}
    fixed_state: dict[str, JsonObject] = {
        "_schema": supplied.schema,
        "_revision_descriptor": supplied.revision_descriptor,
        "config_schema": {"type": "object", "additionalProperties": False},
    }
    assert set(fixed_ids) == set(fixed_state)
    assert vars(provider) == fixed_state
    workloads = shapes()
    lifetime = (
        workloads["minimal_unavailable"][0],
        workloads["dense_complete_descriptor"][0],
    )
    late_refusals = tuple(late_refusal(value) for value in lifetime)
    expected_summaries = tuple(
        summarize_agent_session(parse_agent_session(value)) for value in lifetime
    )
    wide: JsonObject = {f"extra{index}": index for index in range(20_000)}
    deep: JsonObject = {}
    for _ in range(2000):
        deep = {"unexpected": deep}
    report.environment["agent_session_artifact_type_fixed_schema_json_bytes"] = len(
        json.dumps(provider.schema).encode()
    )
    report.environment["agent_session_artifact_type_fixed_descriptor_json_bytes"] = len(
        json.dumps(supplied.revision_descriptor).encode()
    )
    report.environment["agent_session_artifact_type_fixed_config_json_bytes"] = len(
        json.dumps(provider.config_schema).encode()
    )
    report.environment["agent_session_artifact_type_measurement_warm_up"] = WARM_UP
    report.environment["agent_session_artifact_type_measurement_calls"] = MEASURED
    report.environment["agent_session_artifact_type_cpu_observation"] = (
        "process_time under tracemalloc and DEBUG capture includes writer-thread CPU"
    )
    report.environment["agent_session_artifact_type_caller_held_shapes"] = len(
        workloads
    )
    report.environment["agent_session_artifact_type_caller_held_logical_values"] = len(
        workloads
    ) * 2 + len(late_refusals)
    report.environment[
        "agent_session_artifact_type_caller_held_reference_summaries"
    ] = len(expected_summaries)
    report.environment["agent_session_artifact_type_caller_held_projections"] = 0
    report.environment["agent_session_artifact_type_transcript_bytes_allocated"] = 0
    first_sample = len(report.samples)

    with captured(
        CONFORMANCE_COMPONENT,
        artifact_type_capture_settings(),
        "agent-session-artifact-cost",
        utc_now,
    ) as capture:
        context = context_for(capture)
        measure(
            report,
            "agent_session_artifact_type_bind",
            lambda: AgentSessionArtifactType({}, supplied),
        )
        for shape, (value, invalid) in workloads.items():
            measure_shape(report, provider, context, shape, value, invalid)
        for shape, schema in {"wide": wide, "deep": deep}.items():
            incompatible = dataclasses.replace(supplied, schema=schema)
            measure(
                report,
                f"agent_session_artifact_type_binding_refused_{shape}",
                lambda incompatible=incompatible: refused_binding(incompatible),
            )
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]

        for index in range(CYCLES):
            position = index % len(lifetime)
            cycle(
                provider,
                context,
                lifetime[position],
                late_refusals[position],
                expected_summaries[position],
                fixed_ids,
                fixed_state,
            )
        records = capture.finish()
        gc.collect()
        after, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        report.environment["agent_session_artifact_type_cycles"] = CYCLES
        report.environment["agent_session_artifact_type_settled_traced_delta_bytes"] = (
            after - before
        )
        report.environment["agent_session_artifact_type_cycles_traced_peak_bytes"] = (
            peak
        )
        report.environment["agent_session_artifact_type_observer_capture_bytes"] = len(
            capture.reader.data
        )
        report.environment["agent_session_artifact_type_observer_capture_records"] = (
            len(records)
        )
        report.environment["agent_session_artifact_type_observer_samples"] = (
            len(report.samples) - first_sample
        )
        rss = current_rss_bytes()
        report.environment["agent_session_artifact_type_settled_rss_bytes"] = rss
        report.environment["agent_session_artifact_type_settled_rss_available"] = (
            rss is not None
        )
        state = capture.diagnostics.snapshot()
        report.environment["agent_session_artifact_type_settled_queue_bytes"] = (
            state.queue_bytes
        )
        report.environment["agent_session_artifact_type_settled_in_flight_bytes"] = (
            state.in_flight_bytes
        )
        assert state.queue_records == state.queue_bytes == state.in_flight_bytes == 0
        assert_capture_released(capture)
    assert len(report.samples) - first_sample == 19
    assert {name: id(value) for name, value in vars(provider).items()} == fixed_ids
    assert vars(provider) == fixed_state
