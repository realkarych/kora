import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol, Self, cast, get_args

from pydantic import (
    AfterValidator,
    ConfigDict,
    Discriminator,
    Field,
    PlainValidator,
    StrictBool,
    StrictInt,
    StringConstraints,
    Tag,
    TypeAdapter,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir import ConfigWarning, ErrorClass
from kora.ir.cost import (
    Basis,
    BillingClass,
    KnownFields,
    Money,
    Sum,
    Timestamp,
    Token,
    TtlSplit,
    UsageCoverage,
    UsageRow,
    tokens_coherent,
)
from kora.ir.cost import Identifier as CostIdentifier
from kora.ir.json import (
    DEFAULT_MAX_DEPTH,
    MAX_CANONICAL_BYTES,
    CanonicalBudget,
    JsonObject,
    JsonObjectField,
    JsonShapeError,
    JsonValue,
    parse_value,
    spend_string,
)
from kora.ir.scalars import Digest
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import ERROR_CODE
from kora.protocols.identity import Count, Identifier
from kora.protocols.policy import RULE_ID, PolicyClient, PolicyReason
from kora.protocols.vcs import WorkspaceTarget

MAX_TEXT_BYTES: Final[int] = 61_440
MAX_PROMPT_BYTES: Final[int] = 1_048_576
MAX_FILE_NAME_BYTES: Final[int] = 255
MAX_SAMPLE_ITEMS: Final[int] = 1000
MAX_USAGE_ROWS: Final[int] = 64
MAX_MCP_SOURCES: Final[int] = 64
MAX_SESSION_FILES: Final[int] = 64


def refused() -> PydanticCustomError:
    return PydanticCustomError("harness_value_invalid", "the harness value is invalid")


def bounded_text(value: str) -> str:
    try:
        spend_string(CanonicalBudget(MAX_TEXT_BYTES), value, "text", 0)
    except JsonShapeError:
        raise refused() from None
    return value


def bounded_prompt(value: str) -> str:
    if len(value) > MAX_PROMPT_BYTES or not text_fits(value, MAX_PROMPT_BYTES):
        raise refused()
    return value


def text_fits(value: str, limit: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def bounded_value(value: object) -> JsonValue:
    budget = CanonicalBudget(MAX_CANONICAL_BYTES)
    try:
        parsed = parse_value(value, DEFAULT_MAX_DEPTH, 0, budget)
    except JsonShapeError:
        raise refused() from None
    return parsed


def session_file_name(value: str) -> str:
    if (
        value == "."
        or value != value.strip()
        or "/" in value
        or ".." in value
        or not value.isprintable()
        or not text_fits(value, MAX_FILE_NAME_BYTES)
    ):
        raise refused()
    return value


Code = Annotated[str, StringConstraints(strict=True, pattern=f"^{ERROR_CODE.pattern}$")]
RuleId = Annotated[str, StringConstraints(strict=True, pattern=f"^{RULE_ID.pattern}$")]
BoundedText = Annotated[
    str, StringConstraints(strict=True), AfterValidator(bounded_text)
]
Prompt = Annotated[str, StringConstraints(strict=True), AfterValidator(bounded_prompt)]
SessionFileName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_FILE_NAME_BYTES),
    AfterValidator(session_file_name),
]
HttpStatus = Annotated[StrictInt, Field(ge=100, le=599)]
BoundedJsonValue = Annotated[object, PlainValidator(bounded_value), WithJsonSchema({})]

Placement = Literal["harness_home", "harness_config", "harness_tmp", "harness_input"]
RunnerCause = Literal[
    "completed",
    "max_events",
    "budget",
    "stall",
    "deadline",
    "cancel",
    "provider_limit",
    "policy_bypass",
    "version_mismatch",
    "plugin_mismatch",
    "integrity_mismatch",
    "config_check_failed",
    "policy_error",
    "policy_timeout",
    "journal_unavailable",
    "cli_exit_without_result",
    "oversized_message",
    "launch_rejected",
    "launch_failed",
    "launcher_died",
]
HarnessDenialCode = Literal["harness_rule", "policy_error", "policy_timeout"]
HARNESS_DENIAL_CODES: Final[frozenset[str]] = frozenset(get_args(HarnessDenialCode))
CallKind = Literal["shell", "file_edit", "mcp_tool", "subagent", "hosted_tool"]


class HarnessValueRefused(ValueError):
    def __init__(self, field_name: str) -> None:
        super().__init__(f"the harness value {field_name} is invalid")


@dataclass(frozen=True)
class HarnessCapabilities:
    resume: bool
    reports_usage_per_turn: bool
    policy_coverage: frozenset[CallKind]


class SessionFile(BoundaryModel):
    name: SessionFileName
    digest: Digest
    placement: Literal["mcp_config", "skill"]
    storage_ref: Identifier | None


@dataclass(frozen=True)
class LaunchInputs:
    environment: Mapping[str, str]
    cwd: Path


@dataclass(frozen=True)
class LaunchEnd:
    cause: RunnerCause | None
    exit_code: int | None
    signal: int | None


class Launch(Protocol):
    cli_path: Path

    async def kill(self) -> None: ...

    async def ended(self) -> LaunchEnd: ...


class LaunchContext(Protocol):
    placements: Mapping[Placement, Path]

    async def allocate(self, inputs: LaunchInputs) -> Launch: ...


class SessionSpec(BoundaryModel):
    harness: Identifier
    adapter_version: Identifier
    options: JsonObjectField
    files: tuple[SessionFile, ...] = Field(max_length=MAX_SESSION_FILES)
    prompt: Prompt
    resume_handle: Identifier | None
    price_table_id: Identifier
    price_table: JsonObjectField

    @model_validator(mode="after")
    def distinct_file_names(self) -> Self:
        names = {folded_name(item.name) for item in self.files}
        if len(names) != len(self.files):
            raise refused()
        return self


def folded_name(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())


class SessionStartedData(BoundaryModel):
    session_id: Identifier
    harness_version: Identifier
    billing_class: BillingClass
    plugins_digest: Digest
    mcp_sources: tuple[Identifier, ...] = Field(max_length=MAX_MCP_SOURCES)


class TurnData(BoundaryModel):
    message_id: Identifier
    agent_id: Identifier | None = None
    text: BoundedText | None = None
    truncated: StrictBool


class ToolCallData(BoundaryModel):
    tool: Identifier
    tool_use_id: Identifier
    agent_id: Identifier | None = None
    output_channel: StrictBool


class ToolResultData(BoundaryModel):
    tool_use_id: Identifier
    is_error: StrictBool
    content: BoundedText | None = None
    truncated: StrictBool


class PermissionDeniedData(BoundaryModel):
    tool: Identifier
    tool_use_id: Identifier
    source: Literal["harness", "policy"]
    pattern: RuleId | None = None
    code: PolicyReason | HarnessDenialCode

    @model_validator(mode="after")
    def code_matches_source(self) -> Self:
        if self.source == "harness":
            coherent = self.code in HARNESS_DENIAL_CODES and self.pattern is None
        else:
            coherent = self.code not in HARNESS_DENIAL_CODES | {"allowed"} and (
                self.pattern is None
                or (self.code == "denied" and ".deny." in self.pattern)
            )
        if not coherent:
            raise refused()
        return self


class UsageData(BoundaryModel):
    observation_id: CostIdentifier
    observation_sequence: Sum
    observed_at: Timestamp
    message_id: Identifier
    agent_id: Identifier | None = None
    model: CostIdentifier | None = None
    input_tokens: Token
    cached_input_tokens: Token
    cache_creation_tokens: Token
    output_tokens: Token
    reasoning_tokens: Token
    known_token_fields: KnownFields
    cache_creation_by_ttl: TtlSplit | None = None
    reported_usd: Money | None = None
    basis: Basis
    billing_class: BillingClass
    coverage: UsageCoverage

    @model_validator(mode="after")
    def coherent_tokens(self) -> Self:
        if not tokens_coherent(self):
            raise refused()
        return self


class ProviderLimitedData(BoundaryModel):
    retry_after: Timestamp | None
    scope: Literal["rate", "quota"]
    vendor_group: Identifier | None
    model: CostIdentifier
    binding: Identifier


class ProviderRetryingData(BoundaryModel):
    model: CostIdentifier
    retry_number: Token
    retry_after: Timestamp | None = None


class ResultData(BoundaryModel):
    subtype: Code
    is_error: StrictBool
    terminal_reason: Code | None


class ErrorData(BoundaryModel):
    code: Code
    text: BoundedText | None = None
    truncated: StrictBool


class EventFields(BoundaryModel):
    i: Sum
    at: Timestamp


class SessionStartedEvent(EventFields):
    kind: Literal["session_started"]
    data: SessionStartedData


class TurnEvent(EventFields):
    kind: Literal["turn"]
    data: TurnData


class ToolCallEvent(EventFields):
    kind: Literal["tool_call"]
    data: ToolCallData


class ToolResultEvent(EventFields):
    kind: Literal["tool_result"]
    data: ToolResultData


class PermissionDeniedEvent(EventFields):
    kind: Literal["permission_denied"]
    data: PermissionDeniedData


class UsageEvent(EventFields):
    kind: Literal["usage"]
    data: UsageData


class ProviderLimitedEvent(EventFields):
    kind: Literal["provider_limited"]
    data: ProviderLimitedData


class ProviderRetryingEvent(EventFields):
    kind: Literal["provider_retrying"]
    data: ProviderRetryingData


class ResultEvent(EventFields):
    kind: Literal["result"]
    data: ResultData


class ErrorEvent(EventFields):
    kind: Literal["error"]
    data: ErrorData


def event_kind(value: object) -> object:
    if isinstance(value, Mapping):
        return cast("Mapping[object, object]", value).get("kind")
    return getattr(value, "kind", None)


NormalisedEvent = Annotated[
    Annotated[SessionStartedEvent, Tag("session_started")]
    | Annotated[TurnEvent, Tag("turn")]
    | Annotated[ToolCallEvent, Tag("tool_call")]
    | Annotated[ToolResultEvent, Tag("tool_result")]
    | Annotated[PermissionDeniedEvent, Tag("permission_denied")]
    | Annotated[UsageEvent, Tag("usage")]
    | Annotated[ProviderLimitedEvent, Tag("provider_limited")]
    | Annotated[ProviderRetryingEvent, Tag("provider_retrying")]
    | Annotated[ResultEvent, Tag("result")]
    | Annotated[ErrorEvent, Tag("error")],
    Discriminator(
        event_kind,
        custom_error_type="harness_value_invalid",
        custom_error_message="the harness value is invalid",
    ),
]
EVENT_ADAPTER: Final[TypeAdapter[NormalisedEvent]] = TypeAdapter(
    NormalisedEvent, config=ConfigDict(hide_input_in_errors=True)
)


@dataclass(frozen=True)
class Enforcement:
    action: Literal["continue", "terminate"]
    cause: RunnerCause | None

    def __post_init__(self) -> None:
        if (self.action == "terminate") != (self.cause is not None):
            raise HarnessValueRefused("cause")


class EventSink(Protocol):
    def emit(self, kind: str, data: JsonObject) -> None: ...

    def observe_dropped(self, kind: str) -> None: ...

    def enforcement(self) -> Enforcement: ...

    async def termination(self) -> RunnerCause: ...


@dataclass(frozen=True)
class RunnerOutcome:
    cause: RunnerCause
    termination: Literal[
        "none", "interrupted", "killed", "killed_after_grace", "teardown_after_result"
    ]
    exit_code: int | None
    signal: int | None


class OutputCandidate(BoundaryModel):
    source: Literal["structured_output", "result_text"]
    value: BoundedJsonValue


class ErrorSummary(BoundaryModel):
    source: Code
    error_count: Token
    contract_digest: Digest
    code: Code


class DenialSample(BoundaryModel):
    items: tuple[PermissionDeniedData, ...] = Field(max_length=MAX_SAMPLE_ITEMS)
    total: Count

    @model_validator(mode="after")
    def total_covers_items(self) -> Self:
        if self.total < len(self.items):
            raise refused()
        return self


class UnconfirmedCall(BoundaryModel):
    tool: Identifier
    tool_use_id: Identifier


class UnconfirmedSample(BoundaryModel):
    items: tuple[UnconfirmedCall, ...] = Field(max_length=MAX_SAMPLE_ITEMS)
    total: Count

    @model_validator(mode="after")
    def total_covers_items(self) -> Self:
        if self.total < len(self.items):
            raise refused()
        return self


class EventSummary(Protocol):
    pass


class SessionResult(BoundaryModel):
    harness: Identifier
    session_id: Identifier | None
    installed_version: Identifier | None
    billing_class: BillingClass | None
    stop_reason: Code
    terminal_reason: Code | None
    api_error_status: HttpStatus | None
    error_class: ErrorClass | None
    error_summary: ErrorSummary | None
    output: OutputCandidate | None
    output_diagnostic: Code | None
    usage: tuple[UsageRow, ...] = Field(max_length=MAX_USAGE_ROWS)
    usage_coverage: UsageCoverage
    turns: Token
    denials: DenialSample
    denials_complete: StrictBool
    unconfirmed_calls: UnconfirmedSample
    policy_bypass: StrictBool

    @model_validator(mode="after")
    def one_row_per_model(self) -> Self:
        if len({row.model for row in self.usage}) != len(self.usage):
            raise refused()
        return self


class HarnessAdapter(Protocol):
    name: str
    config_schema: JsonObject
    egress_hosts: tuple[str, ...]
    tested_versions: str
    reserved_env: tuple[str, ...]

    def capabilities(self) -> HarnessCapabilities: ...

    def check_config(self, config: JsonObject) -> list[ConfigWarning]: ...

    def build_session_spec(
        self, config: JsonObject, workspace: WorkspaceTarget, feedback: None
    ) -> SessionSpec: ...

    async def run(
        self,
        spec: SessionSpec,
        policy: PolicyClient,
        sink: EventSink,
        launches: LaunchContext,
    ) -> SessionResult: ...

    def finalise(
        self, summary: EventSummary, outcome: RunnerOutcome
    ) -> SessionResult: ...

    def installed_version(self) -> str: ...
