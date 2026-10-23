from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from kora.ir import (
    AgentConfig,
    BranchConfig,
    CommandConfig,
    FanOutConfig,
    HumanInputConfig,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    Retry,
    SubgraphNodeConfig,
    TimerConfig,
    ToolPolicy,
    WaitEventConfig,
    Workspace,
)
from tests.ir.support import EXAMPLES, load_yaml

EXAMPLE_FILES: list[Path] = sorted(EXAMPLES.rglob("*.ir.yaml"))
NODE_CONFIGS: dict[str, type[BaseModel]] = {
    "agent": AgentConfig,
    "human_input": HumanInputConfig,
    "wait_event": WaitEventConfig,
    "timer": TimerConfig,
    "command": CommandConfig,
    "branch": BranchConfig,
    "fan_out": FanOutConfig,
    "subgraph": SubgraphNodeConfig,
}


def tool_policies(value: object) -> list[object]:
    if isinstance(value, dict):
        items = cast("dict[object, object]", value)
        found = [item for key, item in items.items() if key == "tool_policy"]
        return found + [
            policy for item in items.values() for policy in tool_policies(item)
        ]
    if isinstance(value, list):
        return [
            policy
            for item in cast("list[object]", value)
            for policy in tool_policies(item)
        ]
    return []


def test_the_examples_carry_tool_policies() -> None:
    policies = [
        policy for path in EXAMPLE_FILES for policy in tool_policies(load_yaml(path))
    ]

    assert len(EXAMPLE_FILES) >= 2
    assert len(policies) >= 6


def test_the_examples_carry_control_configurations() -> None:
    documents = [cast("dict[str, object]", load_yaml(path)) for path in EXAMPLE_FILES]

    models = {
        model
        for document in documents
        for model, _ in config_records(cast("dict[str, object]", document["graph"]))
    }

    assert {BranchConfig, FanOutConfig, Retry} <= models


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_ir_models_accept_examples(path: Path) -> None:
    document = cast("dict[str, object]", load_yaml(path))
    policies = tool_policies(document)
    records = config_records(cast("dict[str, object]", document["graph"]))
    runtime = cast("dict[str, object]", document["runtime"])
    if "infrastructure_retry" in runtime:
        records.append((Retry, runtime["infrastructure_retry"]))

    accepted = [ToolPolicy.model_validate(policy) for policy in policies]
    configs = [model.model_validate(value) for model, value in records]

    dumped = [policy.model_dump(mode="json", exclude_unset=True) for policy in accepted]
    assert dumped == policies
    assert [
        config.model_dump(mode="json", by_alias=True, exclude_unset=True)
        for config in configs
    ] == [value for _, value in records]
    assert any(model is CommandConfig for model, _ in records)
    assert any(model is Workspace for model, _ in records)
    assert any(model is NodeLimits for model, _ in records)


def config_records(graph: dict[str, object]) -> list[tuple[type[BaseModel], object]]:
    records: list[tuple[type[BaseModel], object]] = []
    if "retry" in graph:
        records.append((Retry, graph["retry"]))
    if "limits" in graph:
        records.append((NodeLimits, graph["limits"]))
    for body in cast(
        "dict[str, dict[str, object]]", graph.get("subgraphs", {})
    ).values():
        records.extend(config_records(body))
    for node in cast("dict[str, dict[str, object]]", graph["nodes"]).values():
        if "limits" in node:
            records.append((NodeLimits, node["limits"]))
        if "retry" in node:
            records.append((Retry, node["retry"]))
        config = cast("dict[str, object]", node["config"])
        kind = cast("str", node["kind"])
        if kind in NODE_CONFIGS:
            records.append((NODE_CONFIGS[kind], config))
        if node["kind"] == "agent":
            for name, model in (
                ("workspace", Workspace),
                ("network_policy", NetworkPolicy),
            ):
                if name in config:
                    records.append((model, config[name]))
            records.extend(
                (McpServer, server)
                for server in cast(
                    "dict[str, object]", config.get("mcp_servers", {})
                ).values()
            )
    return records
