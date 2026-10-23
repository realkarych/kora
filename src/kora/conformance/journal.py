from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from kora.protocols.common import BoundaryModel
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    BorrowedServiceRegistration,
    Generation,
    InvocationId,
    LifetimeReason,
    OperationId,
    OriginalOutcome,
    ProcessRegistration,
    ResourceId,
    ResourceKind,
    ResourceRegistration,
    ResourceState,
    Revision,
    TemporaryTreeRegistration,
)
from kora.runner.filesystem import DirectoryIdentity, FileIdentity


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
    admission_open: StrictBool
    original_outcome: OriginalOutcome
    claim: FileIdentity | None = None
    root: DirectoryIdentity | None = None
    directory: DirectoryIdentity | None = None
    ancestors: Annotated[tuple[DirectoryIdentity, ...], Field(max_length=256)] = ()
    entries: Annotated[tuple[JournalEntry, ...], Field(max_length=256)] = ()
    pending_effects: Annotated[tuple[OperationId, ...], Field(max_length=16)] = ()
    helpers: Annotated[tuple[ProcessIdentity, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def coherent_resources(self) -> Self:
        seen: dict[ResourceId, JournalEntry] = {}
        operations: set[OperationId] = set()
        processes: set[ProcessIdentity] = set()
        for entry in self.entries:
            if (
                entry.resource_id == self.invocation_id
                or entry.resource_id in seen
                or entry.revision > self.revision
                or (
                    entry.parent_resource_id is not None
                    and entry.parent_resource_id not in seen
                )
            ):
                raise ValueError("journal resource graph is incoherent")
            if entry.acquisition_id is not None:
                if entry.acquisition_id in operations:
                    raise ValueError("journal acquisition identity is duplicated")
                operations.add(entry.acquisition_id)
            registration = entry.registration
            if registration is None:
                if entry.state in (ResourceState.GATED, ResourceState.ACTIVE):
                    raise ValueError("journal activated resource is unregistered")
            elif (
                registration.kind != entry.kind.value
                or entry.acquisition_id is None
                or entry.state is ResourceState.RESERVED
            ):
                raise ValueError("journal resource registration is incoherent")
            if isinstance(registration, ProcessRegistration):
                if (
                    registration.acquisition_operation_id != entry.acquisition_id
                    or registration.process in processes
                    or not registration.process.leads_group
                    or registration.process_group_id != registration.process.pid
                ):
                    raise ValueError("journal process registration is incoherent")
                processes.add(registration.process)
            if isinstance(registration, TemporaryTreeRegistration):
                if (
                    registration.resource_id != entry.resource_id
                    or registration.name != entry.resource_id
                    or entry.tree_name != entry.resource_id
                    or registration.parent_resource_id
                    != (entry.parent_resource_id or self.invocation_id)
                    or (
                        entry.parent_resource_id is not None
                        and seen[entry.parent_resource_id].kind
                        is not ResourceKind.TEMPORARY_TREE
                    )
                ):
                    raise ValueError("journal tree registration is incoherent")
            if isinstance(registration, BorrowedServiceRegistration) and (
                registration.connection_scope != entry.parent_resource_id
                or registration.connection_scope not in seen
                or seen[registration.connection_scope].kind
                is not ResourceKind.PROCESS_DOMAIN
            ):
                raise ValueError("journal connection scope is incoherent")
            if entry.tree_name is not None and (
                entry.kind is not ResourceKind.TEMPORARY_TREE
                or entry.tree_name != entry.resource_id
            ):
                raise ValueError("journal tree intent is incoherent")
            seen[entry.resource_id] = entry
        expected = tuple(
            entry.acquisition_id
            for entry in self.entries
            if entry.acquisition_id is not None
            and entry.registration is None
            and entry.state is not ResourceState.CLOSED
        )
        if self.pending_effects != expected:
            raise ValueError("journal pending effects are incoherent")
        return self
