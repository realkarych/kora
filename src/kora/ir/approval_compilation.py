from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import MISSING

from kora.cel import CheckedExpression
from kora.ir.approval_defaults import ApprovalDefaultChecks
from kora.ir.approval_predicates import ApprovalPredicateChecks
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.topology import HumanInputNode


class ApprovalPrerequisite(StrEnum):
    SITE_UNAVAILABLE = "site_unavailable"
    SYNTAX_UNRESOLVED = "syntax_unresolved"
    CONTEXT_UNRESOLVED = "context_unresolved"
    SCHEMA_UNRESOLVED = "schema_unresolved"


@dataclass(frozen=True)
class CheckedApproval:
    path: tuple[str, ...] = field(repr=False)
    checked: CheckedExpression = field(repr=False)


def check_root_approval(
    document: GraphDocument, name: str
) -> CheckedApproval | DiagnosticCollection | ApprovalPrerequisite:
    graph = document.graph
    node = graph.nodes.get(name)
    if not isinstance(node, HumanInputNode):
        return ApprovalPrerequisite.SITE_UNAVAILABLE
    path = ("graph", "nodes", name)
    if node.config.approve_when is MISSING:
        defaults = ApprovalDefaultChecks(document.type_contracts)
        checked = defaults.check(node, name, path)
        if defaults.unresolved:
            return ApprovalPrerequisite.SCHEMA_UNRESOLVED
        if checked is None:
            return defaults.collector.finish()
    else:
        authored = ApprovalPredicateChecks(document.type_contracts, graph.params)
        checked = authored.check(graph, node, name, path)
        if authored.syntax_unresolved:
            return ApprovalPrerequisite.SYNTAX_UNRESOLVED
        if authored.context_unresolved:
            return ApprovalPrerequisite.CONTEXT_UNRESOLVED
        if authored.schema_unresolved:
            return ApprovalPrerequisite.SCHEMA_UNRESOLVED
        if checked is None:
            return authored.collector.finish()
    return CheckedApproval((*path, "config", "approve_when"), checked)
