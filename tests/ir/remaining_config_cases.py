from kora.ir import (
    AgentConfig,
    EventSelector,
    HumanInputBinds,
    HumanInputConfig,
    SensorPoll,
    SubagentConfig,
    TimerConfig,
    WaitEventConfig,
)
from kora.ir.record import IrRecord

MINIMAL: dict[type[IrRecord], dict[str, object]] = {
    SubagentConfig: {"name": "", "description": "", "prompt": ""},
    AgentConfig: {"harness": "claude", "prompt": "", "workspace": {"repo": ""}},
    HumanInputBinds: {},
    HumanInputConfig: {"channel": "cli", "timeout": "0s", "on_expiry": "fail"},
    EventSelector: {"type": "webhook", "name": ""},
    SensorPoll: {"sensor": "http", "until": "true"},
    WaitEventConfig: {
        "event": {"type": "graph", "name": ""},
        "timeout": "0s",
        "on_expiry": "skip",
    },
    TimerConfig: {"duration": "0s"},
}
POPULATED: dict[type[IrRecord], dict[str, object]] = {
    SubagentConfig: {
        "name": "helper",
        "description": "description",
        "prompt": "{{raw}}",
        "tools": ["", "Read"],
        "x-meta": False,
    },
    AgentConfig: {
        **MINIMAL[AgentConfig],
        "model": "",
        "system_prompt_append": "{{raw}}",
        "skills": ["second", "first"],
        "subagents": [
            {**MINIMAL[SubagentConfig], "tools": ["Read", "Write"]},
            {
                "name": "other",
                "description": "",
                "prompt": "",
                "tools": ["Read", "Edit"],
            },
        ],
        "hooks": {"custom": [False, None], "other": [True, 0]},
        "mcp_servers": {
            "": {"type": "http"},
            "x-name": {"type": "stdio", "args": ["", "--raw"]},
        },
        "tool_policy": {
            "allow": ["Read", "Edit"],
            "deny": ["Bash(rm *)", "Bash(sudo *)"],
            "x-meta": False,
        },
        "permission_mode": "dont_ask",
        "sandbox": "workspace_write",
        "trust_repo_config": False,
        "network_policy": {"egress": ["api.example", "tools.example"]},
        "workspace": {"repo": "", "rev": "{{raw}}", "from_ref": "inputs.change"},
        "output_schema": {
            "type": "object",
            "properties": {"first": True, "second": False},
        },
        "env": {"A": "", "B": "{{raw}}"},
        "secret_env": {"A": "TOKEN", "B": "OTHER"},
        "runtime": "",
        "vcs": "",
        "x-meta": False,
    },
    HumanInputBinds: {
        "vcs_ref": "inputs.change",
        "gate_digests": ["inputs.a", "inputs.b"],
        "x-meta": False,
    },
    HumanInputConfig: {
        **MINIMAL[HumanInputConfig],
        "form": {"type": "object", "properties": {"decision": True, "other": False}},
        "default_value": None,
        "binds": {"vcs_ref": "inputs.change", "gate_digests": ["inputs.a", "inputs.b"]},
        "approve_when": "true",
        "context": "inputs.context",
        "x-meta": False,
    },
    EventSelector: {"type": "graph", "name": "event", "match": "true", "x-meta": False},
    SensorPoll: {
        "sensor": "http",
        "config": {"values": [None, False], "other": [True, ""]},
        "until": "true",
        "x-meta": False,
    },
    WaitEventConfig: {
        "poll": {
            "sensor": "http",
            "until": "true",
            "config": {"values": [None, False], "other": [True, ""]},
        },
        "interval": "0001s",
        "timeout": "0s",
        "on_expiry": "fail",
        "x-meta": False,
    },
    TimerConfig: {"at": "2024-02-29t23:59:59.120000-00:00", "x-meta": False},
}
INVALID: dict[type[IrRecord], dict[str, object]] = {
    SubagentConfig: {**MINIMAL[SubagentConfig], "description": False},
    AgentConfig: {**MINIMAL[AgentConfig], "harness": False},
    HumanInputBinds: {"vcs_ref": False},
    HumanInputConfig: {**MINIMAL[HumanInputConfig], "channel": False},
    EventSelector: {"type": "wrong", "name": ""},
    SensorPoll: {"sensor": "http", "until": False},
    WaitEventConfig: {"timeout": "0s", "on_expiry": "fail"},
    TimerConfig: {"at": "2024-02-30T00:00:00Z"},
}
