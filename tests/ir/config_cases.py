from pydantic import BaseModel

from kora.ir import (
    CommandConfig,
    CommandResource,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    Workspace,
)

DIGEST: str = "sha256:" + "a" * 64
RESOURCE: dict[str, object] = {
    "name": "checks/gate.py",
    "sha256": DIGEST,
    "size": 262144,
    "executable": False,
}
MINIMAL: dict[type[BaseModel], dict[str, object]] = {
    NodeLimits: {},
    Workspace: {"repo": ""},
    NetworkPolicy: {},
    McpServer: {"type": "stdio"},
    CommandResource: RESOURCE,
    CommandConfig: {"cmd": [""], "workspace": {"repo": ""}},
}
POPULATED: dict[type[BaseModel], dict[str, object]] = {
    NodeLimits: {
        "timeout": "0ms",
        "max_events": 100000,
        "budget_usd": 0.25,
        "stall_after": "20m",
    },
    Workspace: {"repo": "repo", "rev": "", "from_ref": "inputs.change"},
    NetworkPolicy: {"egress": []},
    McpServer: {
        "type": "http",
        "command": "",
        "args": [],
        "url": "not a URI",
        "env": {},
        "secret_env": {},
        "network_policy": {},
        "headers": {},
        "digest": DIGEST,
    },
    CommandResource: RESOURCE,
    CommandConfig: {
        "cmd": ["tool", "{{ inputs.value }}"],
        "output_file": "",
        "resources": [RESOURCE] * 64,
        "resource_set_digest": DIGEST,
        "workspace": {"repo": "", "rev": ""},
        "env": {},
        "secret_env": {},
        "network_policy": {"egress": []},
        "success_exit_codes": [],
        "runtime": "",
        "vcs": "",
    },
}
CONFIG_FIELDS: frozenset[str] = frozenset(
    key for document in POPULATED.values() for key in document
)
