from dataclasses import dataclass

from pydantic import MISSING

from kora.ir.config import AgentConfig, NodeLimits, Retry
from kora.ir.declarations import IrSourceLocation
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, outputs, safe_source
from kora.ir.link_cycles import cyclic_vertices
from kora.ir.topology import Graph, Node
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

Path = tuple[str | int, ...]
KINDS: tuple[str, ...] = ("agent", "command", "human_input")
HINT: str = (
    "Remove the unsupported configuration or use an installation that implements it."
)
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.first_slice_configuration.completed",
        COMPONENT,
        Level.INFO,
        "authored first-slice configuration check completed",
        {
            "authored_configuration_supported": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "triggers": FieldKind.INTEGER,
            "ordering_checked": FieldKind.INTEGER,
            "ordering_unresolved": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class FirstSliceConfigurationResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    triggers_checked: int
    ordering_checked: int
    ordering_unresolved: int

    @property
    def authored_configuration_supported(self) -> bool:
        return self.diagnostics.valid and self.ordering_unresolved == 0


class ConfigurationChecks:
    def __init__(self) -> None:
        self.collector = DiagnosticCollector()
        self.ordering_checked: int = 0
        self.ordering_unresolved: int = 0

    def refuse(
        self,
        message: str,
        path: Path,
        source: IrSourceLocation | MISSING = MISSING,
        name: str | None = None,
    ) -> None:
        self.collector.add(
            Diagnostic(
                code="IR028",
                message=message,
                path=path,
                location=safe_source(source),
                node=name,
                hint=HINT,
            )
        )

    def limits(
        self,
        limits: NodeLimits | MISSING,
        path: Path,
        source: IrSourceLocation | MISSING = MISSING,
        name: str | None = None,
    ) -> None:
        if limits is not MISSING and limits.budget_usd is not MISSING:
            self.refuse(
                "USD budgets are unavailable in the first slice.",
                path + ("budget_usd",),
                source,
                name,
            )

    def retry(
        self,
        retry: Retry | MISSING,
        path: Path,
        source: IrSourceLocation | MISSING = MISSING,
        name: str | None = None,
    ) -> None:
        if (
            retry is not MISSING
            and retry.max_attempts is not MISSING
            and retry.max_attempts > 1
        ):
            self.refuse(
                "Additional attempts are unavailable in the first slice.",
                path + ("max_attempts",),
                source,
                name,
            )

    def node(self, node: Node, name: str, path: Path) -> None:
        if node.kind not in KINDS:
            self.refuse(
                "This node kind is unavailable in the first slice.",
                path + ("kind",),
                node.source,
                name,
            )
        self.limits(node.limits, path + ("limits",), node.source, name)
        self.retry(node.retry, path + ("retry",), node.source, name)
        config = node.config
        if not isinstance(config, AgentConfig):
            return
        for field, enabled, message in (
            (
                "skills",
                config.skills is not MISSING and bool(config.skills),
                "Declared skills are unavailable in the first slice.",
            ),
            (
                "subagents",
                config.subagents is not MISSING and bool(config.subagents),
                "Declared subagents are unavailable in the first slice.",
            ),
            (
                "hooks",
                config.hooks is not MISSING and bool(config.hooks),
                "Declared hooks are unavailable in the first slice.",
            ),
            (
                "mcp_servers",
                config.mcp_servers is not MISSING and bool(config.mcp_servers),
                "Declared MCP servers are unavailable in the first slice.",
            ),
            (
                "trust_repo_config",
                config.trust_repo_config is True,
                "Repository configuration trust is unavailable in the first slice.",
            ),
        ):
            if enabled:
                self.refuse(message, path + ("config", field), node.source, name)

    def topology(self, graph: Graph) -> None:
        if len(graph.nodes) != 3 or {node.kind for node in graph.nodes.values()} != set(
            KINDS
        ):
            self.refuse(
                "The first slice requires one agent, command and human-input chain.",
                ("graph", "nodes"),
                graph.source,
            )
            return
        names = {node.kind: name for name, node in graph.nodes.items()}
        edges: dict[str, set[str]] = {name: set() for name in graph.nodes}
        unresolved: set[str] = set()
        for name, node in graph.nodes.items():
            if node.depends_on is not MISSING:
                for dependency in node.depends_on:
                    if dependency in graph.nodes:
                        edges[name].add(dependency)
                    else:
                        unresolved.add(name)
            if node.inputs is MISSING:
                continue
            for value in node.inputs.values():
                producer = value.from_
                if producer == "$params":
                    continue
                if producer is MISSING or producer not in graph.nodes:
                    unresolved.add(name)
                    continue
                edges[name].add(producer)
                if value.output not in outputs(graph.nodes[producer]):
                    unresolved.add(name)
        unresolved.update(cyclic_vertices(edges))
        for index, kind in enumerate(KINDS):
            name = names[kind]
            previous = None if index == 0 else names[KINDS[index - 1]]
            participants = edges[name] | {name}
            if previous is not None:
                participants.add(previous)
            if participants & unresolved:
                self.ordering_unresolved += 1
                continue
            self.ordering_checked += 1
            if edges[name] - {names[value] for value in KINDS[:index]} or (
                previous is not None and previous not in edges[name]
            ):
                self.refuse(
                    "The declared dependencies do not follow the first-slice chain.",
                    ("graph", "nodes", name),
                    graph.nodes[name].source,
                    name,
                )


def validate_first_slice_configuration(
    document: GraphDocument,
) -> FirstSliceConfigurationResult:
    checks = ConfigurationChecks()
    graph = document.graph
    checks.topology(graph)
    triggers = () if graph.triggers is MISSING else graph.triggers
    for index, trigger in enumerate(triggers):
        if trigger.kind != "manual":
            checks.refuse(
                "Nonmanual starts are unavailable in the first slice.",
                ("graph", "triggers", index, "kind"),
                graph.source,
            )
    if graph.target is not MISSING:
        checks.refuse(
            "Graph targets are unavailable in the first slice.",
            ("graph", "target"),
            graph.source,
        )
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    if definitions:
        checks.refuse(
            "Stored subgraphs are unavailable in the first slice.",
            ("graph", "subgraphs"),
            graph.source,
        )
    nodes = 0
    for name, node in graph.nodes.items():
        nodes += 1
        checks.node(node, name, ("graph", "nodes", name))
    for body_name, body in definitions.items():
        path = ("graph", "subgraphs", body_name)
        checks.limits(body.limits, path + ("limits",), body.source)
        checks.retry(body.retry, path + ("retry",), body.source)
        for name, node in body.nodes.items():
            nodes += 1
            checks.node(node, name, path + ("nodes", name))
    runtime = document.runtime
    checks.limits(runtime.limits, ("runtime", "limits"))
    checks.retry(runtime.infrastructure_retry, ("runtime", "infrastructure_retry"))
    if runtime.budget_headroom_pct is not MISSING:
        checks.refuse(
            "USD budget headroom is unavailable in the first slice.",
            ("runtime", "budget_headroom_pct"),
        )
    provider = runtime.provider_retry
    if (
        provider is not MISSING
        and provider.max_wait is not MISSING
        and any(character in "123456789" for character in provider.max_wait)
    ):
        checks.refuse(
            "Provider waits are unavailable in the first slice.",
            ("runtime", "provider_retry", "max_wait"),
        )
    result = FirstSliceConfigurationResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        len(triggers),
        checks.ordering_checked,
        checks.ordering_unresolved,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            authored_configuration_supported=result.authored_configuration_supported,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=result.nodes_checked,
            definitions=result.definitions_checked,
            triggers=result.triggers_checked,
            ordering_checked=result.ordering_checked,
            ordering_unresolved=result.ordering_unresolved,
        )
    return result
