import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest

from kora.ir import validate_type_declarations
from kora.ir.declarations import IrSourceLocation
from tests.ir.graph_link_cases import node
from tests.ir.type_declaration_cases import custom, declarations, model, parent_chain
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_type_declaration_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "declarations_1": declarations(1),
        "declarations_100": declarations(100),
        "declarations_1000": declarations(1000),
        "parents_1200": parent_chain(1200),
        "cycle_1200": parent_chain(1200, cyclic=True),
        "shared_parent_1000": model(
            {"example/Base": custom()}
            | {f"example/T{i}": custom("example/Base") for i in range(1000)}
        ),
        "list_depth_2000": declarations(1, depth=2000),
        "faults_3000": declarations(3000, invalid=True),
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
        list_depth=2000,
    )
    for document in workloads.values():
        validate_type_declarations(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_type_declarations(document)
            elapsed = time.perf_counter() - started
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.declarations_checked + result.contracts_checked,
                    elapsed,
                    {
                        "declarations": result.declarations_checked,
                        "contracts": result.contracts_checked,
                    },
                )
            )
            assert result.declarations_valid is (
                name not in {"cycle_1200", "faults_3000"}
            )
            del result
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        rss_before = current_rss_bytes()
        for batch in range(6):
            for document in workloads.values():
                validate_type_declarations(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_peak_bytes"] = peak - baseline
        report.environment["rss_before_bytes"] = rss_before
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 24
    record_testsuite_property("type_declaration_resources", report.to_json())


@pytest.mark.parametrize("invalid", [False, True])
def test_type_declaration_validation_releases_owned_references(invalid: bool) -> None:
    document = model(
        {"example/Value": custom("example/Missing" if invalid else None)},
        {"call": node("subgraph", config={"body": "body"})},
        {
            "body": {
                "source": {"file": "graph.py", "line": 1},
                "inputs": {"value": {"type": "example/Value"}},
                "nodes": {
                    "timer": node(
                        inputs={
                            "value": {
                                "type": "example/Missing"
                                if invalid
                                else "example/Value"
                            }
                        },
                        source={"file": "graph.py", "line": 2},
                    )
                },
            }
        },
    )
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
    ]
    references.extend(weakref.ref(value) for value in document.type_contracts.values())
    references.extend(weakref.ref(value) for value in document.graph.nodes.values())
    assert isinstance(document.graph.subgraphs, dict)
    references.extend(weakref.ref(body) for body in document.graph.subgraphs.values())
    references.extend(
        weakref.ref(value)
        for body in document.graph.subgraphs.values()
        for value in body.nodes.values()
    )
    references.extend(
        weakref.ref(body.source)
        for body in document.graph.subgraphs.values()
        if isinstance(body.source, IrSourceLocation)
    )
    references.extend(
        weakref.ref(value.source)
        for body in document.graph.subgraphs.values()
        for value in body.nodes.values()
        if isinstance(value.source, IrSourceLocation)
    )

    result = validate_type_declarations(document)
    diagnostic_references = [weakref.ref(item) for item in result.diagnostics.errors]
    del document
    gc.collect()
    inputs_released = all(reference() is None for reference in references)
    del result
    gc.collect()

    assert inputs_released
    assert bool(diagnostic_references) is invalid
    assert all(reference() is None for reference in diagnostic_references)
