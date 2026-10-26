from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Annotated, Final, Literal, Protocol, Self, cast

from pydantic import (
    AfterValidator,
    Discriminator,
    Field,
    PlainValidator,
    StrictBool,
    StrictInt,
    StringConstraints,
    Tag,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir import PrincipalRef, VcsRef
from kora.ir.config import ResourceName
from kora.ir.cost import Money, PricingStatus, Sum, UsageCoverage, consistent_price
from kora.ir.json import (
    JsonObject,
    JsonObjectField,
)
from kora.ir.scalars import ArtifactType, Digest, EnvName, SecretRef
from kora.protocols.common import BoundaryModel, Pending, budgeted_object
from kora.protocols.deadlines import ExecutionDeadline, is_duration, require
from kora.protocols.evidence import (
    DispatcherQuiescence,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.harness import SessionSpec
from kora.protocols.history import (
    GrantHistoryPage,
    GrantHistoryProgress,
    GrantHistorySnapshot,
    RecoveryPage,
    RecoveryProgress,
    RecoveryRead,
)
from kora.protocols.identity import (
    Count,
    GrantBinding,
    Handle,
    Identifier,
    Instant,
    ProcessIdentity,
)
from kora.protocols.secret_value import SecretTransfer
from kora.protocols.vcs import (
    MAX_COMPONENT_BYTES,
    MAX_MAP_ENTRIES,
    MAX_PATH_TEXT_BYTES,
    Argument,
    Binding,
    BindingRevision,
    CheckoutPlan,
    CredentialMode,
    PlanExecutor,
    PreparedPublication,
    PublishPlan,
    WorkspaceRef,
    WorkspaceTarget,
    fits,
    is_bool,
    is_executor_set,
    presence_matches_mode,
)

CAPABILITY_FLAGS: tuple[str, ...] = (
    "supports_resume",
    "portable_handles",
    "enforces_network_policy",
    "isolates_tool_secrets",
    "credential_proxy",
    "protected_execution",
)
MAX_TRANSFER_BYTES: Final[int] = 1_073_741_824
MAX_RESOURCE_BYTES: Final[int] = 262_144
MAX_RESOURCES: Final[int] = 64
MAX_EVENT_DATA_BYTES: Final[int] = 65_536
MIN_EXIT_CODE: Final[int] = -255
MAX_EXIT_CODE: Final[int] = 255
MAX_REFERENCE_BYTES: Final[int] = 1024
MAX_TYPE_NAME_LENGTH: Final[int] = 256

RuntimePhase = Literal[
    "preparation",
    "execution",
    "sealing",
    "inspection",
    "awaiting_authorization",
    "publication",
    "complete",
    "failed",
]


def runtime_refused() -> PydanticCustomError:
    return PydanticCustomError("runtime_value_invalid", "the runtime value is invalid")


def is_positive_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class RuntimeCapabilities:
    supports_resume: bool
    portable_handles: bool
    enforces_network_policy: bool
    isolates_tool_secrets: bool
    credential_proxy: bool
    protected_execution: bool
    plan_executors: frozenset[PlanExecutor]
    max_concurrent: int | None
    default_stall_after: timedelta

    def __post_init__(self) -> None:
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        for flag in CAPABILITY_FLAGS:
            require(is_bool(values[flag]), f"{flag} is a bool")
        require(
            is_executor_set(self.plan_executors),
            "plan_executors is a non-empty frozenset of runner and worker_host",
        )
        require(
            self.max_concurrent is None or is_positive_count(self.max_concurrent),
            "max_concurrent is None or a positive int",
        )
        require(
            is_duration(self.default_stall_after, timedelta(0), inclusive=False),
            "default_stall_after is a positive duration",
        )


def transfer_name(value: str) -> str:
    if (
        value in {".", ".."}
        or "/" in value
        or not value.isprintable()
        or not fits(value, MAX_COMPONENT_BYTES)
    ):
        raise runtime_refused()
    return value


def resource_name(value: str) -> str:
    if not fits(value, MAX_PATH_TEXT_BYTES) or any(
        not fits(part, MAX_COMPONENT_BYTES) for part in value.split("/")
    ):
        raise runtime_refused()
    return value


def event_data(value: object) -> JsonObjectField:
    parsed = budgeted_object(value, MAX_EVENT_DATA_BYTES)
    if parsed is None:
        raise runtime_refused()
    return parsed


TransferName = Annotated[
    str, StringConstraints(strict=True, min_length=1), AfterValidator(transfer_name)
]
BoundedResourceName = Annotated[ResourceName, AfterValidator(resource_name)]
EventData = Annotated[
    JsonObjectField, PlainValidator(event_data), WithJsonSchema({"type": "object"})
]
EnvMap = Annotated[dict[EnvName, Argument], Field(max_length=MAX_MAP_ENTRIES)]
BoundedSecretRef = Annotated[SecretRef, Field(max_length=MAX_REFERENCE_BYTES)]
SecretRefs = Annotated[
    dict[EnvName, BoundedSecretRef], Field(max_length=MAX_MAP_ENTRIES)
]


class TransferItem(BoundaryModel):
    name: TransferName
    size: Annotated[Count, Field(le=MAX_TRANSFER_BYTES)]
    sha256: Digest
    kind: Literal["resource", "input", "script"]


class ArtifactRef(BoundaryModel):
    artifact_version_id: Identifier
    payload_digest: Digest
    type_name: Annotated[ArtifactType, Field(max_length=MAX_TYPE_NAME_LENGTH)]


class ResourceRef(BoundaryModel):
    name: BoundedResourceName
    sha256: Digest
    size: Annotated[Count, Field(le=MAX_RESOURCE_BYTES)]
    executable: StrictBool
    storage_ref: Identifier


def checkout_kind(value: object) -> object:
    if isinstance(value, Mapping):
        keys = cast("Mapping[object, object]", value).keys()
        plan, workspace = "steps" in keys, "commit" in keys
    else:
        plan = isinstance(value, CheckoutPlan)
        workspace = isinstance(value, WorkspaceRef)
    if plan == workspace:
        return None
    return "plan" if plan else "workspace"


Checkout = Annotated[
    Annotated[CheckoutPlan, Tag("plan")] | Annotated[WorkspaceRef, Tag("workspace")],
    Discriminator(
        checkout_kind,
        custom_error_type="runtime_value_invalid",
        custom_error_message="the runtime value is invalid",
    ),
]


class Workload(BoundaryModel):
    attempt_id: Identifier
    execution_domain_id: Identifier
    dispatcher_id: Identifier
    execution_request: JsonObjectField
    execution_request_digest: Digest
    kind: Literal["agent", "command"]
    resolved_config: JsonObjectField
    workspace: WorkspaceTarget
    checkout: Checkout
    publish: PublishPlan | None
    plan_executor: PlanExecutor
    env: EnvMap
    secret_refs: SecretRefs
    harness_credentials: SecretRefs
    artifact_inputs: Annotated[
        dict[Identifier, ArtifactRef],
        Field(max_length=MAX_MAP_ENTRIES),
        Pending(issue=106),
    ]
    resources: Annotated[tuple[ResourceRef, ...], Field(max_length=MAX_RESOURCES)]
    limits: JsonObjectField
    deadline: ExecutionDeadline | None
    session_spec: SessionSpec | None
    dispatcher_process: ProcessIdentity | None


class RuntimeStatus(BoundaryModel):
    state: Literal["queued", "starting", "running", "exited", "lost"]
    exit_code: Annotated[StrictInt, Field(ge=MIN_EXIT_CODE, le=MAX_EXIT_CODE)] | None
    phase: RuntimePhase
    observed_at: Instant

    @model_validator(mode="after")
    def exit_code_needs_exit(self) -> Self:
        if self.exit_code is not None and self.state != "exited":
            raise runtime_refused()
        return self


class AdoptedSession(BoundaryModel):
    handle: Handle
    phase: RuntimePhase
    prepared: PreparedPublication | None
    journal_next: Count
    observational: StrictBool


class RawEvent(BoundaryModel):
    i: Count
    at: Instant
    kind: Identifier
    data: EventData


class Liveness(BoundaryModel):
    observed_at: Instant
    supervisor_alive: StrictBool
    candidate_alive: StrictBool
    journal_next: Count
    last_event_at: Instant | None
    limited_events: Sum
    events: Sum
    input_tokens: Sum
    output_tokens: Sum
    usd: Money | None
    known_usd: Money | None
    pricing_status: PricingStatus
    usage_coverage: UsageCoverage
    last_event_kind: Identifier | None

    @model_validator(mode="after")
    def price_is_consistent(self) -> Self:
        consistent_price(self.pricing_status, self.usd, self.known_usd)
        return self


class PublicationAuthorization(BoundaryModel):
    attempt_id: Identifier
    execution_domain_id: Identifier
    dispatcher_id: Identifier
    operation_id: Identifier
    prepared_digest: Digest
    destination_digest: Digest
    binding: Binding
    binding_revision: BindingRevision
    credential_mode: CredentialMode
    principal: PrincipalRef
    credential: SecretTransfer | None

    @model_validator(mode="after")
    def credential_matches_mode(self) -> Self:
        if not presence_matches_mode(
            self.credential_mode, self.credential, self.principal
        ):
            raise runtime_refused()
        return self


class Runtime(Protocol):
    name: str
    config_schema: JsonObject
    execution_domain_id: str

    def capabilities(self) -> RuntimeCapabilities: ...

    async def workspace_target(self, attempt_id: str) -> WorkspaceTarget: ...

    async def submit(self, workload: Workload) -> Handle: ...

    async def transfer_in(
        self, handle: Handle, item: TransferItem, chunks: AsyncIterator[bytes]
    ) -> None: ...

    async def start(self, handle: Handle) -> None: ...

    async def lookup(self, attempt_id: str) -> Handle | None: ...

    async def adopt(self, handle: Handle) -> AdoptedSession: ...

    async def status(self, handle: Handle) -> RuntimeStatus: ...

    def events(self, handle: Handle, from_offset: int) -> AsyncIterator[RawEvent]: ...

    async def heartbeat(self, handle: Handle) -> Liveness: ...

    def transfer_out(self, handle: Handle, name: str) -> AsyncIterator[bytes]: ...

    def read_log(
        self,
        handle: Handle,
        which: Literal["stdout", "stderr"],
        from_offset: int,
    ) -> AsyncIterator[bytes]: ...

    def read_transcript(self, handle: Handle) -> AsyncIterator[bytes]: ...

    async def finalize(
        self, handle: Handle, authorization: PublicationAuthorization
    ) -> VcsRef: ...

    async def cancel(self, handle: Handle, grace: timedelta) -> None: ...

    async def grant_history_status(
        self,
        attempt_id: str,
        current_grant: GrantBinding,
        snapshot: GrantHistorySnapshot,
    ) -> GrantHistoryProgress: ...

    async def accept_grant_history(
        self, attempt_id: str, current_grant: GrantBinding, page: GrantHistoryPage
    ) -> GrantHistoryProgress: ...

    async def recovery(
        self, attempt_id: str, current_grant: GrantBinding, request: RecoveryRead
    ) -> RecoveryProgress | RecoveryPage: ...

    async def quiescence(self, handle: Handle) -> NormalCompletionQuiescence: ...

    async def record_dispatcher_quiescence(
        self,
        attempt_id: str,
        acknowledgement: DispatcherQuiescence,
        grant: GrantBinding,
    ) -> None: ...

    async def retire(self, attempt_id: str, grace: timedelta) -> TerminationReceipt: ...

    async def release(self, handle: Handle) -> None: ...
