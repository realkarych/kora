import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Never, Self, cast

from pydantic import MISSING, ValidationError

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import AcquiredGraphVersion
from kora.engine.graph_parameters import read_graph_parameter_binding
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


_MANUAL_START_FACTS = object()


class ManualStartFacts:
    __slots__ = (
        "namespace_id",
        "pin",
        "overlap",
        "timeout",
        "retention",
        "_request_digest",
    )
    namespace_id: str
    pin: GraphVersionPin
    overlap: Overlap
    timeout: str | None
    retention: Retention | MISSING
    _request_digest: str

    def __new__(
        cls,
        issuer: object,
        request: PreparedManualStartRequest,
        pin: GraphVersionPin,
        document: GraphDocument,
    ) -> Self:
        if issuer is not _MANUAL_START_FACTS:
            del issuer, request, pin, document
            raise TypeError("manual start facts require the facts step")
        limits = document.runtime.limits
        facts = super().__new__(cls)
        for name, value in (
            ("namespace_id", request.identity.namespace_id),
            ("pin", pin),
            ("overlap", resolve_manual_overlap(document.graph.overlap)),
            (
                "timeout",
                None
                if limits is MISSING or limits.timeout is MISSING
                else limits.timeout,
            ),
            ("retention", document.runtime.retention),
            ("_request_digest", request.identity.request_digest),
        ):
            object.__setattr__(facts, name, value)
        return facts

    def belongs_to(self, request: PreparedManualStartRequest) -> bool:
        identity = request.identity
        return (identity.namespace_id, identity.graph_id, identity.request_digest) == (
            self.namespace_id,
            self.pin.graph_id,
            self._request_digest,
        )

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("manual start facts are immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("manual start facts are immutable")

    def __getstate__(self) -> Never:
        raise TypeError("manual start facts require explicit access")


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


def declares_agent_secrets(document: GraphDocument) -> bool:
    for node in _nodes(document):
        config = node.config
        if not isinstance(config, AgentConfig):
            continue
        servers = () if config.mcp_servers is MISSING else config.mcp_servers.values()
        if any(
            declared is not MISSING and bool(declared)
            for declared in (
                config.secret_env,
                *(server.secret_env for server in servers),
            )
        ):
            return True
    return False


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
    document = GraphDocument.model_validate(_strict_object(texts.ir))
    if document.graph.params is not MISSING:
        return StartRefusal.PARAMETERS_UNAVAILABLE
    saved = _strict_object(texts.execution_contract).get("parameters")
    try:
        read_graph_parameter_binding(
            document, saved, max_encoded_bytes=limits.max_encoded_bytes
        )
    except GraphValueRejected:
        return StartRefusal.PARAMETERS_UNAVAILABLE
    if request.parameter_bytes() != EMPTY_PARAMETERS:
        return StartRefusal.PARAMETERS
    if declares_agent_secrets(document):
        return StartRefusal.SECRETS
    findings = installation_findings(document, selection)
    if not findings.valid:
        return InstallationRefusal(findings)
    return ManualStartFacts(_MANUAL_START_FACTS, request, pin, document)


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
