import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel.paths import Path, PathParser
from kora.ir import HumanInputNode, validate_input_paths
from kora.ir.expression_fields import SyntaxField
from kora.ir.input_paths import InputPathChecks
from tests.ir.input_path_cases import binding_document, bindings, shared_bindings
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_input_path_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    boundary = "inputs.data['" + "x" * (4096 - len("inputs.data['']")) + "']"
    workloads = {
        "bindings_1": bindings(1),
        "bindings_100": bindings(100),
        "bindings_1000": bindings(1000),
        "shared_body_1000": shared_bindings(1000),
        "length_boundary_100": bindings(100, boundary),
        "faults_3000": bindings(3000, "inputs.missing"),
    }
    expected_counts = {
        "bindings_1": (1, 0, 1, 1, 0),
        "bindings_100": (100, 0, 100, 100, 0),
        "bindings_1000": (1000, 0, 1000, 1000, 0),
        "shared_body_1000": (1001, 1, 1, 1, 0),
        "length_boundary_100": (100, 0, 100, 100, 0),
        "faults_3000": (3000, 0, 3000, 3000, 0),
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
        intentional_cache="none owned by input path validation",
        peak_scope="cumulative since tracing start",
    )
    for graph in workloads.values():
        validate_input_paths(graph)

    for name, graph in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_input_paths(graph)
            elapsed = time.perf_counter() - started
            counts = (
                result.nodes_checked,
                result.definitions_checked,
                result.paths_checked,
                result.bindings_checked,
                result.paths_unresolved,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.paths_checked,
                    elapsed,
                    dict(
                        zip(
                            (
                                "nodes",
                                "definitions",
                                "paths",
                                "bindings",
                                "unresolved",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                )
            )
            assert result.input_paths_valid is (name != "faults_3000")
            assert counts == expected_counts[name]
            if name == "faults_3000":
                assert len(result.diagnostics.errors) == 1000
                assert result.diagnostics.errors_omitted == 2000
            del result
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        report.environment["rss_before_bytes"] = current_rss_bytes()
        for batch in range(6):
            for graph in workloads.values():
                validate_input_paths(graph)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 18
    record_testsuite_property("input_path_resources", report.to_json())


@pytest.mark.parametrize("text", ["inputs.data", "inputs.missing", "inputs.data."])
def test_input_paths_release_owned_objects(text: str) -> None:
    graph = binding_document(text, path=("config", "binds", "vcs_ref"), nested=True)
    assert graph.graph.subgraphs is not MISSING
    body = graph.graph.subgraphs["unused"]
    subject = body.nodes["subject"]
    assert isinstance(subject, HumanInputNode)
    assert subject.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(graph),
        weakref.ref(graph.graph),
        weakref.ref(body),
        weakref.ref(subject),
        weakref.ref(subject.config),
        weakref.ref(subject.source),
    ]
    if subject.config.binds is not MISSING:
        references.append(weakref.ref(subject.config.binds))
    assert subject.inputs is not MISSING
    references.append(weakref.ref(subject.inputs["data"]))
    del body, subject
    gc.collect()
    baseline_trees = sum(isinstance(value, Path) for value in gc.get_objects())

    result = validate_input_paths(graph)
    diagnostic_refs = [weakref.ref(item) for item in result.diagnostics.errors]
    source_refs = [
        weakref.ref(item.location)
        for item in result.diagnostics.errors
        if item.location is not None
    ]
    del graph
    gc.collect()
    inputs_released = all(reference() is None for reference in references)
    remaining_trees = sum(isinstance(value, Path) for value in gc.get_objects())
    remaining_owners = sum(
        isinstance(value, PathParser | InputPathChecks | SyntaxField)
        for value in gc.get_objects()
    )
    result_ref = weakref.ref(result)
    del result
    gc.collect()

    assert inputs_released
    assert remaining_trees == baseline_trees
    assert remaining_owners == 0
    assert bool(diagnostic_refs) is (text == "inputs.missing")
    assert all(reference() is None for reference in diagnostic_refs + source_refs)
    assert result_ref() is None
