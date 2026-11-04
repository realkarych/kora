from kora.ir.json import JsonObject
from tests.engine.graph_inventory_cases import mixed_document
from tests.engine.graph_output_cases import child


def retry_document() -> JsonObject:
    raw = mixed_document()
    runtime = child(raw, "runtime")
    runtime["provider_retry"] = {"max_wait": "000ms"}
    runtime["infrastructure_retry"] = {
        "max_attempts": 1.0,
        "initial_interval": "0002m",
        "backoff_coefficient": 10**1000,
        "retry_on": ["timeout", "stalled"],
        "non_retryable": [],
        "x-informational": "private-infrastructure-extension",
    }
    child(raw, "graph", "nodes", "producer")["retry"] = {
        "strategy": "resume",
        "predicate": '"é" == "é"',
        "non_retryable": ["command_failed", "budget_exhausted"],
        "retry_on": ["artifact_invalid", "harness_error"],
        "max_interval": "020m",
        "backoff_coefficient": 2.0,
        "initial_interval": "003s",
        "x-informational": {"max_attempts": 9},
    }
    bodies = child(raw, "graph", "subgraphs")
    bodies["alpha"] = {
        "nodes": {"timer": {"kind": "timer", "config": {"duration": "0s"}}}
    }
    child(bodies, "unused")["retry"] = {"max_attempts": 1, "predicate": "false"}
    child(bodies, "unused", "nodes", "approve")["retry"] = {}
    return raw
