from dataclasses import dataclass

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.ir.agent_secrets import RuntimeEnforcement
from kora.ir.deployment_checks import DeploymentSelection
from kora.protocols.runtime import RuntimeCapabilities


@dataclass(frozen=True, slots=True)
class GraphServices:
    database: Database
    preparer: DeploymentPreparer
    selection: DeploymentSelection
    identifiers: IdentifierGenerator


def deployment_selection(
    harness: str, capabilities: RuntimeCapabilities
) -> DeploymentSelection:
    return DeploymentSelection(
        harness,
        RuntimeEnforcement(
            enforces_network_policy=capabilities.enforces_network_policy,
            isolates_tool_secrets=capabilities.isolates_tool_secrets,
        ),
    )
