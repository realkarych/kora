import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from pydantic import MISSING, ValidationError

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import AcquiredGraphVersion
from kora.engine.graph_parameters import PARAMETER_INTERPRETATION
from kora.engine.graph_version_identity import (
    GraphVersionIdentityLimits,
    GraphVersionPin,
    read_acquired_graph_version_texts,
)
from kora.engine.start_capacity import Overlap, resolve_manual_overlap
from kora.engine.start_records import PreparedManualStartRequest
from kora.ir.config import AgentConfig
from kora.ir.deployment_checks import DeploymentSelection, installation_findings
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.runtime import Retention
from kora.ir.topology import Node
from kora.ir.vcs_ref_json import finite_float, refuse_constant, unique_object

EMPTY_PARAMETERS = b"{}"
CLOSED_EMPTY_PARAMETERS: dict[str, object] = {
    "interpretation": PARAMETER_INTERPRETATION,
    "schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


class StartRefusal(StrEnum):
    PARAMETERS = "parameters"
    PARAMETERS_UNAVAILABLE = "parameters_unavailable"
    SECRETS = "secrets"


@dataclass(frozen=True, slots=True)
class InstallationRefusal:
    diagnostics: DiagnosticCollection


@dataclass(frozen=True, slots=True)
class SelectedStartVersion:
    namespace_id: str
    graph_id: str
    version_id: str
    acquired: AcquiredGraphVersion


@dataclass(frozen=True, slots=True)
class ManualStartFacts:
    namespace_id: str
    pin: GraphVersionPin
    overlap: Overlap
    timeout: str | None
    retention: Retention | MISSING


StartFactsResult = ManualStartFacts | StartRefusal | InstallationRefusal


def _strict_object(text: str) -> dict[str, object]:
    decoded: object = json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=refuse_constant,
        parse_float=finite_float,
    )
    if type(decoded) is not dict:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return cast("dict[str, object]", decoded)


def _nodes(document: GraphDocument) -> Iterator[Node]:
    graph = document.graph
    yield from graph.nodes.values()
    if graph.subgraphs is not MISSING:
        for body in graph.subgraphs.values():
            yield from body.nodes.values()


def _facts(
    selected: SelectedStartVersion,
    request: PreparedManualStartRequest,
    selection: DeploymentSelection,
    limits: GraphVersionIdentityLimits,
) -> StartFactsResult:
    identity = request.identity
    if (identity.namespace_id, identity.graph_id) != (
        selected.namespace_id,
        selected.graph_id,
    ) or request.graph_version_id not in (None, selected.version_id):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    pin, texts = read_acquired_graph_version_texts(selected.acquired, limits=limits)
    if pin.version_id != selected.version_id:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    document = GraphDocument.model_validate(_strict_object(texts.ir))
    contract = _strict_object(texts.execution_contract)
    if (
        document.graph.params is not MISSING
        or contract.get("parameters") != CLOSED_EMPTY_PARAMETERS
    ):
        return StartRefusal.PARAMETERS_UNAVAILABLE
    if request.parameter_bytes() != EMPTY_PARAMETERS:
        return StartRefusal.PARAMETERS
    if any(
        isinstance(node.config, AgentConfig)
        and node.config.secret_env is not MISSING
        and bool(node.config.secret_env)
        for node in _nodes(document)
    ):
        return StartRefusal.SECRETS
    findings = installation_findings(document, selection)
    if not findings.valid:
        return InstallationRefusal(findings)
    runtime = document.runtime
    limits_section = runtime.limits
    return ManualStartFacts(
        namespace_id=selected.namespace_id,
        pin=pin,
        overlap=resolve_manual_overlap(document.graph.overlap),
        timeout=None
        if limits_section is MISSING or limits_section.timeout is MISSING
        else limits_section.timeout,
        retention=runtime.retention,
    )


def read_manual_start_facts(
    selected: SelectedStartVersion,
    request: PreparedManualStartRequest,
    selection: DeploymentSelection,
    *,
    limits: GraphVersionIdentityLimits,
) -> StartFactsResult:
    try:
        return _facts(selected, request, selection, limits)
    except GraphValueRejected as rejection:
        reason = rejection.reason
    except (ValidationError, ValueError, RecursionError):
        reason = GraphValueFailure.INVALID
    del selected, request
    raise_safe(GraphValueRejected(reason))
