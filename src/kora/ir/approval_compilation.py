from dataclasses import dataclass, field
from enum import StrEnum
from itertools import chain

from pydantic import MISSING

from kora.cel import CheckedExpression
from kora.ir.approval_defaults import ApprovalDefaultChecks
from kora.ir.approval_predicates import ApprovalPredicateChecks
from kora.ir.cel_inventory_admission import (
    CheckedInventoryAdmission,
    retain_checked_site,
)
from kora.ir.diagnostic_collection import DiagnosticCollection, DiagnosticCollector
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


@dataclass(frozen=True, slots=True, repr=False)
class CheckedApprovalSite:
    path: tuple[str, ...]
    checked: CheckedExpression
    authored: bool


def checked_approval_sites(
    document: GraphDocument,
    *,
    max_declarations: int,
    admission: CheckedInventoryAdmission | None = None,
) -> (
    tuple[CheckedApprovalSite, ...] | DiagnosticCollection | ApprovalPrerequisite | None
):
    graph = document.graph
    definitions = {} if graph.subgraphs is MISSING else graph.subgraphs
    remaining = max_declarations - 1 - len(document.type_contracts) - len(definitions)
    if remaining < 0:
        return None
    for body in chain((graph,), definitions.values()):
        remaining -= len(body.nodes)
        if remaining < 0:
            return None
    collector = DiagnosticCollector()
    defaults = ApprovalDefaultChecks(document.type_contracts, collector, admission)
    authored = ApprovalPredicateChecks(
        document.type_contracts, graph.params, collector, admission
    )
    sites: list[CheckedApprovalSite] = []
    bodies = chain(
        ((graph, ("graph",)),),
        ((body, ("graph", "subgraphs", name)) for name, body in definitions.items()),
    )
    for body, path in bodies:
        for name, node in body.nodes.items():
            if not isinstance(node, HumanInputNode):
                continue
            node_path = (*path, "nodes", name)
            is_authored = node.config.approve_when is not MISSING
            checked = (
                authored.check(body, node, name, node_path)
                if is_authored
                else defaults.check(node, name, node_path)
            )
            if checked is not None:
                field_path = (*node_path, "config", "approve_when")
                retain_checked_site(admission, field_path, checked)
                sites.append(CheckedApprovalSite(field_path, checked, is_authored))
    if authored.syntax_unresolved:
        return ApprovalPrerequisite.SYNTAX_UNRESOLVED
    if authored.context_unresolved:
        return ApprovalPrerequisite.CONTEXT_UNRESOLVED
    if authored.schema_unresolved or defaults.unresolved:
        return ApprovalPrerequisite.SCHEMA_UNRESOLVED
    diagnostics = collector.finish()
    if not diagnostics.valid or diagnostics.warnings_omitted:
        return diagnostics
    return tuple(sites)


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
