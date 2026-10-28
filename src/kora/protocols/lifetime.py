import re
import secrets
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity import Count, Identifier, ProcessIdentity
from kora.protocols.lifetime_records import (
    BorrowedServiceRegistration as BorrowedServiceRegistration,
)
from kora.protocols.lifetime_records import (
    Generation as Generation,
)
from kora.protocols.lifetime_records import (
    InvocationId as InvocationId,
)
from kora.protocols.lifetime_records import (
    LifetimeReason as LifetimeReason,
)
from kora.protocols.lifetime_records import (
    LocalRuntimeBinding as LocalRuntimeBinding,
)
from kora.protocols.lifetime_records import (
    LocalRuntimeRegistration as LocalRuntimeRegistration,
)
from kora.protocols.lifetime_records import (
    OperationId as OperationId,
)
from kora.protocols.lifetime_records import (
    OriginalOutcome as OriginalOutcome,
)
from kora.protocols.lifetime_records import (
    ProcessRegistration as ProcessRegistration,
)
from kora.protocols.lifetime_records import (
    ResourceId as ResourceId,
)
from kora.protocols.lifetime_records import (
    ResourceKind as ResourceKind,
)
from kora.protocols.lifetime_records import (
    ResourceRegistration as ResourceRegistration,
)
from kora.protocols.lifetime_records import (
    ResourceState as ResourceState,
)
from kora.protocols.lifetime_records import (
    Revision as Revision,
)
from kora.protocols.lifetime_records import (
    TemporaryTreeRegistration as TemporaryTreeRegistration,
)
from kora.protocols.lifetime_records import (
    Uint32 as Uint32,
)
from kora.protocols.lifetime_records import (
    Uint64 as Uint64,
)

DeadlineSeconds = Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)]
ExecutionString = Annotated[
    StrictStr,
    Field(max_length=65536, pattern=re.compile(r"^[^\x00]*(?![\s\S])")),
]
Executable = Annotated[
    StrictStr,
    Field(min_length=1, max_length=4096, pattern=re.compile(r"^[^\x00]*(?![\s\S])")),
]
EnvironmentName = Annotated[
    StrictStr,
    Field(min_length=1, max_length=4096, pattern=re.compile(r"^[^\x00=]*(?![\s\S])")),
]


def require_code_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("closed codes require strings")
    return value


def new_operation_id() -> OperationId:
    return secrets.token_hex(16)


class ProcessDomain(StrEnum):
    INVOCATION = "invocation"
    DETACHED = "detached"


class ProcessOutput(StrEnum):
    CAPTURE = "capture"


class ActivationResult(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"


class CleanupOutcome(StrEnum):
    CLOSED = "closed"
    UNKNOWN = "unknown"


class LifetimeLimits(BoundaryModel):
    max_resource_records: Annotated[StrictInt, Field(gt=0, le=256)] = 256
    max_outstanding_operations: Annotated[StrictInt, Field(gt=0, le=16)] = 16
    max_control_frame_bytes: Annotated[StrictInt, Field(gt=0, le=65536)] = 65536
    max_control_recovery_bytes: Annotated[StrictInt, Field(gt=0, le=1048576)] = 1048576
    max_execution_snapshot_bytes: Annotated[StrictInt, Field(gt=0, le=1048576)] = (
        1048576
    )
    max_unresolved_samples: Annotated[StrictInt, Field(gt=0, le=32)] = 32
    max_state_root_invocations: Annotated[StrictInt, Field(gt=0, le=32)] = 32
    acquisition_seconds: DeadlineSeconds = 30.0
    cooperative_seconds: DeadlineSeconds = 5.0
    term_seconds: DeadlineSeconds = 2.0
    kill_observation_seconds: DeadlineSeconds = 5.0
    drain_seconds: DeadlineSeconds = 2.0


class ReservationToken(BoundaryModel):
    invocation_id: InvocationId
    resource_id: ResourceId
    owner: ProcessIdentity
    generation: Generation


class ReservationRequest(BoundaryModel):
    operation_id: OperationId
    kind: Annotated[ResourceKind, BeforeValidator(require_code_string)]
    parent_resource_id: ResourceId | None = None
    runtime_binding: LocalRuntimeBinding | None = None

    @model_validator(mode="after")
    def validate_runtime_binding(self) -> Self:
        if (self.kind == ResourceKind.LOCAL_RUNTIME_DOMAIN) != (
            self.runtime_binding is not None
        ):
            raise ValueError("runtime_binding is required exactly for runtime domains")
        return self


class Reservation(BoundaryModel):
    token: ReservationToken
    kind: Annotated[ResourceKind, BeforeValidator(require_code_string)]
    revision: Revision
    state: Annotated[ResourceState, BeforeValidator(require_code_string)]


class EnvironmentVariable(BoundaryModel):
    name: EnvironmentName
    value: ExecutionString


class ProcessLaunch(BoundaryModel):
    kind: Literal["process_domain"] = "process_domain"
    executable: Executable
    argv: Annotated[tuple[ExecutionString, ...], Field(strict=True, max_length=4096)]
    environment: Annotated[
        tuple[EnvironmentVariable, ...], Field(strict=True, max_length=4096)
    ]
    working_directory_resource_id: ResourceId
    stdin: None
    stdout: Annotated[ProcessOutput, BeforeValidator(require_code_string)] | None
    stderr: Annotated[ProcessOutput, BeforeValidator(require_code_string)] | None
    domain: Annotated[ProcessDomain, BeforeValidator(require_code_string)]

    @model_validator(mode="after")
    def validate_execution_snapshot(self) -> Self:
        names = tuple(variable.name for variable in self.environment)
        if len(names) != len(set(names)):
            raise ValueError("duplicate environment names are refused")
        strings = (
            self.executable,
            self.working_directory_resource_id,
            *self.argv,
            *(
                item
                for variable in self.environment
                for item in (variable.name, variable.value)
            ),
        )
        try:
            snapshot_bytes = sum(len(value.encode("utf-8")) for value in strings)
        except UnicodeEncodeError as error:
            raise ValueError(
                "execution snapshot strings require valid UTF-8"
            ) from error
        if snapshot_bytes > 1048576:
            raise ValueError("execution snapshot exceeds one MiB")
        return self


class LocalRuntimeLaunch(BoundaryModel):
    kind: Literal["local_runtime_domain"] = "local_runtime_domain"
    binding: LocalRuntimeBinding
    launch: ProcessLaunch


class TemporaryTreeRequest(BoundaryModel):
    kind: Literal["temporary_tree"] = "temporary_tree"
    parent_resource_id: ResourceId | None = None


class BorrowedServiceRequest(BoundaryModel):
    kind: Literal["borrowed_service"] = "borrowed_service"
    service_reference: Identifier
    pre_existing: StrictBool

    @model_validator(mode="after")
    def validate_pre_existing(self) -> Self:
        if not self.pre_existing:
            raise ValueError("pre_existing must be True")
        return self


class Acquisition(BoundaryModel):
    token: ReservationToken
    operation_id: OperationId
    revision: Revision
    registration: ResourceRegistration

    @model_validator(mode="after")
    def validate_registration_binding(self) -> Self:
        process = None
        if isinstance(self.registration, ProcessRegistration):
            process = self.registration
        elif isinstance(self.registration, LocalRuntimeRegistration):
            process = self.registration.runner
        elif (
            isinstance(self.registration, TemporaryTreeRegistration)
            and self.registration.resource_id != self.token.resource_id
        ):
            raise ValueError("tree registration must match the token resource")
        if (
            process is not None
            and process.acquisition_operation_id != self.operation_id
        ):
            raise ValueError(
                "process registration must match the acquisition operation"
            )
        return self


class ActivationAcknowledgement(BoundaryModel):
    token: ReservationToken
    acquisition_operation_id: OperationId
    revision: Revision
    result: Annotated[ActivationResult, BeforeValidator(require_code_string)]


class ResourceObservation(BoundaryModel):
    resource_id: ResourceId
    kind: Annotated[ResourceKind, BeforeValidator(require_code_string)]
    revision: Revision
    state: Annotated[ResourceState, BeforeValidator(require_code_string)]
    pending_acquisition_count: Count
    live_owned_process_count: Count
    remaining_pipe_count: Count
    remaining_tree_count: Count
    reason: Annotated[LifetimeReason, BeforeValidator(require_code_string)]


class CleanupReference(BoundaryModel):
    invocation_id: InvocationId


class ClosureReport(BoundaryModel):
    invocation_id: InvocationId
    resource_id: ResourceId | None
    revision: Revision
    state: Annotated[CleanupOutcome, BeforeValidator(require_code_string)]
    original_outcome: Annotated[OriginalOutcome, BeforeValidator(require_code_string)]
    cleanup_outcome: Annotated[CleanupOutcome, BeforeValidator(require_code_string)]
    unresolved_count: Count
    samples: Annotated[
        tuple[ResourceObservation, ...], Field(strict=True, max_length=32)
    ]
    cleanup_reference: CleanupReference | None

    @model_validator(mode="after")
    def validate_cleanup_evidence(self) -> Self:
        if self.state != self.cleanup_outcome:
            raise ValueError("closure state must match cleanup outcome")
        if len(self.samples) > self.unresolved_count:
            raise ValueError("samples cannot exceed the unresolved total")
        if self.state == CleanupOutcome.CLOSED:
            if (
                self.unresolved_count
                or self.samples
                or self.cleanup_reference is not None
            ):
                raise ValueError("closed reports cannot retain unresolved evidence")
        elif (
            self.cleanup_reference is None
            or self.cleanup_reference.invocation_id != self.invocation_id
        ):
            raise ValueError("unknown cleanup requires this invocation's reference")
        return self


class ConformanceLifetime(Protocol):
    async def reserve(self, request: ReservationRequest) -> Reservation: ...

    async def acquire(
        self,
        token: ReservationToken,
        operation_id: OperationId,
        request: ProcessLaunch
        | LocalRuntimeLaunch
        | TemporaryTreeRequest
        | BorrowedServiceRequest,
    ) -> Acquisition: ...

    async def acknowledge(
        self,
        token: ReservationToken,
        acquisition_operation_id: OperationId,
        revision: int,
    ) -> ActivationAcknowledgement: ...

    async def close(self, resource_id: ResourceId | None) -> ClosureReport: ...

    async def observe(
        self, resource_id: ResourceId | None
    ) -> tuple[ResourceObservation, ...]: ...


class LocalLaunchAuthority(Protocol):
    async def reserve(self, request: ReservationRequest) -> Reservation: ...

    async def acquire(
        self,
        token: ReservationToken,
        operation_id: OperationId,
        request: LocalRuntimeLaunch,
    ) -> Acquisition: ...

    async def acknowledge(
        self,
        token: ReservationToken,
        acquisition_operation_id: OperationId,
        revision: int,
    ) -> ActivationAcknowledgement: ...

    async def close(self, resource_id: ResourceId | None) -> ClosureReport: ...

    async def observe(
        self, resource_id: ResourceId | None
    ) -> tuple[ResourceObservation, ...]: ...
