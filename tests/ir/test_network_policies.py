from typing import cast

import pytest
from pydantic import MISSING

from kora.ir.agent_secrets import (
    UNENFORCED,
    NamespaceSecretPolicy,
    RuntimeEnforcement,
)
from kora.ir.document import GraphDocument
from kora.ir.errors import PLACEHOLDER_LOCATION
from kora.ir.network_policies import network_policy_findings
from tests.ir.secret_alias_cases import secret_document, secret_node
from tests.ir.test_agent_secrets import (
    ALLOWED,
    DEFAULTS,
    EGRESS,
    ENFORCING,
    SECRETS,
    agent,
    server,
)

Path = tuple[str | int, ...]
ENFORCING_ONLY = RuntimeEnforcement(
    enforces_network_policy=True, isolates_tool_secrets=False
)
AGENT_POLICY: Path = ("graph", "nodes", "agent", "config", "network_policy")
SERVER_POLICY: Path = (
    "graph",
    "nodes",
    "agent",
    "config",
    "mcp_servers",
    PLACEHOLDER_LOCATION,
    "network_policy",
)
COMMAND_POLICY: Path = ("graph", "nodes", "gate", "config", "network_policy")


def command(network_policy: object, secret_env: object = SECRETS) -> GraphDocument:
    node = secret_node("command", secret_env)
    cast("dict[str, object]", node["config"])["network_policy"] = network_policy
    return secret_document({"gate": node})


def tool(secret_env: object, network_policy: object) -> dict[str, object]:
    return {"type": "stdio", "secret_env": secret_env, "network_policy": network_policy}


def findings(
    document: GraphDocument,
    runtime: RuntimeEnforcement,
    policy: NamespaceSecretPolicy,
) -> set[tuple[str, Path | None]]:
    collection = network_policy_findings(document, {"local": runtime}, policy)
    assert collection.warnings == ()
    return {(error.code, error.path) for error in collection.errors}


@pytest.mark.parametrize(
    ("document", "runtime", "policy", "paths"),
    [
        pytest.param(
            agent(network_policy=EGRESS),
            UNENFORCED,
            DEFAULTS,
            {AGENT_POLICY},
            id="agent-egress",
        ),
        pytest.param(
            agent(network_policy={"egress": []}),
            UNENFORCED,
            DEFAULTS,
            {AGENT_POLICY},
            id="agent-deny-all",
        ),
        pytest.param(
            agent(network_policy={}),
            UNENFORCED,
            DEFAULTS,
            {AGENT_POLICY},
            id="agent-empty-object",
        ),
        pytest.param(
            agent(network_policy=EGRESS),
            UNENFORCED,
            ALLOWED,
            {AGENT_POLICY},
            id="agent-without-secrets-not-exempt",
        ),
        pytest.param(
            agent(SECRETS, EGRESS),
            UNENFORCED,
            DEFAULTS,
            {AGENT_POLICY},
            id="agent-secrets-not-allowed",
        ),
        pytest.param(
            command({"egress": []}),
            UNENFORCED,
            ALLOWED,
            {COMMAND_POLICY},
            id="command-never-exempt",
        ),
        pytest.param(
            agent(server=tool({}, EGRESS)),
            UNENFORCED,
            ALLOWED,
            {SERVER_POLICY},
            id="server-without-secrets-not-exempt",
        ),
        pytest.param(
            agent(server=server(EGRESS)),
            ENFORCING_ONLY,
            DEFAULTS,
            {SERVER_POLICY},
            id="server-without-isolation",
        ),
        pytest.param(
            agent(SECRETS, EGRESS, server=tool({}, EGRESS)),
            UNENFORCED,
            ALLOWED,
            {SERVER_POLICY},
            id="agent-secrets-do-not-exempt-server",
        ),
        pytest.param(
            agent(server=tool({}, EGRESS)),
            ENFORCING,
            DEFAULTS,
            {SERVER_POLICY},
            id="server-without-secrets-not-isolated",
        ),
        pytest.param(
            agent(network_policy=EGRESS, server=server(EGRESS)),
            UNENFORCED,
            ALLOWED,
            {AGENT_POLICY},
            id="server-secrets-do-not-exempt-agent",
        ),
        pytest.param(
            agent(SECRETS, EGRESS, server=server(EGRESS), runtime="docker"),
            ENFORCING,
            DEFAULTS,
            {AGENT_POLICY, SERVER_POLICY},
            id="uninstalled-runtime",
        ),
    ],
)
def test_unenforced_network_policies_are_refused(
    document: GraphDocument,
    runtime: RuntimeEnforcement,
    policy: NamespaceSecretPolicy,
    paths: set[Path],
) -> None:
    result = findings(document, runtime, policy)

    assert result == {("IR028", path) for path in paths}


@pytest.mark.parametrize(
    ("document", "runtime", "policy"),
    [
        pytest.param(agent(SECRETS, EGRESS), UNENFORCED, ALLOWED, id="agent-exempt"),
        pytest.param(
            agent(server=server({"egress": []})),
            UNENFORCED,
            ALLOWED,
            id="server-exempt",
        ),
        pytest.param(agent(network_policy=EGRESS), ENFORCING, DEFAULTS, id="agent"),
        pytest.param(command(EGRESS), ENFORCING_ONLY, DEFAULTS, id="command"),
        pytest.param(agent(server=server(EGRESS)), ENFORCING, DEFAULTS, id="server"),
        pytest.param(agent(SECRETS), UNENFORCED, DEFAULTS, id="omitted"),
        pytest.param(command(MISSING), UNENFORCED, DEFAULTS, id="command-omitted"),
    ],
)
def test_enforced_exempt_or_omitted_policies_are_accepted(
    document: GraphDocument,
    runtime: RuntimeEnforcement,
    policy: NamespaceSecretPolicy,
) -> None:
    result = findings(document, runtime, policy)

    assert result == set()
