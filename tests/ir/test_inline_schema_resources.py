import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import validate_inline_schemas
from kora.ir.schema_keywords import SafeArray, SafeObject
from tests.ir.inline_schema_cases import document_for, many_roots, shared_body
from tests.ir.type_schema_cases import nested_schema, wide_schema
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_inline_schema_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "roots_1": many_roots(1),
        "roots_100": many_roots(100),
        "roots_1000": many_roots(1000),
        "depth_1200": document_for(nested_schema(1200)),
        "shared_refs_1000": document_for(wide_schema(1000)),
        "shared_body_1000": shared_body(1000),
        "faults_3000": document_for({"type": ["invalid" for _ in range(3000)]}),
    }
    expected_counts = {
        "roots_1": (1, 0, 1, 1, 0),
        "roots_100": (100, 0, 100, 100, 0),
        "roots_1000": (1000, 0, 1000, 1000, 0),
        "depth_1200": (1, 0, 1, 1201, 0),
        "shared_refs_1000": (1, 0, 1, 1002, 1000),
        "shared_body_1000": (1001, 1, 1, 1, 0),
        "faults_3000": (1, 0, 1, 1, 0),
    }
    report = new_report()
    if not report.revision:
        report.revision = os.environ.get("GITHUB_SHA", "local-uncommitted")
    report.environment.update(
        classification="diagnostic",
        warmup_per_workload=1,
        timed_repeats=3,
        memory_batches=6,
        validations_per_batch=len(workloads),
        input_construction="excluded",
        traced_timing="excluded",
        intentional_cache="stdlib regex entry-bounded cache; no fixed byte bound",
    )
    for document in workloads.values():
        validate_inline_schemas(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_inline_schemas(document)
            elapsed = time.perf_counter() - started
            counts = (
                result.nodes_checked,
                result.definitions_checked,
                result.inline_schemas_checked,
                result.schemas_checked,
                result.references_checked,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.schemas_checked,
                    elapsed,
                    {
                        "nodes": counts[0],
                        "definitions": counts[1],
                        "roots": counts[2],
                        "schemas": counts[3],
                        "references": counts[4],
                    },
                )
            )
            assert result.inline_schemas_valid is (name != "faults_3000")
            assert counts == expected_counts[name]
            del result
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        report.environment["rss_before_bytes"] = current_rss_bytes()
        for batch in range(6):
            for document in workloads.values():
                validate_inline_schemas(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 21
    record_testsuite_property("inline_schema_resources", report.to_json())


@pytest.mark.parametrize("invalid", [False, True])
def test_inline_schema_validation_releases_inputs_views_and_results(
    invalid: bool,
) -> None:
    schema = wide_schema(100, invalid=invalid)
    schema["pattern"] = "(" * 1200 if invalid else "[a-z]"
    document = document_for(schema, source={"file": "graph.py", "line": 1})
    assert document.graph.params is not MISSING
    assert document.graph.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(document.graph.params),
        weakref.ref(document.graph.source),
    ]
    del schema

    result = validate_inline_schemas(document)
    diagnostic_references = [weakref.ref(item) for item in result.diagnostics.errors]
    source_references = [
        weakref.ref(item.location)
        for item in result.diagnostics.errors
        if item.location is not None
    ]
    del document
    gc.collect()
    inputs_released = all(reference() is None for reference in references)
    remaining_views = sum(
        isinstance(value, SafeObject | SafeArray) for value in gc.get_objects()
    )
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert inputs_released
    assert remaining_views == 0
    assert bool(diagnostic_references) is invalid
    assert all(
        reference() is None for reference in diagnostic_references + source_references
    )
    assert result_reference() is None
