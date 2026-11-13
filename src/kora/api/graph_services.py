from dataclasses import dataclass

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.ir.deployment_checks import DeploymentSelection


@dataclass(frozen=True, slots=True)
class GraphServices:
    database: Database
    preparer: DeploymentPreparer
    selection: DeploymentSelection
    identifiers: IdentifierGenerator
