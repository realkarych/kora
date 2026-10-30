import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import validate_edge_types
from kora.ir.contracts import CustomTypeContract
from kora.ir.edge_types import EdgeTypeChecks
from kora.ir.type_ancestry import TypeAncestry
from tests.ir.edge_type_cases import (
    ancestry_edges,
    edge_nodes,
    edges,
    shared_body,
    siblings,
)
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_edge_type_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "edges_1": edges(1),
        "edges_100": edges(100),
        "edges_1000": edges(1000),
        "ancestry_5000": ancestry_edges(5000, 1000),
        "siblings_1000": siblings(1000),
        "unknown_ancestry_1000": ancestry_edges(1000, 1000, invalid=True),
        "shared_body_1000": shared_body(1000),
        "faults_3000": edges(3000, invalid=True),
    }
    expected_counts = {
        "edges_1": (2, 0, 1, 2, 2, 1, 0, 0),
        "edges_100": (101, 0, 100, 2, 2, 100, 0, 0),
        "edges_1000": (1001, 0, 1000, 2, 2, 1000, 0, 0),
        "ancestry_5000": (1001, 0, 1000, 5000, 5000, 1000, 0, 0),
        "siblings_1000": (2000, 0, 1000, 1001, 1001, 1000, 0, 0),
        "unknown_ancestry_1000": (1001, 0, 1000, 1000, 0, 0, 1000, 0),
        "shared_body_1000": (1002, 1, 1, 1, 1, 1, 0, 0),
        "faults_3000": (3001, 0, 3000, 2, 2, 3000, 0, 0),
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
        intentional_cache="per-call saved ancestry intervals",
        peak_scope="cumulative since tracing start",
    )
    for document in workloads.values():
        validate_edge_types(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_edge_types(document)
            elapsed = time.perf_counter() - started
            counts = (
                result.nodes_checked,
                result.definitions_checked,
                result.inputs_checked,
                result.contracts_checked,
                result.types_indexed,
                result.edges_checked,
                result.edges_unresolved,
                result.edges_excluded,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.inputs_checked,
                    elapsed,
                    dict(
                        zip(
                            (
                                "nodes",
                                "definitions",
                                "inputs",
                                "contracts",
                                "types_indexed",
                                "edges",
                                "unresolved",
                                "excluded",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                )
            )
            assert result.edge_types_valid is (name != "faults_3000")
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
            for document in workloads.values():
                validate_edge_types(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 24
    record_testsuite_property("edge_type_resources", report.to_json())


@pytest.mark.parametrize("invalid", [False, True])
def test_edge_type_validation_releases_inputs_index_and_results(invalid: bool) -> None:
    nodes = edge_nodes("example/A", "example/B" if invalid else "example/A")
    nodes["consumer"]["source"] = {"file": "consumer.py", "line": 1}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        bodies={"unused": {"nodes": nodes}},
    )
    assert document.graph.subgraphs is not MISSING
    body = document.graph.subgraphs["unused"]
    consumer = body.nodes["consumer"]
    producer = body.nodes["producer"]
    assert consumer.inputs is not MISSING
    assert producer.outputs is not MISSING
    assert consumer.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(body),
        weakref.ref(consumer),
        weakref.ref(producer),
        weakref.ref(consumer.inputs["value"]),
        weakref.ref(producer.outputs["value"]),
        weakref.ref(consumer.source),
    ]
    for contract in document.type_contracts.values():
        assert isinstance(contract, CustomTypeContract)
        references.extend((weakref.ref(contract), weakref.ref(contract.schema_)))
        del contract
    del nodes, body, consumer, producer

    result = validate_edge_types(document)
    diagnostic_references = [weakref.ref(item) for item in result.diagnostics.errors]
    source_references = [
        weakref.ref(item.location)
        for item in result.diagnostics.errors
        if item.location is not None
    ]
    del document
    gc.collect()
    inputs_released = all(reference() is None for reference in references)
    remaining_state = sum(
        isinstance(value, TypeAncestry | EdgeTypeChecks) for value in gc.get_objects()
    )
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert inputs_released
    assert remaining_state == 0
    assert bool(diagnostic_references) is invalid
    assert all(
        reference() is None for reference in diagnostic_references + source_references
    )
    assert result_reference() is None
