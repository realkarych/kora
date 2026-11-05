from functools import partial

from pydantic import MISSING

from kora.ir.approval_compilation import (
    ApprovalPrerequisite,
    CheckedApprovalSite,
    checked_approval_sites,
)
from kora.ir.branch_expressions import BranchExpressionResult, checked_branch_sites
from kora.ir.cel_inventory_admission import CheckedInventoryAdmission
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.expression_fields import CheckedCelSite
from kora.ir.retry_expressions import (
    CheckedRetrySite,
    InfrastructureRetryPrerequisite,
    RetryExpressionResult,
    checked_infrastructure_retry_site,
    checked_retry_sites,
)
from kora.ir.trigger_expressions import (
    SensorTriggerExpressionResult,
    checked_sensor_trigger_sites,
)
from kora.ir.wait_expressions import WaitExpressionResult, checked_wait_sites

TypedRefusal = (
    ApprovalPrerequisite
    | InfrastructureRetryPrerequisite
    | DiagnosticCollection
    | BranchExpressionResult
    | RetryExpressionResult
    | WaitExpressionResult
    | SensorTriggerExpressionResult
)


def checked_graph_sites(
    document: GraphDocument,
    max_declarations: int,
    max_triggers: int,
    *,
    admission: CheckedInventoryAdmission | None = None,
) -> tuple[CheckedCelSite, ...] | TypedRefusal | None:
    triggers = document.graph.triggers
    if triggers is not MISSING and len(triggers) > max_triggers:
        return None
    producers = (
        partial(checked_branch_sites, max_declarations=max_declarations),
        partial(checked_approval_sites, max_declarations=max_declarations),
        partial(checked_retry_sites, max_declarations=max_declarations),
        partial(checked_wait_sites, max_declarations=max_declarations),
        partial(checked_sensor_trigger_sites, max_triggers=max_triggers),
    )
    sites: list[CheckedCelSite] = []
    for produce in producers:
        inventory = produce(document, admission=admission)
        if not isinstance(inventory, tuple):
            return inventory
        sites.extend(
            CheckedCelSite(
                site.path,
                site.checked,
                site.authored if isinstance(site, CheckedApprovalSite) else True,
            )
            for site in inventory
        )
    infrastructure = checked_infrastructure_retry_site(document, admission=admission)
    if isinstance(infrastructure, CheckedRetrySite):
        sites.append(CheckedCelSite(infrastructure.path, infrastructure.checked, True))
    elif infrastructure is not None:
        return infrastructure
    return tuple(sites)
