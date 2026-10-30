from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.declarations import IrSourceLocation
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.errors import PLACEHOLDER_LOCATION
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.topology import AgentNode, CommandNode
from kora.ir.trigger import WebhookTrigger
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.secret_aliases.completed",
        COMPONENT,
        Level.INFO,
        "secret alias membership validation completed",
        {
            "secret_aliases_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "triggers": FieldKind.INTEGER,
            "mcp_servers": FieldKind.INTEGER,
            "references": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class SecretAliasResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    triggers_checked: int
    mcp_servers_checked: int
    references_checked: int

    @property
    def secret_aliases_valid(self) -> bool:
        return self.diagnostics.valid


class SecretAliasChecks:
    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.collector = DiagnosticCollector()
        self.references: int = 0

    def reference(
        self,
        alias: str,
        path: tuple[str | int, ...],
        source: IrSourceLocation | MISSING,
        node: str | None = None,
    ) -> None:
        self.references += 1
        if alias not in self.aliases:
            self.collector.add(
                Diagnostic(
                    code="IR007",
                    message="The secret alias is not declared.",
                    path=path,
                    node=node,
                    location=safe_source(source),
                )
            )

    def environment(
        self,
        values: dict[str, str] | MISSING,
        path: tuple[str | int, ...],
        source: IrSourceLocation | MISSING,
        node: str,
        *,
        accepted_names: bool,
    ) -> None:
        if values is MISSING:
            return
        for name, alias in values.items():
            self.reference(
                alias,
                path + (name if accepted_names else PLACEHOLDER_LOCATION,),
                source,
                node,
            )


def validate_secret_aliases(document: GraphDocument) -> SecretAliasResult:
    aliases = (
        {}
        if document.secrets is MISSING or document.secrets.aliases is MISSING
        else document.secrets.aliases
    )
    checks = SecretAliasChecks(aliases)
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = 0
    servers = 0
    for body, path in bodies:
        for name, node in body.nodes.items():
            nodes += 1
            if not isinstance(node, AgentNode | CommandNode):
                continue
            config_path = path + ("nodes", name, "config")
            checks.environment(
                node.config.secret_env,
                config_path + ("secret_env",),
                node.source,
                name,
                accepted_names=True,
            )
            if isinstance(node, AgentNode) and node.config.mcp_servers is not MISSING:
                for server in node.config.mcp_servers.values():
                    servers += 1
                    checks.environment(
                        server.secret_env,
                        config_path
                        + ("mcp_servers", PLACEHOLDER_LOCATION, "secret_env"),
                        node.source,
                        name,
                        accepted_names=False,
                    )
    triggers = () if graph.triggers is MISSING else graph.triggers
    for index, trigger in enumerate(triggers):
        if isinstance(trigger, WebhookTrigger):
            checks.reference(
                trigger.auth.secret,
                ("graph", "triggers", index, "auth", "secret"),
                graph.source,
            )
    result = SecretAliasResult(
        checks.collector.finish(),
        nodes,
        len(definitions),
        len(triggers),
        servers,
        checks.references,
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            secret_aliases_valid=result.secret_aliases_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=result.nodes_checked,
            definitions=result.definitions_checked,
            triggers=result.triggers_checked,
            mcp_servers=result.mcp_servers_checked,
            references=result.references_checked,
        )
    return result
