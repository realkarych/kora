from dataclasses import dataclass
from itertools import chain

from pydantic import MISSING

from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.graph_links import COMPONENT, safe_source
from kora.ir.tool_patterns import tool_policy_refusals
from kora.ir.topology import AgentNode
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "ir.tool_policies.completed",
        COMPONENT,
        Level.INFO,
        "declared tool policy validation completed",
        {
            "tool_policies_valid": FieldKind.BOOLEAN,
            "errors": FieldKind.INTEGER,
            "errors_omitted": FieldKind.INTEGER,
            "nodes": FieldKind.INTEGER,
            "definitions": FieldKind.INTEGER,
            "policies": FieldKind.INTEGER,
            "patterns": FieldKind.INTEGER,
        },
    )
)


@dataclass(frozen=True)
class ToolPolicyResult:
    diagnostics: DiagnosticCollection
    nodes_checked: int
    definitions_checked: int
    policies_checked: int
    patterns_checked: int

    @property
    def tool_policies_valid(self) -> bool:
        return self.diagnostics.valid


def validate_tool_policies(document: GraphDocument) -> ToolPolicyResult:
    collector = DiagnosticCollector()
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    nodes = policies = patterns = 0
    for body, path in bodies:
        for name, node in body.nodes.items():
            nodes += 1
            if not isinstance(node, AgentNode) or node.config.tool_policy is MISSING:
                continue
            policy = node.config.tool_policy
            policies += 1
            patterns += len(policy.allow) + len(policy.deny)
            for relative, refusal in tool_policy_refusals(policy):
                collector.add(
                    Diagnostic(
                        code="IR013",
                        message=refusal.value,
                        path=path + ("nodes", name, "config", "tool_policy") + relative,
                        node=name,
                        location=safe_source(node.source),
                    )
                )
    result = ToolPolicyResult(
        collector.finish(), nodes, len(definitions), policies, patterns
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED,
            tool_policies_valid=result.tool_policies_valid,
            errors=len(result.diagnostics.errors) + result.diagnostics.errors_omitted,
            errors_omitted=result.diagnostics.errors_omitted,
            nodes=nodes,
            definitions=result.definitions_checked,
            policies=policies,
            patterns=patterns,
        )
    return result
