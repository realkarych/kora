import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import validate_export_types
from kora.ir.contracts import CustomTypeContract
from kora.ir.export_types import ExportTypeChecks
from kora.ir.type_ancestry import TypeAncestry
from kora.ir.type_declarations import TypeShape
from tests.ir.export_type_cases import (
    ancestry_exports,
    export_body,
    exports,
    shared_exports,
    unused_exports,
)
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_export_type_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "exports_1": exports(1),
        "exports_100": exports(100),
        "exports_1000": exports(1000),
        "lists_1000": exports(1000, list_depth=2),
        "ancestry_5000": ancestry_exports(5000, 1000),
        "unknown_ancestry_1000": ancestry_exports(1000, 1000, invalid=True),
        "shared_body_1000": shared_exports(1000),
        "unused_bodies_1000": unused_exports(1000),
        "deep_lists_100": exports(100, list_depth=2000),
        "list_faults_3000": exports(3000, invalid=True, list_depth=2),
    }
    expected_counts = {
        "exports_1": (1, 1, 2, 2, 1, 0),
        "exports_100": (1, 100, 2, 2, 100, 0),
        "exports_1000": (1, 1000, 2, 2, 1000, 0),
        "lists_1000": (1, 1000, 2, 2, 1000, 0),
        "ancestry_5000": (1, 1000, 5000, 5000, 1000, 0),
        "unknown_ancestry_1000": (1, 1000, 1000, 0, 0, 1000),
        "shared_body_1000": (1, 1, 1, 1, 1, 0),
        "unused_bodies_1000": (1000, 1000, 1, 1, 1000, 0),
        "deep_lists_100": (1, 100, 2, 2, 100, 0),
        "list_faults_3000": (1, 3000, 2, 2, 3000, 0),
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
        validate_export_types(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_export_types(document)
            elapsed = time.perf_counter() - started
            counts = (
                result.definitions_checked,
                result.exports_checked,
                result.contracts_checked,
                result.types_indexed,
                result.exports_compared,
                result.exports_unresolved,
            )
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.exports_checked,
                    elapsed,
                    dict(
                        zip(
                            (
                                "definitions",
                                "exports",
                                "contracts",
                                "types_indexed",
                                "compared",
                                "unresolved",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                )
            )
            assert result.export_types_valid is (name != "list_faults_3000")
            assert counts == expected_counts[name]
            if name == "list_faults_3000":
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
                validate_export_types(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 30
    record_testsuite_property("export_type_resources", report.to_json())


@pytest.mark.parametrize(
    ("produced", "expected", "missing", "invalid"),
    [
        ("example/A", "example/A", "", False),
        ("example/A", "example/B", "", True),
        ("List[example/A]", "List[example/A]", "", False),
        ("List[example/A]", "List[example/B]", "", True),
        ("List[example/Missing]", "example/A", "", False),
        ("List[example/A]]", "List[example/A]", "", False),
        ("example/A", "example/B", "from", False),
        ("example/A", "example/B", "output", False),
    ],
)
def test_export_type_validation_releases_inputs_index_and_results(
    produced: str,
    expected: str,
    missing: str,
    invalid: bool,
) -> None:
    body_data = export_body(produced, expected)
    body_data["source"] = {"file": "body.py", "line": 1}
    body_data["outputs"] = {
        "value": {
            "type": expected,
            "from": "producer",
            "output": "value",
            **({missing: "absent"} if missing else {}),
        }
    }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        bodies={"unused": body_data},
    )
    assert document.graph.subgraphs is not MISSING
    body = document.graph.subgraphs["unused"]
    producer = body.nodes["producer"]
    assert body.outputs is not MISSING
    assert producer.outputs is not MISSING
    assert body.source is not MISSING
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(body),
        weakref.ref(producer),
        weakref.ref(body.outputs["value"]),
        weakref.ref(producer.outputs["value"]),
        weakref.ref(body.source),
    ]
    for contract in document.type_contracts.values():
        assert isinstance(contract, CustomTypeContract)
        references.extend((weakref.ref(contract), weakref.ref(contract.schema_)))
        del contract
    del body_data, body, producer

    result = validate_export_types(document)
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
        isinstance(value, TypeAncestry | ExportTypeChecks | TypeShape)
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
