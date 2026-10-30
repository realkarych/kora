import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import validate_subgraph_interfaces
from kora.ir.contracts import CustomTypeContract
from kora.ir.subgraph_interfaces import SubgraphInterfaceChecks
from kora.ir.type_ancestry import TypeAncestry
from kora.ir.type_declarations import TypeShape
from tests.ir.inline_schema_cases import document_for
from tests.ir.subgraph_interface_cases import (
    deep_ancestry_interfaces,
    interface_body,
    interface_wrapper,
    interfaces,
    unused_interfaces,
)
from tests.ir.type_declaration_cases import custom
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_subgraph_interface_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "wrappers_1": interfaces(1),
        "wrappers_100": interfaces(100),
        "wrappers_1000": interfaces(1000),
        "lists_1000": interfaces(1000, depth=2),
        "ancestry_5000": deep_ancestry_interfaces(5000, 1000),
        "unknown_ancestry_1000": deep_ancestry_interfaces(1000, 1000, invalid=True),
        "unused_bodies_1000": unused_interfaces(1000),
        "deep_lists_100": interfaces(100, depth=2000),
        "faults_3000": interfaces(3000, invalid=True),
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
        validate_subgraph_interfaces(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_subgraph_interfaces(document)
            elapsed = time.perf_counter() - started
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.wrappers_checked,
                    elapsed,
                    {
                        "nodes": result.nodes_checked,
                        "definitions": result.definitions_checked,
                        "wrappers": result.wrappers_checked,
                        "compared": result.types_compared,
                        "unresolved": result.types_unresolved,
                    },
                )
            )
            assert result.interfaces_valid is (name != "faults_3000")
            assert result.wrappers_checked == (
                1
                if name == "wrappers_1"
                else 100
                if name in ("wrappers_100", "deep_lists_100")
                else 3000
                if name == "faults_3000"
                else 1000
            )
            assert (
                result.inputs_checked
                == result.outputs_checked
                == result.wrappers_checked
            )
            assert (
                result.types_compared + result.types_unresolved
                == 2 * result.wrappers_checked
            )
            assert result.types_unresolved == (
                2000 if name == "unknown_ancestry_1000" else 0
            )
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
                validate_subgraph_interfaces(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 27
    record_testsuite_property("subgraph_interface_resources", report.to_json())


@pytest.mark.parametrize(
    "case", ["valid", "type", "name", "unknown", "malformed", "body", "extra_output"]
)
def test_subgraph_interface_validation_releases_call_state(case: str) -> None:
    source = {"file": "wrapper.py", "line": 1}
    wrapper = interface_wrapper(
        {
            "type": "example/B",
            "unknown": "example/Unknown",
            "malformed": "List[example/A]]",
        }.get(case, "example/A"),
        source=source,
    )
    if case == "name":
        wrapper["inputs"] = {}
    if case == "body":
        wrapper["config"] = {"body": "absent"}
    if case == "extra_output":
        wrapper["outputs"] = {
            "value": {"type": "example/A"},
            "extra": {"type": "example/A"},
        }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={"call": wrapper},
        bodies={"body": interface_body()},
    )
    assert document.graph.subgraphs is not MISSING
    body = document.graph.subgraphs["body"]
    call = document.graph.nodes["call"]
    assert body.inputs is not MISSING
    assert body.outputs is not MISSING
    assert call.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(value)
        for value in (
            document,
            document.graph,
            body,
            call,
            call.config,
            call.source,
            body.nodes["producer"],
            *body.inputs.values(),
            *body.outputs.values(),
        )
    ]
    if call.inputs is not MISSING:
        references.extend(weakref.ref(value) for value in call.inputs.values())
    if call.outputs is not MISSING:
        references.extend(weakref.ref(value) for value in call.outputs.values())
    for contract in document.type_contracts.values():
        assert isinstance(contract, CustomTypeContract)
        references.extend((weakref.ref(contract), weakref.ref(contract.schema_)))
        del contract
    del source, wrapper, body, call

    result = validate_subgraph_interfaces(document)
    copied = [weakref.ref(error) for error in result.diagnostics.errors]
    locations = [
        weakref.ref(error.location)
        for error in result.diagnostics.errors
        if error.location is not None
    ]
    del document
    gc.collect()
    released = all(reference() is None for reference in references)
    remaining_state = sum(
        isinstance(value, TypeAncestry | SubgraphInterfaceChecks | TypeShape)
        for value in gc.get_objects()
    )
    invalid = not result.interfaces_valid
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert released
    assert remaining_state == 0
    assert invalid is (case in ("type", "name"))
    assert bool(copied) is invalid
    assert all(reference() is None for reference in copied + locations)
    assert result_reference() is None
