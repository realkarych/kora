from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.config import AgentConfig, CommandConfig, NetworkPolicy
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.errors import PLACEHOLDER_LOCATION
from kora.ir.graph_links import safe_source
from kora.ir.topology import Node

Path = tuple[str | int, ...]
AGENT_HINT: str = (
    "Declare a network policy on a runtime that enforces it, "
    "or have the namespace allow unenforced agent secrets."
)
COMMAND_HINT: str = (
    "Run the command on a runtime that enforces network policy, or remove its secrets."
)


@dataclass(frozen=True, slots=True)
class RuntimeEnforcement:
    enforces_network_policy: bool
    isolates_tool_secrets: bool


UNENFORCED = RuntimeEnforcement(
    enforces_network_policy=False, isolates_tool_secrets=False
)


@dataclass(frozen=True, slots=True)
class NamespaceSecretPolicy:
    allow_unenforced_agent_secrets: bool
    require_network_enforcement: bool


def _declares(secrets: dict[str, str] | MISSING) -> bool:
    return secrets is not MISSING and bool(secrets)


def _restricts(policy: NetworkPolicy | MISSING) -> bool:
    return policy is not MISSING and policy.egress is not MISSING


def _nodes(document: GraphDocument) -> Iterator[tuple[str, Node, Path]]:
    graph = document.graph
    for name, node in graph.nodes.items():
        yield name, node, ("graph", "nodes", name)
    bodies = {} if graph.subgraphs is MISSING else graph.subgraphs
    for body_name, body in bodies.items():
        for name, node in body.nodes.items():
            yield name, node, ("graph", "subgraphs", body_name, "nodes", name)


class AgentSecretChecks:
    def __init__(
        self,
        document: GraphDocument,
        runtimes: Mapping[str, RuntimeEnforcement],
        policy: NamespaceSecretPolicy,
    ) -> None:
        self.default_runtime = document.runtime.runtime
        self.runtimes = runtimes
        self.policy = policy
        self.collector = DiagnosticCollector()

    def runtime(self, config: AgentConfig | CommandConfig) -> RuntimeEnforcement:
        name = self.default_runtime if config.runtime is MISSING else config.runtime
        return self.runtimes.get(name, UNENFORCED)

    def refuse(
        self, message: str, hint: str, path: Path, node: Node, name: str
    ) -> None:
        self.collector.add(
            Diagnostic(
                code="IR015",
                message=message,
                path=path,
                location=safe_source(node.source),
                node=name,
                hint=hint,
            )
        )

    def agent(self, config: AgentConfig, node: Node, name: str, path: Path) -> None:
        if self.policy.allow_unenforced_agent_secrets:
            return
        runtime = self.runtime(config)
        if _declares(config.secret_env) and not (
            _restricts(config.network_policy) and runtime.enforces_network_policy
        ):
            self.refuse(
                "The agent's secrets need a network policy its runtime enforces.",
                AGENT_HINT,
                path + ("secret_env",),
                node,
                name,
            )
        servers = () if config.mcp_servers is MISSING else config.mcp_servers.values()
        for server in servers:
            if _declares(server.secret_env) and not (
                _restricts(server.network_policy)
                and runtime.enforces_network_policy
                and runtime.isolates_tool_secrets
            ):
                self.refuse(
                    "The MCP server's secrets need its own network policy on a "
                    "runtime that isolates tool secrets and enforces it.",
                    AGENT_HINT,
                    path + ("mcp_servers", PLACEHOLDER_LOCATION, "secret_env"),
                    node,
                    name,
                )

    def command(self, config: CommandConfig, node: Node, name: str, path: Path) -> None:
        if (
            self.policy.require_network_enforcement
            and _declares(config.secret_env)
            and not self.runtime(config).enforces_network_policy
        ):
            self.refuse(
                "The namespace requires network enforcement for command secrets, "
                "and the runtime does not enforce network policy.",
                COMMAND_HINT,
                path + ("secret_env",),
                node,
                name,
            )


def agent_secret_findings(
    document: GraphDocument,
    runtimes: Mapping[str, RuntimeEnforcement],
    policy: NamespaceSecretPolicy,
) -> DiagnosticCollection:
    checks = AgentSecretChecks(document, runtimes, policy)
    for name, node, path in _nodes(document):
        config = node.config
        if isinstance(config, AgentConfig):
            checks.agent(config, node, name, path + ("config",))
        elif isinstance(config, CommandConfig):
            checks.command(config, node, name, path + ("config",))
    return checks.collector.finish()
