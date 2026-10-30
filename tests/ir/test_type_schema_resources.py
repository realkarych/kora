import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest

from kora.ir import validate_type_schemas
from kora.ir.contracts import CustomTypeContract
from kora.ir.schema_keywords import SafeArray, SafeObject
from tests.ir.type_schema_cases import document_for, nested_schema, wide_schema
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_type_schema_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "nodes_1": document_for({}),
        "nodes_100": document_for({"allOf": [True for _ in range(99)]}),
        "nodes_1000": document_for({"allOf": [True for _ in range(999)]}),
        "depth_1200": document_for(nested_schema(1200)),
        "shared_refs_1000": document_for(wide_schema(1000)),
        "faults_3000": document_for({"type": ["invalid" for _ in range(3000)]}),
    }
    expected_nodes = {
        "nodes_1": 1,
        "nodes_100": 100,
        "nodes_1000": 1000,
        "depth_1200": 1201,
        "shared_refs_1000": 1002,
        "faults_3000": 1,
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
        validate_type_schemas(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_type_schemas(document)
            elapsed = time.perf_counter() - started
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.schemas_checked,
                    elapsed,
                    {
                        "schemas": result.schemas_checked,
                        "references": result.references_checked,
                    },
                )
            )
            assert result.schemas_valid is (name != "faults_3000")
            assert result.schemas_checked == expected_nodes[name]
            assert result.references_checked == (
                1000 if name == "shared_refs_1000" else 0
            )
            del result
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        report.environment["rss_before_bytes"] = current_rss_bytes()
        for batch in range(6):
            for document in workloads.values():
                validate_type_schemas(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 18
    record_testsuite_property("type_schema_resources", report.to_json())


@pytest.mark.parametrize("invalid", [False, True])
def test_type_schema_validation_releases_owned_references(invalid: bool) -> None:
    schema = wide_schema(100, invalid=invalid)
    schema["pattern"] = "(" * 1200 if invalid else "[a-z]"
    document = document_for(schema)
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
    ]
    references.extend(
        weakref.ref(contract) for contract in document.type_contracts.values()
    )
    references.extend(
        weakref.ref(contract.schema_)
        for contract in document.type_contracts.values()
        if isinstance(contract, CustomTypeContract)
    )
    del schema

    result = validate_type_schemas(document)
    diagnostic_references = [weakref.ref(item) for item in result.diagnostics.errors]
    del document
    gc.collect()
    inputs_released = all(reference() is None for reference in references)
    remaining_views = sum(
        isinstance(value, SafeObject | SafeArray) for value in gc.get_objects()
    )
    del result
    gc.collect()

    assert inputs_released
    assert remaining_views == 0
    assert bool(diagnostic_references) is invalid
    assert all(reference() is None for reference in diagnostic_references)
