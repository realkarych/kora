from typing import Annotated, Literal

from pydantic import Field

from kora.protocols.common import BoundaryModel
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    Generation,
    InvocationId,
    LifetimeReason,
    OperationId,
    OriginalOutcome,
    ResourceId,
    ResourceKind,
    ResourceRegistration,
    ResourceState,
    Revision,
)
from kora.runner.filesystem import DirectoryIdentity


class JournalEntry(BoundaryModel):
    resource_id: ResourceId
    parent_resource_id: ResourceId | None
    kind: ResourceKind
    state: ResourceState
    revision: Revision
    acquisition_id: OperationId | None = None
    registration: ResourceRegistration | None = None
    tree_name: ResourceId | None = None
    reason: LifetimeReason = LifetimeReason.COMPLETE


class JournalSnapshot(BoundaryModel):
    format: Literal[1] = 1
    invocation_id: InvocationId
    owner: ProcessIdentity
    generation: Generation
    revision: Revision
    admission_open: bool
    original_outcome: OriginalOutcome
    root: DirectoryIdentity | None = None
    directory: DirectoryIdentity | None = None
    ancestors: Annotated[tuple[DirectoryIdentity, ...], Field(max_length=256)] = ()
    entries: Annotated[tuple[JournalEntry, ...], Field(max_length=256)] = ()
    pending_effects: Annotated[tuple[OperationId, ...], Field(max_length=16)] = ()
    helpers: Annotated[tuple[ProcessIdentity, ...], Field(max_length=16)] = ()
