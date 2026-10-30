import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import validate_output_schemas
from kora.ir.json import JsonObject
from kora.ir.output_schemas import OutputSchemaChecks, StructuredAncestry
from kora.ir.topology import AgentNode, HumanInputNode
from tests.ir.inline_schema_cases import document_for
from tests.ir.output_schema_cases import (
    agent,
    contracts,
    deep_value,
    human,
    many_humans,
    shared_ancestry,
    shared_body,
)
from tests.ir.type_declaration_cases import custom
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_output_schema_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    wide: JsonObject = {f"k{index}": index for index in range(10_000)}
    workloads = {
        "nodes_1": many_humans(1),
        "nodes_100": many_humans(100),
        "nodes_1000": many_humans(1000),
        "depth_5000": document_for(
            nodes={
                "gate": human({"const": deep_value(5000)}, {"const": deep_value(5000)})
            }
        ),
        "width_10000": document_for(
            nodes={"gate": human({"const": wide}, {"const": wide})}
        ),
        "ancestry_1000": shared_ancestry(1000, 1000),
        "shared_body_1000": shared_body(1000),
        "faults_3000": many_humans(3000, invalid=True),
    }
    expected_counts = {
        "nodes_1": (1, 0, 1, 0, 1, 1),
        "nodes_100": (100, 0, 100, 0, 100, 100),
        "nodes_1000": (1000, 0, 1000, 0, 1000, 1000),
        "depth_5000": (1, 0, 1, 0, 1, 5002),
        "width_10000": (1, 0, 1, 0, 1, 10002),
        "ancestry_1000": (1000, 0, 2000, 1002, 1000, 1000),
        "shared_body_1000": (1001, 1, 1, 0, 1, 1),
        "faults_3000": (3000, 0, 3000, 0, 3000, 3000),
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
        intentional_cache="per-call ancestry classifications, including unknowns",
        peak_scope="cumulative since tracing start",
    )
    for document in workloads.values():
        validate_output_schemas(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_output_schemas(document)
            elapsed = time.perf_counter() - started
            counts = (
                result.nodes_checked,
                result.definitions_checked,
                result.outputs_checked,
                result.ancestry_steps,
                result.comparisons_checked,
                result.values_checked,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.values_checked,
                    elapsed,
                    dict(
                        zip(
                            (
                                "nodes",
                                "definitions",
                                "outputs",
                                "ancestry",
                                "comparisons",
                                "values",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                )
            )
            assert result.output_schemas_valid is (name != "faults_3000")
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
                validate_output_schemas(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 24
    record_testsuite_property("output_schema_resources", report.to_json())


@pytest.mark.parametrize("invalid", [False, True])
def test_output_schema_validation_releases_inputs_state_and_results(
    invalid: bool,
) -> None:
    left: JsonObject = {"const": deep_value(5000)}
    right: JsonObject = {"const": deep_value(5000, int(invalid))}
    document = document_for(
        nodes={
            "agent": agent(
                {"data": {"type": "example/Child", "schema": right}},
                left,
                source={"file": "agent.py", "line": 1},
            ),
            "gate": human(left, right, source={"file": "gate.py", "line": 1}),
        },
        contracts={**contracts(), "example/Child": custom("StructuredOutput")},
    )
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
    ]
    references.extend(
        weakref.ref(contract) for contract in document.type_contracts.values()
    )
    for candidate in document.graph.nodes.values():
        assert isinstance(candidate, AgentNode | HumanInputNode)
        assert candidate.source is not MISSING
        references.extend(
            (
                weakref.ref(candidate),
                weakref.ref(candidate.config),
                weakref.ref(candidate.source),
            )
        )
        for declaration in candidate.outputs.values():
            references.append(weakref.ref(declaration))
            if declaration.schema_ is not MISSING:
                references.append(weakref.ref(declaration.schema_))
            del declaration
        schema = (
            candidate.config.output_schema
            if isinstance(candidate, AgentNode)
            else candidate.config.form
        )
        assert schema is not MISSING
        references.append(weakref.ref(schema))
        del candidate, schema
    del left, right

    result = validate_output_schemas(document)
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
        isinstance(value, OutputSchemaChecks | StructuredAncestry)
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
