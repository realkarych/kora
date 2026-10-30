import gc
import os
import time
import tracemalloc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import AgentNode, ToolPolicy, validate_tool_policies
from kora.ir.tool_patterns import ToolPattern, tool_policy_refusals
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.tool_policy_cases import (
    byte_boundary_patterns,
    folding_boundary_patterns,
    piece_boundary_patterns,
    policy_agent,
    policy_document,
)
from tests.measurements.harness import Sample, current_rss_bytes, new_report


def test_tool_policy_resource_observations(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    workloads = {
        "nodes_1": policy_document({"allow": ["Read", "Bash(git *)"]}),
        "nodes_100": policy_document({"allow": ["Read", "Bash(git *)"]}, 100),
        "nodes_1000": policy_document({"allow": ["Read", "Bash(git *)"]}, 1000),
        "shared_1000": policy_document({"deny": ["Bash(rm *)"]}, 1000),
        "full_arrays": policy_document(
            {"allow": ["Read"] * 128, "deny": ["Bash"] * 128}
        ),
        "array_bytes": policy_document({"allow": byte_boundary_patterns()}),
        "combining_deny": policy_document({"deny": ["Write(x\u00e1*)"]}),
        "faults_3000": policy_document({"deny": ["("]}, 3000),
        "array_refusal": policy_document({"allow": ["Read"] * 129}),
        "layer_refusal": policy_document(
            {"deny": piece_boundary_patterns() + ["Read"]}
        ),
    }
    shared = workloads["shared_1000"].graph.nodes["n0"]
    for name in workloads["shared_1000"].graph.nodes:
        workloads["shared_1000"].graph.nodes[name] = shared
    del shared
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
        intentional_cache="none in static checker; provider cache unchanged",
        peak_scope="cumulative since tracing start",
    )
    for document in workloads.values():
        validate_tool_policies(document)

    for name, document in workloads.items():
        for repeat in range(3):
            started = time.perf_counter()
            result = validate_tool_policies(document)
            elapsed = time.perf_counter() - started
            report.samples.append(
                Sample(
                    name,
                    repeat,
                    result.nodes_checked,
                    elapsed,
                    {
                        "nodes": result.nodes_checked,
                        "policies": result.policies_checked,
                        "patterns": result.patterns_checked,
                    },
                )
            )
            assert result.tool_policies_valid is (
                name
                not in (
                    "combining_deny",
                    "faults_3000",
                    "array_refusal",
                    "layer_refusal",
                )
            )
            assert result.policies_checked == result.nodes_checked
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
                validate_tool_policies(document)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.environment[f"batch_{batch}_settled_bytes"] = current - baseline
            report.environment[f"batch_{batch}_cumulative_peak_bytes"] = peak - baseline
        report.environment["rss_after_bytes"] = current_rss_bytes()
    finally:
        tracemalloc.stop()

    assert len(report.samples) == 30
    record_testsuite_property("tool_policy_resources", report.to_json())


@pytest.mark.parametrize(
    "case", ["valid", "malformed", "reserved", "array", "pieces", "folding"]
)
def test_tool_policy_validation_releases_call_state(case: str) -> None:
    values = {
        "valid": ["Bash(git *)"],
        "malformed": ["Bash("],
        "reserved": ["StructuredOutput"],
        "array": ["Read"] * 129,
        "pieces": piece_boundary_patterns() + ["Read"],
        "folding": folding_boundary_patterns(1),
    }
    document = document_for(
        nodes={"root": node()},
        bodies={
            "unused": {
                "nodes": {
                    "agent": policy_agent(
                        {"deny": values[case]}, source={"file": "agent.py", "line": 1}
                    )
                },
                "source": {"file": "body.py", "line": 2},
            }
        },
    )
    assert document.graph.subgraphs is not MISSING
    body = document.graph.subgraphs["unused"]
    agent = body.nodes["agent"]
    assert isinstance(agent, AgentNode)
    assert agent.config.tool_policy is not MISSING
    policy = agent.config.tool_policy
    references: list[Callable[[], object | None]] = [
        weakref.ref(value)
        for value in (document, document.graph, body, agent, agent.config, policy)
    ]
    assert body.source is not MISSING
    assert agent.source is not MISSING
    references.extend((weakref.ref(body.source), weakref.ref(agent.source)))
    del agent, body, policy, values

    result = validate_tool_policies(document)
    copied = [weakref.ref(error) for error in result.diagnostics.errors]
    copied_sources = [
        weakref.ref(error.location)
        for error in result.diagnostics.errors
        if error.location is not None
    ]
    del document
    gc.collect()
    released = all(reference() is None for reference in references)
    remaining = sum(isinstance(value, ToolPattern) for value in gc.get_objects())
    invalid = not result.tool_policies_valid
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert released
    assert remaining == 0
    assert invalid is (case != "valid")
    assert bool(copied) is invalid
    assert all(reference() is None for reference in copied + copied_sources)
    assert result_reference() is None


def test_namespace_reusable_core_checker_releases_the_supplied_layer() -> None:
    policy = ToolPolicy(allow=("Read",), deny=("StructuredOutput",))
    reference = weakref.ref(policy)

    found = tuple(tool_policy_refusals(policy))
    del policy
    gc.collect()

    assert reference() is None
    assert len(found) == 1
    assert found[0][0] == ("deny", 0)
