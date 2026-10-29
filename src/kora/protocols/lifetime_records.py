import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr

from kora.ir.scalars import Digest
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import Identifier, Pid, ProcessIdentity

InvocationId = Annotated[
    StrictStr, Field(pattern=re.compile(r"^[0-9a-f]{32}(?![\s\S])"))
]
ResourceId = InvocationId
OperationId = InvocationId
Revision = Annotated[StrictInt, Field(gt=0, le=2**64 - 1)]
Generation = Revision
Uint64 = Annotated[StrictInt, Field(ge=0, le=2**64 - 1)]
Uint32 = Annotated[StrictInt, Field(ge=0, le=2**32 - 1)]


class ResourceKind(StrEnum):
    PROCESS_DOMAIN = "process_domain"
    LOCAL_RUNTIME_DOMAIN = "local_runtime_domain"
    TEMPORARY_TREE = "temporary_tree"
    BORROWED_SERVICE = "borrowed_service"


class ResourceState(StrEnum):
    RESERVED = "reserved"
    ACQUIRING = "acquiring"
    GATED = "gated"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class LifetimeReason(StrEnum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ACQUISITION_FAILED = "acquisition_failed"
    IDENTITY_UNKNOWN = "identity_unknown"
    IDENTITY_MISMATCH = "identity_mismatch"
    EFFECT_UNSETTLED = "effect_unsettled"
    RESOURCE_LIVE = "resource_live"
    CLEANUP_FAILED = "cleanup_failed"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    OWNER_BUSY = "owner_busy"
    JOURNAL_UNAVAILABLE = "journal_unavailable"


class OriginalOutcome(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LocalRuntimeBinding(BoundaryModel):
    execution_domain_id: Identifier
    state_root_resource_id: ResourceId
    attempt_id: Identifier
    launch_nonce: Identifier
    request_digest: Digest


class ProcessRegistration(BoundaryModel):
    kind: Literal["process_domain"] = "process_domain"
    process: ProcessIdentity
    acquisition_operation_id: OperationId
    process_group_id: Pid
    session_id: Pid


class LocalRuntimeRegistration(BoundaryModel):
    kind: Literal["local_runtime_domain"] = "local_runtime_domain"
    binding: LocalRuntimeBinding
    runner: ProcessRegistration
    controller: Literal["local_runtime"] = "local_runtime"


class TemporaryTreeRegistration(BoundaryModel):
    kind: Literal["temporary_tree"] = "temporary_tree"
    resource_id: ResourceId
    parent_resource_id: ResourceId
    name: ResourceId
    device: Uint64
    inode: Uint64
    owner_uid: Uint32


class BorrowedServiceRegistration(BoundaryModel):
    kind: Literal["borrowed_service"] = "borrowed_service"
    service_reference: Identifier
    connection_scope: ResourceId


ResourceRegistration = Annotated[
    ProcessRegistration
    | LocalRuntimeRegistration
    | TemporaryTreeRegistration
    | BorrowedServiceRegistration,
    Field(discriminator="kind"),
]
