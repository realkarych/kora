import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel.parser import Parser
from kora.cel.paths import Path, PathParser
from kora.cel.tree import Node
from kora.ir import HumanInputNode, validate_expression_syntax
from kora.ir.expression_fields import SyntaxField
from kora.ir.expression_syntax import SyntaxChecks
from tests.ir.expression_syntax_cases import (
    field_document,
    paths,
    predicates,
    shared_body,
)
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_expression_syntax_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "predicates_1": predicates(1),
        "predicates_100": predicates(100),
        "predicates_1000": predicates(1000),
        "paths_1000": paths(1000),
        "shared_body_1000": shared_body(1000),
        "length_boundary_100": predicates(100, " " * 4092 + "true"),
        "faults_3000": predicates(3000, "true &&"),
    }
    expected_counts = {
        "predicates_1": (1, 0, 0, 1, 0),
        "predicates_100": (100, 0, 0, 100, 0),
        "predicates_1000": (1000, 0, 0, 1000, 0),
        "paths_1000": (1000, 0, 0, 0, 1000),
        "shared_body_1000": (1001, 1, 0, 1, 0),
        "length_boundary_100": (100, 0, 0, 100, 0),
        "faults_3000": (3000, 0, 0, 3000, 0),
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
        intentional_cache="none owned by syntax validation",
        peak_scope="cumulative since tracing start",
    )
    for graph in workloads.values():
        validate_expression_syntax(graph)

    for name, graph in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_expression_syntax(graph)
            elapsed = time.perf_counter() - started
            counts = (
                result.nodes_checked,
                result.definitions_checked,
                result.triggers_checked,
                result.expressions_checked,
                result.paths_checked,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.expressions_checked + result.paths_checked,
                    elapsed,
                    dict(
                        zip(
                            (
                                "nodes",
                                "definitions",
                                "triggers",
                                "expressions",
                                "paths",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                )
            )
            assert result.syntax_valid is (name != "faults_3000")
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
                validate_expression_syntax(graph)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 21
    record_testsuite_property("expression_syntax_resources", report.to_json())


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("is_path", [False, True])
def test_expression_syntax_releases_graph_parser_state_and_results(
    invalid: bool, is_path: bool
) -> None:
    suffix = ("config", "binds", "vcs_ref") if is_path else ("config", "approve_when")
    text = "inputs.value." if invalid else "inputs.value"
    graph = field_document("human_input", suffix, text, nested=True)
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
    del body, subject
    gc.collect()
    baseline_trees = sum(isinstance(value, Node | Path) for value in gc.get_objects())

    result = validate_expression_syntax(graph)
    diagnostic_refs = [weakref.ref(item) for item in result.diagnostics.errors]
    source_refs = [
        weakref.ref(item.location)
        for item in result.diagnostics.errors
        if item.location is not None
    ]
    del graph
    gc.collect()
    inputs_released = all(reference() is None for reference in references)
    remaining_trees = sum(isinstance(value, Node | Path) for value in gc.get_objects())
    remaining_owners = sum(
        isinstance(value, Parser | PathParser | SyntaxChecks | SyntaxField)
        for value in gc.get_objects()
    )
    result_ref = weakref.ref(result)
    del result
    gc.collect()

    assert inputs_released
    assert remaining_trees == baseline_trees
    assert remaining_owners == 0
    assert bool(diagnostic_refs) is invalid
    assert all(reference() is None for reference in diagnostic_refs + source_refs)
    assert result_ref() is None
