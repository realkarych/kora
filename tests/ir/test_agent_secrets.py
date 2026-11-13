from typing import cast

import pytest
from pydantic import MISSING

from kora.ir.agent_secrets import (
    UNENFORCED,
    NamespaceSecretPolicy,
    RuntimeEnforcement,
    agent_secret_findings,
)
from kora.ir.document import GraphDocument
from kora.ir.errors import PLACEHOLDER_LOCATION
from kora.ir.export import export_ir
from kora.ir.json import JsonObject
from tests.ir.secret_alias_cases import secret_document, secret_node

Path = tuple[str | int, ...]
ENFORCING = RuntimeEnforcement(enforces_network_policy=True, isolates_tool_secrets=True)
ISOLATING_ONLY = RuntimeEnforcement(
    enforces_network_policy=False, isolates_tool_secrets=True
)
DEFAULTS = NamespaceSecretPolicy(
    allow_unenforced_agent_secrets=False, require_network_enforcement=False
)
ALLOWED = NamespaceSecretPolicy(
    allow_unenforced_agent_secrets=True, require_network_enforcement=False
)
REQUIRED = NamespaceSecretPolicy(
    allow_unenforced_agent_secrets=False, require_network_enforcement=True
)
EGRESS = {"egress": ["example.com"]}
SECRETS = {"TOKEN": "TOKEN"}
AGENT_PATH: Path = ("graph", "nodes", "agent", "config", "secret_env")
SERVER_PATH: Path = (
    "graph",
    "nodes",
    "agent",
    "config",
    "mcp_servers",
    PLACEHOLDER_LOCATION,
    "secret_env",
)
COMMAND_PATH: Path = ("graph", "nodes", "gate", "config", "secret_env")


def agent(
    secret_env: object = MISSING,
    network_policy: object = MISSING,
    server: dict[str, object] | None = None,
    **config: object,
) -> GraphDocument:
    node = secret_node(
        secret_env=secret_env,
        mcp_servers=MISSING if server is None else {"tools": server},
    )
    node_config = cast("dict[str, object]", node["config"])
    node_config.update(network_policy=network_policy, **config)
    return secret_document({"agent": node})


def on_default_runtime(document: GraphDocument, runtime: str) -> GraphDocument:
    data = export_ir(document)
    cast("JsonObject", data["runtime"])["runtime"] = runtime
    return GraphDocument.model_validate(data)


def server(network_policy: object = MISSING) -> dict[str, object]:
    return {"type": "stdio", "secret_env": SECRETS, "network_policy": network_policy}


def findings(
    document: GraphDocument,
    runtime: RuntimeEnforcement,
    policy: NamespaceSecretPolicy,
) -> set[tuple[str, Path | None]]:
    collection = agent_secret_findings(document, {"local": runtime}, policy)
    assert collection.warnings == ()
    return {(error.code, error.path) for error in collection.errors}


@pytest.mark.parametrize(
    ("document", "runtime", "path"),
    [
        pytest.param(agent(SECRETS), ENFORCING, AGENT_PATH, id="agent-no-policy"),
        pytest.param(agent(SECRETS, {}), ENFORCING, AGENT_PATH, id="agent-empty"),
        pytest.param(
            agent(SECRETS, EGRESS), UNENFORCED, AGENT_PATH, id="agent-unenforced"
        ),
        pytest.param(
            agent(SECRETS, EGRESS, runtime="docker"),
            ENFORCING,
            AGENT_PATH,
            id="agent-unknown-runtime",
        ),
        pytest.param(
            on_default_runtime(agent(SECRETS, EGRESS), "docker"),
            ENFORCING,
            AGENT_PATH,
            id="agent-uninstalled-default-runtime",
        ),
        pytest.param(
            agent(network_policy=EGRESS, server=server()),
            ENFORCING,
            SERVER_PATH,
            id="server-no-own-policy",
        ),
        pytest.param(
            agent(server=server(EGRESS)),
            ISOLATING_ONLY,
            SERVER_PATH,
            id="server-isolated-unenforced",
        ),
        pytest.param(
            agent(server=server(EGRESS)),
            RuntimeEnforcement(
                enforces_network_policy=True, isolates_tool_secrets=False
            ),
            SERVER_PATH,
            id="server-not-isolated",
        ),
    ],
)
def test_agent_secret_rule_fails_closed(
    document: GraphDocument, runtime: RuntimeEnforcement, path: Path
) -> None:
    refused = findings(document, runtime, DEFAULTS)
    lifted = findings(document, runtime, ALLOWED)

    assert refused == {("IR015", path)}
    assert lifted == set()


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(agent(SECRETS, EGRESS), id="agent-egress"),
        pytest.param(agent(SECRETS, {"egress": []}), id="agent-deny-all"),
        pytest.param(agent(server=server({"egress": []})), id="server-own-policy"),
        pytest.param(
            on_default_runtime(agent(SECRETS, EGRESS, runtime="local"), "docker"),
            id="node-runtime-overrides-default",
        ),
    ],
)
def test_agent_secret_rule_accepts_enforced_policies(document: GraphDocument) -> None:
    accepted = findings(document, ENFORCING, DEFAULTS)

    assert accepted == set()


def test_agent_secret_rule_ignores_graphs_without_secrets() -> None:
    documents = (
        agent(),
        agent({}, server={"type": "http", "secret_env": {}}),
        agent(server={"type": "stdio"}),
    )

    results = [findings(document, UNENFORCED, DEFAULTS) for document in documents]

    assert results == [set(), set(), set()]


def test_agent_secret_rule_checks_every_body_and_server() -> None:
    document = secret_document(
        {
            "agent": secret_node(
                secret_env=SECRETS,
                mcp_servers={"one": server(), "two": server(EGRESS)},
            )
        },
        bodies={"stored": {"nodes": {"inner": secret_node(secret_env=SECRETS)}}},
    )

    refused = agent_secret_findings(document, {"local": UNENFORCED}, DEFAULTS)
    lifted = agent_secret_findings(document, {"local": UNENFORCED}, ALLOWED)

    assert [(error.code, error.path, error.node) for error in refused.errors] == [
        ("IR015", SERVER_PATH, "agent"),
        ("IR015", SERVER_PATH, "agent"),
        ("IR015", AGENT_PATH, "agent"),
        (
            "IR015",
            ("graph", "subgraphs", "stored", "nodes", "inner", "config", "secret_env"),
            "inner",
        ),
    ]
    assert all("TOKEN" not in repr(error) for error in refused.errors)
    assert lifted.valid


@pytest.mark.parametrize(
    ("runtime", "policy", "refused"),
    [
        pytest.param(UNENFORCED, REQUIRED, True, id="required"),
        pytest.param(
            UNENFORCED,
            NamespaceSecretPolicy(
                allow_unenforced_agent_secrets=True, require_network_enforcement=True
            ),
            True,
            id="agent-permission-does-not-lift",
        ),
        pytest.param(UNENFORCED, ALLOWED, False, id="not-required"),
        pytest.param(ENFORCING, REQUIRED, False, id="enforcing-runtime"),
    ],
)
def test_command_secrets_follow_the_network_enforcement_requirement(
    runtime: RuntimeEnforcement, policy: NamespaceSecretPolicy, refused: bool
) -> None:
    document = secret_document({"gate": secret_node("command", SECRETS)})

    result = findings(document, runtime, policy)

    assert result == ({("IR015", COMMAND_PATH)} if refused else set())
