from dataclasses import dataclass
from enum import StrEnum

from pydantic import MISSING

from kora.api.graph_cel import GraphCelChecks
from kora.api.graph_deployment_limits import DEPLOYMENT, IR_BYTES
from kora.api.graph_execution_preparation import (
    GraphDeploymentCelRefusal,
    PreparedGraphDeploymentInputs,
    prepare_graph_deployment_inputs,
)
from kora.cel_eval.thread import Evaluator
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    TextBudget,
    render_object,
)
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.engine.graph_types import read_document
from kora.ir.deployment_checks import (
    DeploymentSelection,
    check_deployment_document,
)
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject

DIGEST_PREFIX = "sha256:"


class DeploymentRefusal(StrEnum):
    DOCUMENT = "document"
    TOO_LARGE = "too_large"
    SELECTORS = "selectors"
    INCOMPLETE = "incomplete"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class PreparedDeployment:
    graph_name: str
    inputs: PreparedGraphDeploymentInputs
    source_digest: str
    warnings: DiagnosticCollection


@dataclass(frozen=True, slots=True)
class DeploymentFindings:
    diagnostics: DiagnosticCollection


DeploymentPreparation = PreparedDeployment | DeploymentFindings | DeploymentRefusal


def _read(ir: JsonObject) -> tuple[str, GraphDocument] | DeploymentRefusal:
    try:
        text = render_object(ir, TextBudget(IR_BYTES))
        return text, read_document(text, TextBudget(IR_BYTES))
    except GraphValueRejected as rejected:
        if rejected.reason is GraphValueFailure.BUDGET:
            return DeploymentRefusal.TOO_LARGE
        return DeploymentRefusal.DOCUMENT


def _selected(document: GraphDocument, selectors: GraphDeploymentSelectors) -> bool:
    graph = document.graph
    return (
        graph.id == selectors.route_graph == selectors.request_graph
        and graph.namespace == selectors.authorized_namespace_name
    )


def _source_digest(document: GraphDocument, ir_digest: str) -> str:
    meta = document.meta
    if meta is MISSING or meta.source_digest is MISSING:
        return ir_digest
    return meta.source_digest.removeprefix(DIGEST_PREFIX)


def _cel_findings(refusal: GraphDeploymentCelRefusal) -> DeploymentPreparation:
    result = refusal.result
    if isinstance(result, GraphCelChecks) and not result.diagnostics.valid:
        return DeploymentFindings(result.diagnostics)
    if isinstance(result, DiagnosticCollection) and not result.valid:
        return DeploymentFindings(result)
    return DeploymentRefusal.INCOMPLETE


async def prepare_graph_deployment(
    ir: JsonObject,
    selectors: GraphDeploymentSelectors,
    selection: DeploymentSelection,
    evaluator: Evaluator,
) -> DeploymentPreparation:
    read = _read(ir)
    if isinstance(read, DeploymentRefusal):
        return read
    text, document = read
    if not _selected(document, selectors):
        return DeploymentRefusal.SELECTORS
    checks = check_deployment_document(document, selection)
    if not checks.complete:
        return DeploymentRefusal.INCOMPLETE
    if not checks.diagnostics.valid:
        return DeploymentFindings(checks.diagnostics)
    try:
        prepared = await prepare_graph_deployment_inputs(
            text,
            (),
            evaluator,
            selectors=selectors,
            storage_identity=None,
            limits=DEPLOYMENT,
        )
    except GraphValueRejected:
        return DeploymentRefusal.INCOMPLETE
    if isinstance(prepared, GraphDeploymentCelRefusal):
        return _cel_findings(prepared)
    return PreparedDeployment(
        document.graph.id,
        prepared,
        _source_digest(document, prepared.logical.digests.ir_digest),
        checks.diagnostics,
    )
