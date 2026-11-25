from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Never, Self

from pydantic import MISSING, ValidationError

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, TextBudget
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.graph_parameter_evaluation import (
    parameter_request_reservation,
    validate_graph_parameter_value,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_snapshot import AcquiredGraphSnapshot
from kora.engine.graph_types import read_document
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.start_capacity import Overlap, resolve_manual_overlap
from kora.engine.start_parameter_cel import (
    ParameterCelFailure,
    ParameterCelRejected,
    check_manual_start_parameter_cel,
)
from kora.engine.start_parameter_cut import (
    ParameterCutFailure,
    ParameterCutRejected,
    prepare_manual_start_parameter_cut,
)
from kora.engine.start_preparation import StartPreparationLimits
from kora.engine.start_records import PreparedManualStartRequest
from kora.engine.start_replay import ManualStartCandidate
from kora.ir.config import AgentConfig
from kora.ir.deployment_checks import (
    DeploymentSelection,
    approval_installation_findings,
    installation_findings,
    merge_diagnostics,
)
from kora.ir.diagnostic_collection import DiagnosticCollection
from kora.ir.document import GraphDocument
from kora.ir.runtime import Retention
from kora.ir.topology import Node


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
    acquired: AcquiredGraphSnapshot
    candidate: ManualStartCandidate


_MANUAL_START_FACTS = object()


class ManualStartFacts:
    __slots__ = (
        "namespace_id",
        "pin",
        "overlap",
        "timeout",
        "retention",
        "_request_digest",
        "_candidate",
        "projection",
    )
    namespace_id: str
    pin: GraphVersionPin
    overlap: Overlap
    timeout: str | None
    retention: Retention | MISSING
    _request_digest: str
    _candidate: ManualStartCandidate
    projection: bytes

    def __new__(
        cls,
        issuer: object,
        request: PreparedManualStartRequest,
        pin: GraphVersionPin,
        settings: tuple[Overlap, str | None, Retention | MISSING],
        candidate: ManualStartCandidate,
        projection: bytes,
    ) -> Self:
        if issuer is not _MANUAL_START_FACTS:
            del issuer, request, pin, settings, candidate, projection
            raise TypeError("manual start facts require the facts step")
        facts = super().__new__(cls)
        for name, value in (
            ("namespace_id", request.identity.namespace_id),
            ("pin", pin),
            ("overlap", settings[0]),
            ("timeout", settings[1]),
            ("retention", settings[2]),
            ("_candidate", candidate),
            ("projection", projection),
            ("_request_digest", request.identity.request_digest),
        ):
            object.__setattr__(facts, name, value)
        return facts

    def belongs_to(self, request: PreparedManualStartRequest, key: str) -> bool:
        identity = request.identity
        return self._candidate.is_new_for(identity.request_digest, key) and (
            identity.namespace_id,
            identity.graph_id,
            identity.request_digest,
        ) == (
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
    key: str,
    limits: StartPreparationLimits,
) -> StartFactsResult:
    limits.admit()
    identity = request.identity
    if (
        (identity.namespace_id, identity.graph_id)
        != (selected.namespace_id, selected.graph_id)
        or request.graph_version_id not in (None, selected.version_id)
        or not selected.candidate.is_new_for(identity.request_digest, key)
        or selected.candidate.version_id != selected.version_id
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    acquired = selected.acquired
    graph = acquired.graph
    pin = GraphVersionPin(
        selected.graph_id, selected.version_id, graph.digests[1].decode(graph.encoding)
    )
    checked = check_detached_execution_contract(
        graph,
        pin,
        GraphResourceScope(
            selected.namespace_id, selected.graph_id, selected.version_id
        ),
        acquired.physical,
        acquired.owners,
        limits=limits.snapshot.graph,
    )
    prepared = checked.parameter_evaluation
    if prepared is None:
        return StartRefusal.PARAMETERS_UNAVAILABLE
    maximum = limits.snapshot.graph.resources.logical.ir_bytes
    document = read_document(checked.canonical_ir, TextBudget(maximum))
    if declares_agent_secrets(document):
        return StartRefusal.SECRETS
    findings = merge_diagnostics(
        (
            installation_findings(document, selection),
            approval_installation_findings(document),
        )
    )
    if not findings.valid:
        return InstallationRefusal(findings)
    runtime_limits = document.runtime.limits
    settings = (
        resolve_manual_overlap(document.graph.overlap),
        None
        if runtime_limits is MISSING or runtime_limits.timeout is MISSING
        else runtime_limits.timeout,
        document.runtime.retention,
    )
    del document, runtime_limits, findings
    retained, work = parameter_request_reservation(request, prepared)
    decision = validate_graph_parameter_value(
        request, prepared, max_retained_bytes=retained, max_work=work
    )
    if decision.outcome != "valid":
        return (
            StartRefusal.PARAMETERS
            if decision.outcome == "invalid"
            else StartRefusal.PARAMETERS_UNAVAILABLE
        )
    cut = prepare_manual_start_parameter_cut(
        request,
        key,
        selected.candidate,
        checked,
        max_work=limits.cut_work,
        max_scratch_bytes=limits.cut_scratch,
    )
    check_manual_start_parameter_cel(
        cut, checked, max_ir_bytes=maximum, max_schema_work=limits.schema_work
    )
    return ManualStartFacts(
        _MANUAL_START_FACTS,
        request,
        pin,
        settings,
        selected.candidate,
        cut.projection_bytes(),
    )


def read_manual_start_facts(
    selected: SelectedStartVersion,
    request: PreparedManualStartRequest,
    selection: DeploymentSelection,
    *,
    key: str,
    limits: StartPreparationLimits,
) -> StartFactsResult:
    try:
        return _facts(selected, request, selection, key, limits)
    except ParameterCutRejected as rejected:
        return (
            StartRefusal.PARAMETERS_UNAVAILABLE
            if rejected.reason
            in (ParameterCutFailure.CAPACITY, ParameterCutFailure.BINDING)
            else StartRefusal.PARAMETERS
        )
    except ParameterCelRejected as rejected:
        return (
            StartRefusal.PARAMETERS
            if rejected.reason is ParameterCelFailure.VALUES
            else StartRefusal.PARAMETERS_UNAVAILABLE
        )
    except GraphValueRejected as rejection:
        reason = rejection.reason
    except (ValidationError, ValueError, RecursionError):
        reason = GraphValueFailure.INVALID
    del selected, request
    raise_safe(GraphValueRejected(reason))
