import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import FanOutNode, validate_fan_out_types
from kora.ir.contracts import CustomTypeContract
from kora.ir.fan_out_types import FanOutTypeChecks
from kora.ir.type_ancestry import TypeAncestry
from kora.ir.type_declarations import TypeShape
from tests.ir.export_type_cases import export_body
from tests.ir.fan_out_type_cases import (
    ancestry_fan_outs,
    fan_out_node,
    fan_outs,
    stored_fan_outs,
)
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_fan_out_type_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "nodes_1": fan_outs(1),
        "nodes_1000": fan_outs(1000),
        "outputs_1000": fan_outs(1, output_count=1000),
        "ancestry_5000": ancestry_fan_outs(5000, 1000),
        "unknown_ancestry_1000": ancestry_fan_outs(1000, 1000, invalid=True),
        "shared_body_1000": stored_fan_outs(1000, shared=True),
        "unused_bodies_1000": stored_fan_outs(1000, shared=False),
        "deep_lists_100": fan_outs(100, list_depth=2000),
        "faults_3000": fan_outs(1, invalid=True, output_count=3000),
    }
    expected_counts = {
        "nodes_1": (2, 1, 1, 1, 2, 2, 1, 0, 1, 0),
        "nodes_1000": (1001, 1, 1000, 1000, 2, 2, 1000, 0, 1000, 0),
        "outputs_1000": (2, 1, 1, 1000, 2, 2, 1, 0, 1000, 0),
        "ancestry_5000": (1001, 1, 1000, 1000, 5000, 5000, 1000, 0, 1000, 0),
        "unknown_ancestry_1000": (1001, 1, 1000, 1000, 1000, 0, 0, 1000, 0, 1000),
        "shared_body_1000": (1002, 2, 1, 1, 1, 1, 1, 0, 1, 0),
        "unused_bodies_1000": (1002, 1001, 1000, 1000, 1, 1, 1000, 0, 1000, 0),
        "deep_lists_100": (101, 1, 100, 100, 2, 2, 100, 0, 100, 0),
        "faults_3000": (2, 1, 1, 3000, 2, 2, 1, 0, 3000, 0),
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
        validate_fan_out_types(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_fan_out_types(document)
            elapsed = time.perf_counter() - started
            counts = (
                result.nodes_checked,
                result.definitions_checked,
                result.fan_outs_checked,
                result.collections_checked,
                result.contracts_checked,
                result.types_indexed,
                result.mapped_inputs_checked,
                result.mapped_inputs_unresolved,
                result.collections_compared,
                result.collections_unresolved,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.fan_outs_checked,
                    elapsed,
                    dict(
                        zip(
                            (
                                "nodes",
                                "definitions",
                                "fan_outs",
                                "collections",
                                "contracts",
                                "types_indexed",
                                "mapped_inputs_checked",
                                "mapped_inputs_unresolved",
                                "collections_compared",
                                "collections_unresolved",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                )
            )
            assert result.fan_out_types_valid is (name != "faults_3000")
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
                validate_fan_out_types(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 27
    record_testsuite_property("fan_out_type_resources", report.to_json())


@pytest.mark.parametrize(
    ("mapped", "exported", "collected", "missing", "invalid"),
    [
        ("List[example/A]", "example/A", "List[example/A]", "", False),
        ("example/A", "example/A", "List[example/B]", "", True),
        (
            "List[List[example/A]]",
            "List[example/A]",
            "List[List[example/A]]",
            "",
            False,
        ),
        ("List[example/A]", "List[example/A]", "List[example/A]", "", True),
        ("example/Missing", "example/Missing", "example/Missing", "", False),
        ("List[example/A]]", "List[example/A]]", "List[example/A]", "", False),
        ("List[example/A]", "example/A", "List[example/A]", "inputs", False),
        ("List[example/A]", "example/A", "List[example/A]", "body", False),
        ("List[example/A]", "example/A", "List[example/A]", "collect", False),
    ],
)
def test_fan_out_type_validation_releases_inputs_index_and_results(
    mapped: str, exported: str, collected: str, missing: str, invalid: bool
) -> None:
    fan_data = fan_out_node(mapped, collected, source={"file": "fan.py", "line": 1})
    if missing == "inputs":
        fan_data.pop("inputs")
    if missing == "body":
        fan_data["config"] = {"body": "absent", "over": "items", "max_parallel": 2}
    if missing == "collect":
        fan_data["outputs"] = {"c0": {"type": collected}}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={"fan": fan_data},
        bodies={"body": export_body("example/A", exported)},
    )
    fan = document.graph.nodes["fan"]
    assert isinstance(fan, FanOutNode)
    assert document.graph.subgraphs is not MISSING
    body = document.graph.subgraphs["body"]
    assert body.outputs is not MISSING
    assert fan.outputs is not MISSING
    assert fan.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(body),
        weakref.ref(fan),
        weakref.ref(fan.config),
        weakref.ref(fan.outputs["c0"]),
        weakref.ref(body.outputs["e0"]),
        weakref.ref(fan.source),
    ]
    if fan.inputs is not MISSING:
        references.append(weakref.ref(fan.inputs["items"]))
    for contract in document.type_contracts.values():
        assert isinstance(contract, CustomTypeContract)
        references.extend((weakref.ref(contract), weakref.ref(contract.schema_)))
        del contract
    del fan_data, body, fan

    result = validate_fan_out_types(document)
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
        isinstance(value, TypeAncestry | FanOutTypeChecks | TypeShape)
        for value in gc.get_objects()
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
