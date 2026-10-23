from copy import deepcopy
from typing import cast

from pydantic import BaseModel

from kora.ir import (
    AgentConfig,
    AgentNode,
    BranchConfig,
    BranchNode,
    CommandConfig,
    CommandNode,
    FanOutConfig,
    FanOutNode,
    Graph,
    GraphBody,
    GraphDocument,
    HumanInputBinds,
    HumanInputConfig,
    HumanInputNode,
    InputRef,
    IrSourceLocation,
    NodeLimits,
    OutputDecl,
    Provenance,
    Retry,
    RuntimeSection,
    SecretsSection,
    Subgraph,
    SubgraphNode,
    SubgraphNodeConfig,
    TimerConfig,
    TimerNode,
    WaitEventConfig,
    WaitEventNode,
)
from tests.ir.config_cases import POPULATED as CONFIG_POPULATED
from tests.ir.config_cases import RESOURCE
from tests.ir.control_cases import POPULATED as CONTROL_POPULATED
from tests.ir.declaration_cases import POPULATED as DECLARATION_POPULATED
from tests.ir.remaining_config_cases import POPULATED as REMAINING_POPULATED


def document() -> dict[str, object]:
    return {
        "ir_version": "1.0",
        "type_contracts": {},
        "graph": {
            "id": "graph",
            "namespace": "example",
            "nodes": {"timer": {"kind": "timer", "config": {"duration": "0s"}}},
        },
        "runtime": {"runtime": "local", "vcs": "git"},
    }


NODE_CASES: dict[str, dict[str, object]] = {
    "agent": {
        "config": {"harness": "claude", "prompt": "", "workspace": {"repo": ""}},
        "outputs": {"session": {"type": "AgentSession"}},
    },
    "command": {
        "config": {"cmd": ["true"], "workspace": {"repo": ""}},
        "outputs": {"result": {"type": "CommandResult"}},
    },
    "human_input": {
        "config": {"channel": "cli", "timeout": "0s", "on_expiry": "fail"},
        "outputs": {"response": {"type": "StructuredOutput"}},
    },
    "wait_event": {
        "config": {
            "event": {"type": "webhook", "name": ""},
            "timeout": "0s",
            "on_expiry": "skip",
        },
        "outputs": {"event": {"type": "StructuredOutput"}},
    },
    "timer": {"config": {"duration": "0s"}},
    "branch": {"config": {"cases": [{"when": "true", "to": ["next"]}]}},
    "fan_out": {"config": {"body": "body", "over": "items"}},
    "subgraph": {"config": {"body": "body"}},
}


NODE_MODELS: tuple[type[BaseModel], ...] = (
    AgentNode,
    CommandNode,
    HumanInputNode,
    WaitEventNode,
    TimerNode,
    BranchNode,
    FanOutNode,
    SubgraphNode,
)
CONFIG_MODELS: tuple[type[BaseModel], ...] = (
    AgentConfig,
    CommandConfig,
    HumanInputConfig,
    WaitEventConfig,
    TimerConfig,
    BranchConfig,
    FanOutConfig,
    SubgraphNodeConfig,
)
MINIMAL: dict[type[BaseModel], dict[str, object]] = {
    model: deepcopy({"kind": kind, **NODE_CASES[kind]})
    for model, kind in zip(NODE_MODELS, NODE_CASES, strict=True)
}
MINIMAL.update(
    {
        GraphBody: {"nodes": {"timer": deepcopy(MINIMAL[TimerNode])}},
        Subgraph: {"nodes": {"timer": deepcopy(MINIMAL[TimerNode])}},
        Graph: deepcopy(cast("dict[str, object]", document()["graph"])),
        Provenance: {},
        GraphDocument: document(),
    }
)


def populated_workspace() -> dict[str, object]:
    return {"repo": "", "rev": "", "from_ref": "inputs.change"}


def populated_network() -> dict[str, object]:
    return {"egress": ["api.example", "tools.example"]}


def populated_mcp_server() -> dict[str, object]:
    return {
        "type": "http",
        "command": "",
        "args": ["", "--raw"],
        "url": "not a URI",
        "env": {"A": "", "B": ""},
        "secret_env": {"A": "TOKEN", "B": "OTHER"},
        "network_policy": populated_network(),
        "headers": {"First": "", "Second": ""},
        "digest": "sha256:" + "a" * 64,
        "x-meta": False,
    }


ALL_CONFIGS: dict[type[BaseModel], dict[str, object]] = {
    model: deepcopy(value)
    for model, value in {
        **CONFIG_POPULATED,
        **CONTROL_POPULATED,
        **REMAINING_POPULATED,
    }.items()
}
ALL_CONFIGS[Retry].update(
    {
        "retry_on": ["timeout", "artifact_invalid"],
        "non_retryable": ["cancelled", "config_error"],
    }
)
ALL_CONFIGS[CommandConfig] = {
    "cmd": ["tool", "{{ inputs.value }}"],
    "output_file": "",
    "resources": [dict(RESOURCE), dict(RESOURCE)],
    "resource_set_digest": "sha256:" + "a" * 64,
    "workspace": populated_workspace(),
    "env": {"A": "", "B": ""},
    "secret_env": {"A": "TOKEN", "B": "OTHER"},
    "network_policy": populated_network(),
    "success_exit_codes": [0, 1],
    "runtime": "",
    "vcs": "",
    "x-meta": False,
}
ALL_CONFIGS[AgentConfig]["mcp_servers"] = {
    "first": populated_mcp_server(),
    "second": populated_mcp_server(),
}
ALL_CONFIGS[HumanInputConfig]["binds"] = deepcopy(REMAINING_POPULATED[HumanInputBinds])
ALL_CONFIGS[BranchConfig] = {
    "cases": [
        {"when": "true", "to": ["first", "second"], "reason": "'first'"},
        {"when": "false", "to": ["second", "$fail_attempt"], "reason": "'second'"},
    ],
    "else": ["first", "second"],
    "else_reason": "'no match'",
    "x-meta": False,
}
POPULATED: dict[type[BaseModel], dict[str, object]] = {}
for model, config_model in zip(NODE_MODELS, CONFIG_MODELS, strict=True):
    common: dict[str, object] = {
        **MINIMAL[model],
        "description": "",
        "source": deepcopy(DECLARATION_POPULATED[IrSourceLocation]),
        "inputs": {
            "first": deepcopy(DECLARATION_POPULATED[InputRef]),
            "second": deepcopy(DECLARATION_POPULATED[InputRef]),
        },
        "depends_on": ["first", "second"],
        "trigger_rule": "none_failed_min_one_success",
        "retry": deepcopy(ALL_CONFIGS[Retry]),
        "limits": deepcopy(ALL_CONFIGS[NodeLimits]),
        "attempt_invariant": False,
        "independent_of": ["first", "second"],
        "protected_paths": ["", "gates/**"],
        "preset": "",
        "config": deepcopy(ALL_CONFIGS[config_model]),
        "x-meta": False,
    }
    if model is AgentNode:
        common["outputs"] = {
            "session": {
                **deepcopy(DECLARATION_POPULATED[OutputDecl]),
                "type": "AgentSession",
            },
            "result": deepcopy(DECLARATION_POPULATED[OutputDecl]),
        }
    elif model is CommandNode:
        common["outputs"] = {
            "result": {
                **deepcopy(DECLARATION_POPULATED[OutputDecl]),
                "type": "CommandResult",
            },
            "data": deepcopy(DECLARATION_POPULATED[OutputDecl]),
        }
    elif model in (HumanInputNode, WaitEventNode):
        role = "response" if model is HumanInputNode else "event"
        common["outputs"] = {role: deepcopy(DECLARATION_POPULATED[OutputDecl])}
    else:
        common["outputs"] = {
            "first": deepcopy(DECLARATION_POPULATED[OutputDecl]),
            "second": deepcopy(DECLARATION_POPULATED[OutputDecl]),
        }
    POPULATED[model] = common
POPULATED[GraphBody] = {
    "description": "",
    "nodes": {
        "first": deepcopy(POPULATED[AgentNode]),
        "second": deepcopy(POPULATED[CommandNode]),
    },
    "x-meta": False,
}
POPULATED[Subgraph] = {
    **deepcopy(POPULATED[GraphBody]),
    "inputs": {
        "first": deepcopy(DECLARATION_POPULATED[OutputDecl]),
        "second": deepcopy(DECLARATION_POPULATED[OutputDecl]),
    },
    "outputs": {
        "first": {"type": "StructuredOutput", "from": "first", "output": "result"},
        "second": {"type": "StructuredOutput", "from": "second", "output": "data"},
    },
    "retry": deepcopy(ALL_CONFIGS[Retry]),
    "limits": deepcopy(ALL_CONFIGS[NodeLimits]),
    "source": deepcopy(DECLARATION_POPULATED[IrSourceLocation]),
}
POPULATED[Graph] = {
    **deepcopy(POPULATED[GraphBody]),
    "id": "graph",
    "version": "",
    "namespace": "example",
    "params": {"properties": {"first": True, "second": False}},
    "triggers": [
        {"kind": "manual", "params": {"first": False, "second": None}},
        {
            "kind": "cron",
            "expr": "",
            "timezone": "",
            "params": {"first": True, "second": ""},
        },
    ],
    "overlap": "queue",
    "target": {
        "key": {"from": "first", "output": "result", "path": "target"},
        "on_active": "defer",
    },
    "subgraphs": {
        "first": deepcopy(POPULATED[Subgraph]),
        "second": deepcopy(POPULATED[Subgraph]),
    },
    "source": deepcopy(DECLARATION_POPULATED[IrSourceLocation]),
}
POPULATED[Provenance] = {
    "compiler": "",
    "source_digest": "sha256:" + "a" * 64,
    "presets": {"first": {"model": ""}, "second": {"prompt": ""}},
    "defaults": {"global": {}, "graph": {}},
    "x-meta": False,
    "data": [None, False],
}
POPULATED[GraphDocument] = {
    "$schema": "https://kora.dev/schemas/ir/1.0.json",
    "ir_version": "1.0",
    "meta": deepcopy(POPULATED[Provenance]),
    "type_contracts": {
        "AgentSession": {"builtin_revision": "1"},
        "CommandResult": {"builtin_revision": "1"},
    },
    "graph": deepcopy(POPULATED[Graph]),
    "runtime": deepcopy(DECLARATION_POPULATED[RuntimeSection]),
    "secrets": deepcopy(DECLARATION_POPULATED[SecretsSection]),
    "ignored": False,
    "x-meta": False,
}
INVALID: dict[type[BaseModel], dict[str, object]] = {
    model: {**deepcopy(MINIMAL[model]), "config": False} for model in NODE_MODELS
}
INVALID.update(
    {
        GraphBody: {"nodes": False},
        Subgraph: {**deepcopy(MINIMAL[Subgraph]), "inputs": False},
        Graph: {**deepcopy(MINIMAL[Graph]), "id": False},
        Provenance: {"compiler": False},
        GraphDocument: {**document(), "ir_version": False},
    }
)
