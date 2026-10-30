import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

from kora.ir import GraphDocument, validate_graph_links
from tests.ir.graph_link_cases import chain, definition_chain, dense_faults, model, node
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def branch_fan_in(count: int) -> GraphDocument:
    targets = [f"n{index}" for index in range(count)]
    nodes = {
        name: node(depends_on=["branch", *targets[:index]])
        for index, name in enumerate(targets)
    }
    nodes["branch"] = node(
        "branch", config={"cases": [{"when": "true", "to": targets}]}
    )
    return model(nodes)


def test_graph_link_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "chain_1": chain(1),
        "chain_100": chain(100),
        "chain_1000": chain(1000),
        "faults_1000": dense_faults(1000),
        "definitions_1200": definition_chain(1200),
        "shared_1000": model(
            {
                f"call{index}": node("subgraph", config={"body": "shared"})
                for index in range(1000)
            },
            {"shared": {"nodes": {"timer": node()}}},
        ),
        "branch_fan_in_300": branch_fan_in(300),
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
        intentional_cache="none",
    )
    for document in workloads.values():
        validate_graph_links(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_graph_links(document)
            elapsed = time.perf_counter() - started
            report.samples.append(Sample(name, repeat, result.nodes_checked, elapsed))
            assert result.links_valid is (name != "faults_1000")
            del result
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        rss_before = current_rss_bytes()
        for batch in range(6):
            for document in workloads.values():
                validate_graph_links(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_peak_bytes"] = peak - baseline
        report.environment["rss_before_bytes"] = rss_before
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 21
    record_testsuite_property("graph_link_resources", report.to_json())


def test_graph_link_validation_releases_owned_references() -> None:
    document = dense_faults(1)
    document_reference = weakref.ref(document)

    result = validate_graph_links(document)
    diagnostic_references = [weakref.ref(item) for item in result.diagnostics.errors]
    del document
    gc.collect()
    document_released = document_reference() is None
    del result
    gc.collect()

    assert document_released
    assert diagnostic_references
    assert all(reference() is None for reference in diagnostic_references)
