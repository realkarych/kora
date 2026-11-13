from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.agent_secrets import (
    NamespaceSecretPolicy,
    RuntimeEnforcement,
    declares_secrets,
    execution_nodes,
    node_runtime,
)
from kora.ir.config import AgentConfig, CommandConfig, NetworkPolicy
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.errors import PLACEHOLDER_LOCATION
from kora.ir.first_slice_configuration import HINT
from kora.ir.graph_links import safe_source

Path = tuple[str | int, ...]
NODE_MESSAGE: str = "The runtime does not enforce network policy."
SERVER_MESSAGE: str = "The runtime does not enforce an MCP server's own network policy."


@dataclass(frozen=True, slots=True)
class PolicySite:
    policy: NetworkPolicy | MISSING
    exempt: bool
    enforced: bool
    path: Path
    message: str


def _sites(
    config: AgentConfig | CommandConfig,
    runtime: RuntimeEnforcement,
    policy: NamespaceSecretPolicy,
    path: Path,
) -> Iterator[PolicySite]:
    allowed = policy.allow_unenforced_agent_secrets
    if isinstance(config, CommandConfig):
        yield PolicySite(
            config.network_policy,
            False,
            runtime.enforces_network_policy,
            path + ("network_policy",),
            NODE_MESSAGE,
        )
        return
    yield PolicySite(
        config.network_policy,
        allowed and declares_secrets(config.secret_env),
        runtime.enforces_network_policy,
        path + ("network_policy",),
        NODE_MESSAGE,
    )
    servers = () if config.mcp_servers is MISSING else config.mcp_servers.values()
    for server in servers:
        isolated = runtime.isolates_tool_secrets and declares_secrets(server.secret_env)
        yield PolicySite(
            server.network_policy,
            allowed and declares_secrets(server.secret_env),
            runtime.enforces_network_policy and isolated,
            path + ("mcp_servers", PLACEHOLDER_LOCATION, "network_policy"),
            SERVER_MESSAGE,
        )


def network_policy_findings(
    document: GraphDocument,
    runtimes: Mapping[str, RuntimeEnforcement],
    policy: NamespaceSecretPolicy,
) -> DiagnosticCollection:
    collector = DiagnosticCollector()
    default = document.runtime.runtime
    for name, node, path in execution_nodes(document):
        config = node.config
        if not isinstance(config, AgentConfig | CommandConfig):
            continue
        runtime = node_runtime(default, config, runtimes)
        for site in _sites(config, runtime, policy, path + ("config",)):
            if site.policy is MISSING or site.enforced or site.exempt:
                continue
            collector.add(
                Diagnostic(
                    code="IR028",
                    message=site.message,
                    path=site.path,
                    location=safe_source(node.source),
                    node=name,
                    hint=HINT,
                )
            )
    return collector.finish()
