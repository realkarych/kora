from dataclasses import dataclass
from typing import TypeGuard

from pydantic import ValidationError

from kora.api.graph_execution_preparation import (
    PreparedExecutionContract,
    PreparedGraphDeploymentInputs,
    PreparedLogicalExecutionContract,
)
from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.types import DIGEST_LENGTH
from kora.engine.graph_command_manifests import (
    MAX_CONTENT_BYTES,
    MAX_FILES,
    CommandManifestLimits,
    PreparedCommandStageGroup,
)
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceReferenceLimits,
    GraphResourceReferences,
    GraphResourceScope,
    PreparedGraphResourceReferences,
    build_graph_resource_references,
)
from kora.engine.graph_version_identity import GraphVersionDigests
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.protocols.storage_models import (
    MAX_STORAGE_METADATA,
    ContentAddress,
    ContentRef,
    StorageIdentity,
    StorageModel,
)

IDENTIFIER_WIDTH = 256
STORAGE_DIGEST_WIDTH = len("sha256:") + DIGEST_LENGTH
ATTACHMENT_SLOTS = 32
REFERENCE_SNAPSHOT_UNITS = 8 * MAX_STORAGE_METADATA
GROUP_DESCRIPTOR_UNITS = 4 * (IDENTIFIER_WIDTH + STORAGE_DIGEST_WIDTH) + 16
ORDINAL_UNITS = 4
_ATTACHMENT_ISSUER = object()


@dataclass(frozen=True, slots=True)
class GraphDeploymentAttachmentLimits:
    resources: GraphResourceReferenceLimits
    physical_input_bytes: int
    max_retained_bytes: int
    max_work: int


class AttachedGraphDeployment(PreparedExecutionContract):
    __slots__ = ("_issuer",)
    _issuer: object

    def __init__(
        self,
        issuer: object,
        scope: GraphResourceScope,
        logical: PreparedLogicalExecutionContract,
        physical: PreparedGraphResourceReferences,
    ) -> None:
        if issuer is not _ATTACHMENT_ISSUER:
            del self, issuer, scope, logical, physical
            raise TypeError("graph deployment attachment requires the checked producer")
        super().__init__(
            scope,
            logical.canonical_ir,
            logical.canonical_contract,
            logical.digests,
            GraphResourceReferences(physical.text),
            physical.owners,
        )
        object.__setattr__(self, "_issuer", issuer)


def is_checked_graph_attachment(
    candidate: object,
) -> TypeGuard[AttachedGraphDeployment]:
    return (
        type(candidate) is AttachedGraphDeployment
        and getattr(candidate, "_issuer", None) is _ATTACHMENT_ISSUER
    )


def _bounded_text(value: object, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return value


def _native_fields(
    value: object, model: type[StorageModel], expected: tuple[str, ...]
) -> dict[str, object]:
    if type(value) is not model:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    fields: dict[str, object] = vars(value)
    if (
        type(fields) is not dict
        or len(fields) != len(expected)
        or any(type(name) is not str for name in fields)
        or any(name not in fields for name in expected)
        or getattr(value, "__pydantic_extra__", None) is not None
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    version = fields["version"]
    if type(version) is not int or version != 1:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return fields


def _snapshot_reference(reference: ContentRef) -> ContentRef:
    fields = _native_fields(
        reference,
        ContentRef,
        ("version", "address", "object_incarnation", "total_size"),
    )
    address = _native_fields(
        fields["address"],
        ContentAddress,
        ("version", "storage_identity", "payload_digest"),
    )
    storage = _native_fields(
        address["storage_identity"], StorageIdentity, ("version", "store_id")
    )
    size = fields["total_size"]
    if type(size) is not int or not 0 <= size <= COMMAND_RESOURCE_MAX_BYTES:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    store_id = _bounded_text(storage["store_id"], IDENTIFIER_WIDTH)
    incarnation = _bounded_text(fields["object_incarnation"], IDENTIFIER_WIDTH)
    digest = _bounded_text(address["payload_digest"], STORAGE_DIGEST_WIDTH)
    return ContentRef(
        version=1,
        address=ContentAddress(
            version=1,
            storage_identity=StorageIdentity(version=1, store_id=store_id),
            payload_digest=digest,
        ),
        object_incarnation=incarnation,
        total_size=size,
    )


def _snapshot_scope(scope: GraphResourceScope) -> GraphResourceScope:
    if type(scope) is not GraphResourceScope:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return GraphResourceScope(scope.namespace_id, scope.graph_id, scope.version_id)


def _snapshot_owner(owner: GraphResourceOwner) -> GraphResourceOwner:
    if type(owner) is not GraphResourceOwner:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return GraphResourceOwner(
        _snapshot_scope(owner.scope),
        owner.path,
        owner.name,
        _snapshot_reference(owner.reference),
    )


def _group_bytes(groups: tuple[PreparedCommandStageGroup, ...], count: int) -> int:
    store: str | None = None
    previous_digest = ""
    seen = 0
    total = 0
    entries = 0
    for group in groups:
        if type(group) is not PreparedCommandStageGroup:
            raise GraphValueRejected(GraphValueFailure.INVALID)
        store_id = _bounded_text(group.store_id, IDENTIFIER_WIDTH)
        digest = _bounded_text(group.payload_digest, STORAGE_DIGEST_WIDTH)
        if (
            (store is not None and store != store_id)
            or digest <= previous_digest
            or type(group.data) is not bytes
            or len(group.data) > COMMAND_RESOURCE_MAX_BYTES
            or type(group.ordinals) is not tuple
            or not 1 <= len(group.ordinals) <= count - entries
        ):
            raise GraphValueRejected(GraphValueFailure.INVALID)
        store, previous_digest = store_id, digest
        total += len(group.data)
        entries += len(group.ordinals)
        previous = -1
        for ordinal in group.ordinals:
            if type(ordinal) is not int or not previous < ordinal < count:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            bit = 1 << ordinal
            if seen & bit:
                raise GraphValueRejected(GraphValueFailure.INVALID)
            seen |= bit
            previous = ordinal
    if entries != count or total > MAX_CONTENT_BYTES:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return total


def _admit_attachment(
    prepared: PreparedGraphDeploymentInputs,
    owners: tuple[GraphResourceOwner, ...],
    limits: GraphDeploymentAttachmentLimits,
) -> None:
    if (
        type(prepared) is not PreparedGraphDeploymentInputs
        or type(prepared.logical) is not PreparedLogicalExecutionContract
        or type(prepared.logical.canonical_ir) is not str
        or type(prepared.logical.canonical_contract) is not str
        or type(prepared.logical.digests) is not GraphVersionDigests
        or type(prepared.groups) is not tuple
        or len(prepared.groups) > MAX_FILES
        or type(owners) is not tuple
        or len(owners) > MAX_FILES
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if (
        type(limits) is not GraphDeploymentAttachmentLimits
        or type(limits.resources) is not GraphResourceReferenceLimits
        or type(limits.resources.logical) is not CommandManifestLimits
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    resources = limits.resources
    logical = resources.logical
    if any(
        type(value) is not int or value <= 0
        for value in (
            limits.physical_input_bytes,
            limits.max_retained_bytes,
            limits.max_work,
            resources.source_bytes,
            resources.scratch,
            resources.work,
            logical.ir_bytes,
            logical.scratch,
            logical.work,
        )
    ):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    count = len(owners)
    overhead = (
        ATTACHMENT_SLOTS
        + len(prepared.groups) * GROUP_DESCRIPTOR_UNITS
        + count * (ORDINAL_UNITS + REFERENCE_SNAPSHOT_UNITS)
    )
    if overhead + resources.work + logical.work > limits.max_work:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    retained = (
        overhead
        + len(prepared.logical.canonical_ir)
        + len(prepared.logical.canonical_contract)
        + 3 * DIGEST_LENGTH
        + _group_bytes(prepared.groups, count)
        + resources.source_bytes
        + resources.scratch
        + logical.ir_bytes
        + logical.scratch
    )
    if retained > limits.max_retained_bytes:
        raise GraphValueRejected(GraphValueFailure.BUDGET)


def _require_group_references(
    groups: tuple[PreparedCommandStageGroup, ...],
    owners: tuple[GraphResourceOwner, ...],
) -> None:
    for group in groups:
        incarnation = owners[group.ordinals[0]].reference.object_incarnation
        for ordinal in group.ordinals:
            reference = owners[ordinal].reference
            if (
                reference.address.storage_identity.store_id != group.store_id
                or reference.address.payload_digest != group.payload_digest
                or reference.total_size != group.total_size
                or reference.object_incarnation != incarnation
            ):
                raise GraphValueRejected(GraphValueFailure.INVALID)


def _attach(
    prepared: PreparedGraphDeploymentInputs,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    limits: GraphDeploymentAttachmentLimits,
) -> AttachedGraphDeployment:
    _admit_attachment(prepared, owners, limits)
    target = _snapshot_scope(scope)
    detached = tuple(_snapshot_owner(owner) for owner in owners)
    logical = prepared.logical
    physical = build_graph_resource_references(
        logical.canonical_ir,
        target,
        detached,
        limits=limits.resources,
        max_output_bytes=limits.physical_input_bytes,
    )
    _require_group_references(prepared.groups, physical.owners)
    return AttachedGraphDeployment(_ATTACHMENT_ISSUER, target, logical, physical)


def attach_graph_deployment_references(
    prepared: PreparedGraphDeploymentInputs,
    scope: GraphResourceScope,
    owners: tuple[GraphResourceOwner, ...],
    *,
    limits: GraphDeploymentAttachmentLimits,
) -> AttachedGraphDeployment:
    try:
        return _attach(prepared, scope, owners, limits)
    except GraphValueRejected as rejected:
        reason = rejected.reason
    except ValidationError:
        reason = GraphValueFailure.INVALID
    del prepared, scope, owners, limits
    raise_safe(GraphValueRejected(reason))
