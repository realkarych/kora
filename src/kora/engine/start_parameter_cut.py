from enum import StrEnum
from typing import Never, Self

from kora.cel_eval.bounds import MAX_BYTES
from kora.cel_eval.errors import raise_safe
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.run_input_values import RunInputValueRejected, encode_run_input_value
from kora.engine.start_records import PreparedManualStartRequest, StartRecordRejected
from kora.engine.start_replay import ManualStartCandidate
from kora.ir.graph_projection_paths import (
    ProjectionAdmission,
    ProjectionFailure,
    ProjectionRejected,
    cut_parameter_value,
    prepare_parameter_cut_paths,
)

_PARAMETER_CUT = object()


class ParameterCutFailure(StrEnum):
    BINDING = "binding"
    PATHS = "paths"
    CAPACITY = "capacity"
    VALUES = "values"


class ParameterCutRejected(ValueError):
    def __init__(self, reason: ParameterCutFailure) -> None:
        super().__init__(reason.value)
        self.reason: ParameterCutFailure = reason


class PreparedManualStartParameterCut:
    __slots__ = (
        "namespace_id",
        "graph_id",
        "version_id",
        "execution_contract_digest",
        "request_digest",
        "_projection",
    )
    namespace_id: str
    graph_id: str
    version_id: str
    execution_contract_digest: str
    request_digest: str
    _projection: bytes

    def __new__(
        cls,
        issuer: object,
        scope: GraphResourceScope,
        execution_contract_digest: str,
        request_digest: str,
        projection: bytes,
    ) -> Self:
        if issuer is not _PARAMETER_CUT:
            del issuer, scope, execution_contract_digest, request_digest, projection
            raise TypeError("manual parameter cut requires checked preparation")
        result = super().__new__(cls)
        object.__setattr__(result, "namespace_id", scope.namespace_id)
        object.__setattr__(result, "graph_id", scope.graph_id)
        object.__setattr__(result, "version_id", scope.version_id)
        object.__setattr__(
            result, "execution_contract_digest", execution_contract_digest
        )
        object.__setattr__(result, "request_digest", request_digest)
        object.__setattr__(result, "_projection", projection)
        return result

    def projection_bytes(self) -> bytes:
        return self._projection

    def __setattr__(self, name: str, value: object) -> Never:
        del self, value
        raise AttributeError("prepared manual parameter cut is immutable")

    def __delattr__(self, name: str) -> Never:
        del self
        raise AttributeError("prepared manual parameter cut is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("prepared manual parameter cut requires explicit access")


def _prepare_cut(
    request: PreparedManualStartRequest,
    key: str,
    candidate: ManualStartCandidate,
    checked: CheckedExecutionContract,
    max_work: int,
    max_scratch_bytes: int,
) -> PreparedManualStartParameterCut | ParameterCutFailure:
    if (
        type(request) is not PreparedManualStartRequest
        or type(candidate) is not ManualStartCandidate
        or type(checked) is not CheckedExecutionContract
    ):
        return ParameterCutFailure.BINDING
    identity, scope = request.identity, checked.scope
    selected = request.graph_version_id
    if (
        identity.namespace_id != scope.namespace_id
        or identity.graph_id != scope.graph_id
        or candidate.version_id != scope.version_id
        or (selected is not None and selected != scope.version_id)
    ):
        return ParameterCutFailure.BINDING
    try:
        if not candidate.is_new_for(identity.request_digest, key):
            return ParameterCutFailure.BINDING
        admission = ProjectionAdmission(max_work, max_scratch_bytes)
        paths = checked.parameter_paths
        plan = prepare_parameter_cut_paths(paths.whole, paths.presence, admission)
        cut = cut_parameter_value(request.parameters(), plan, admission)
        projection = encode_run_input_value(cut, max_bytes=MAX_BYTES)
    except ProjectionRejected as rejected:
        return {
            ProjectionFailure.CAPACITY: ParameterCutFailure.CAPACITY,
            ProjectionFailure.ROUTE: ParameterCutFailure.PATHS,
            ProjectionFailure.DATA: ParameterCutFailure.VALUES,
        }[rejected.reason]
    except StartRecordRejected:
        return ParameterCutFailure.BINDING
    except RunInputValueRejected:
        return ParameterCutFailure.PATHS
    return PreparedManualStartParameterCut(
        _PARAMETER_CUT,
        scope,
        checked.pin.execution_contract_digest,
        identity.request_digest,
        projection,
    )


def prepare_manual_start_parameter_cut(
    request: PreparedManualStartRequest,
    key: str,
    candidate: ManualStartCandidate,
    checked: CheckedExecutionContract,
    *,
    max_work: int,
    max_scratch_bytes: int,
) -> PreparedManualStartParameterCut:
    result = _prepare_cut(request, key, candidate, checked, max_work, max_scratch_bytes)
    if isinstance(result, PreparedManualStartParameterCut):
        return result
    del request, key, candidate, checked, max_work, max_scratch_bytes
    raise_safe(ParameterCutRejected(result))
