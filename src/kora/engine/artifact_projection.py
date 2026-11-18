import hashlib
from dataclasses import dataclass
from typing import Never

from kora.cel_eval.artifact_projection_codec import (
    RECORD_ENTRIES,
    RECORD_VARIABLE_BYTES,
    RECORD_WORK,
    REPRESENTATION,
    ProjectionFailure,
    ProjectionIdentity,
    ProjectionLimits,
    ProjectionRecord,
    ProjectionRejected,
    decode_record,
)
from kora.cel_eval.artifact_projection_view import prepare_record
from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueRejected
from kora.engine.artifact_projection_source import ProjectionBinding, projection_source
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.locks import COMPONENT
from kora.ir.cel_inventory_admission import (
    InventoryAdmissionExhausted,
)
from kora.ir.standard_schema_policy import LIMITS as SCHEMA_LIMITS
from kora.ir.structured_json import MAX_JSON_BYTES
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.common import ProtocolContext
from kora.protocols.effective_artifact_type import (
    EffectiveArtifactType,
    EffectiveRefusal,
    EffectiveValid,
    InstalledBehaviors,
    prepare_effective_artifact_type,
)

_PROJECTION_ISSUER = object()
MODEL_VARIABLE_BYTES = 64 * MAX_JSON_BYTES + SCHEMA_LIMITS["max_variable_bytes"]
MODEL_ENTRIES = 64 * MAX_JSON_BYTES + SCHEMA_LIMITS["max_auxiliary_entries"]
MODEL_WORK = (
    64 * MAX_JSON_BYTES * MAX_JSON_BYTES + 4 * SCHEMA_LIMITS["max_validation_score"]
)
COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.artifact_projection.completed",
        COMPONENT,
        Level.DEBUG,
        "artifact CEL projection operation completed",
        {"operation": FieldKind.STRING, "outcome": FieldKind.STRING},
    )
)


class PreparedArtifactProjection:
    __slots__ = ("binding", "_record")
    binding: ProjectionBinding
    _record: ProjectionRecord

    def __init__(
        self, binding: ProjectionBinding, record: ProjectionRecord, *, issuer: object
    ) -> None:
        if issuer is not _PROJECTION_ISSUER or hasattr(self, "binding"):
            del self, binding, record, issuer
            raise TypeError("artifact projection requires complete preparation")
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "_record", record)

    def __setattr__(self, name: str, value: object) -> Never:
        del self, value
        raise AttributeError("artifact projection is immutable")

    def __delattr__(self, name: str) -> Never:
        del self
        raise AttributeError("artifact projection is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("artifact projection requires explicit access")

    @property
    def identity(self) -> ProjectionIdentity:
        return self._record.identity

    @property
    def content_digest(self) -> str:
        return self._record.content_digest

    def encoded(self) -> bytes:
        return self._record.content


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionReadMetadata:
    binding: ProjectionBinding
    identity: ProjectionIdentity
    content_digest: str
    content_bytes: int
    representation: str
    available: bool


def report(operation: str, outcome: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            COMPLETED, operation=operation, outcome=outcome
        )


def _prepare(
    checked: CheckedExecutionContract,
    path: tuple[str, ...],
    value: object,
    behaviors: InstalledBehaviors,
    ctx: ProtocolContext,
    limits: ProjectionLimits,
) -> PreparedArtifactProjection | ProjectionFailure:
    try:
        source = projection_source(checked, path, limits, validating=True)
        source.admission.reserve(
            MODEL_VARIABLE_BYTES + RECORD_VARIABLE_BYTES,
            MODEL_ENTRIES + RECORD_ENTRIES,
            MODEL_WORK + RECORD_WORK,
        )
    except ProjectionRejected as rejected:
        return rejected.reason
    except InventoryAdmissionExhausted:
        return ProjectionFailure.CAPACITY
    except GraphValueRejected:
        return ProjectionFailure.BINDING
    effective = prepare_effective_artifact_type(source.schema, behaviors)
    if isinstance(effective, EffectiveRefusal):
        return (
            ProjectionFailure.CAPACITY
            if effective.reason == "evaluation_exhausted"
            else ProjectionFailure.UNAVAILABLE
        )
    if not isinstance(effective, EffectiveArtifactType):
        return ProjectionFailure.UNAVAILABLE
    decision = effective.validate(value, ctx)
    if isinstance(decision, EffectiveRefusal):
        return (
            ProjectionFailure.CAPACITY
            if decision.reason == "evaluation_exhausted"
            else ProjectionFailure.UNAVAILABLE
        )
    if not isinstance(decision, EffectiveValid):
        return ProjectionFailure.VALIDATION
    try:
        original_model = decision.value
        canonical = original_model.canonical_bytes()
        identity = ProjectionIdentity(
            source.binding.contract_digest,
            hashlib.sha256(canonical).hexdigest(),
            source.binding.path_set_digest,
        )
        return PreparedArtifactProjection(
            source.binding,
            prepare_record(
                identity, original_model.value, source.view, source.admission
            ),
            issuer=_PROJECTION_ISSUER,
        )
    except ProjectionRejected as rejected:
        return rejected.reason
    except InventoryAdmissionExhausted:
        return ProjectionFailure.CAPACITY


def prepare_artifact_projection(
    checked: CheckedExecutionContract,
    path: tuple[str, ...],
    value: object,
    behaviors: InstalledBehaviors,
    ctx: ProtocolContext,
    *,
    limits: ProjectionLimits,
) -> PreparedArtifactProjection:
    result = _prepare(checked, path, value, behaviors, ctx, limits)
    report(
        "prepare",
        "prepared" if isinstance(result, PreparedArtifactProjection) else result,
    )
    if isinstance(result, PreparedArtifactProjection):
        return result
    del checked, path, value, behaviors, ctx, limits
    raise_safe(ProjectionRejected(result))


def _check(
    checked: CheckedExecutionContract,
    path: tuple[str, ...],
    content: object,
    owner: ProjectionReadMetadata,
    limits: ProjectionLimits,
) -> PreparedArtifactProjection | ProjectionFailure:
    try:
        if type(owner) is not ProjectionReadMetadata or (
            type(owner.representation) is not str
            or owner.representation != REPRESENTATION
            or owner.available is not True
        ):
            return ProjectionFailure.UNAVAILABLE
        source = projection_source(checked, path, limits, validating=False)
        if (
            type(owner.binding) is not ProjectionBinding
            or owner.binding != source.binding
        ):
            return ProjectionFailure.BINDING
        if type(owner.identity) is not ProjectionIdentity or (
            owner.identity.contract_digest != source.binding.contract_digest
            or owner.identity.path_set_digest != source.binding.path_set_digest
        ):
            return ProjectionFailure.BINDING
        record = decode_record(
            content,
            owner.identity,
            owner.content_digest,
            owner.content_bytes,
            source.view.variable,
            source.admission,
        )
        return PreparedArtifactProjection(
            source.binding, record, issuer=_PROJECTION_ISSUER
        )
    except ProjectionRejected as rejected:
        return rejected.reason
    except InventoryAdmissionExhausted:
        return ProjectionFailure.CAPACITY
    except GraphValueRejected:
        return ProjectionFailure.BINDING


def check_artifact_projection(
    checked: CheckedExecutionContract,
    path: tuple[str, ...],
    content: object,
    owner: ProjectionReadMetadata,
    *,
    limits: ProjectionLimits,
) -> PreparedArtifactProjection:
    result = _check(checked, path, content, owner, limits)
    report(
        "check", "checked" if isinstance(result, PreparedArtifactProjection) else result
    )
    if isinstance(result, PreparedArtifactProjection):
        return result
    del checked, path, content, owner, limits
    raise_safe(ProjectionRejected(result))
