from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Annotated, Final, Literal, Protocol, Self, cast

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictBytes,
    StrictInt,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from kora.ir import PrincipalRef, VcsRef
from kora.ir.json import JsonObject
from kora.ir.scalars import Digest, EnvName
from kora.ir.vcs_ref import MAX_PATH_BYTES, MAX_PATHS, ChangedPath
from kora.ir.vcs_ref_json import MAX_JSON_BYTES, canonical_size
from kora.protocols.common import BoundaryModel, ProtocolContext, is_identifier
from kora.protocols.deadlines import require
from kora.protocols.identity import Count, Identifier, Instant, object_size
from kora.protocols.secret_value import SecretTransfer, SecretValue

PlanExecutor = Literal["runner", "worker_host"]
CredentialMode = Literal["credential_required", "local_credential_free"]
Capture = Literal[
    "commit", "branch", "changed_paths", "diff", "diff_stat", "inspection"
]
StepFailureClass = Literal["artifact_invalid", "config_error", "infrastructure"]

PLAN_EXECUTORS: frozenset[str] = frozenset({"runner", "worker_host"})
MAX_FILE_CONTENT_BYTES: Final[int] = 262_144
MAX_STDIN_BYTES: Final[int] = 65_536
MAX_PLAN_CONTENT_BYTES: Final[int] = 524_288
MAX_PLAN_FILES: Final[int] = 64
MAX_PLAN_STEPS: Final[int] = 64
MAX_ARGV_ITEMS: Final[int] = 256
MAX_ARGUMENT_BYTES: Final[int] = 4096
MAX_ARGV_BYTES: Final[int] = 65_536
MAX_NAMED_ITEMS: Final[int] = 64
MAX_MAP_ENTRIES: Final[int] = 256
MAX_STEP_TIMEOUT: Final[timedelta] = timedelta(hours=1)
MAX_PATH_TEXT_BYTES: Final[int] = 4096
MAX_COMPONENT_BYTES: Final[int] = 255
MAX_REPO_BYTES: Final[int] = 4096
MAX_BRANCH_BYTES: Final[int] = 1024
MAX_REVISION_BYTES: Final[int] = 1024
MAX_DIFF_STAT_BYTES: Final[int] = 4096
MAX_COMMIT_MESSAGE_BYTES: Final[int] = 65_536
MAX_BINDING_BYTES: Final[int] = 256
MAX_BINDING_REVISION: Final[int] = 2**53 - 1
MAX_FILE_MODE: Final[int] = 0o777
WORST_IDENTITY_BYTES: Final[int] = 255 * 4 + 2
WORST_PRINCIPAL_BYTES: Final[int] = object_size(
    {
        "provider_instance": WORST_IDENTITY_BYTES,
        "stable_id": WORST_IDENTITY_BYTES,
        "kind": len('"local_service"'),
        "display_name": WORST_IDENTITY_BYTES,
    }
)
PUSHED_BY_MEMBER_BYTES: Final[int] = len(',"pushed_by":')


def vcs_refused() -> PydanticCustomError:
    return PydanticCustomError("vcs_value_invalid", "the VCS value is invalid")


def utf8_size(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def fits(value: str, limit: int) -> bool:
    size = utf8_size(value)
    return size is not None and size <= limit


def is_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) > 0
        and value.isprintable()
        and fits(value, limit)
    )


def bounded_text(limit: int) -> AfterValidator:
    def check(value: str) -> str:
        if not is_text(value, limit):
            raise vcs_refused()
        return value

    return AfterValidator(check)


def relative_path(value: str) -> str:
    if not fits(value, MAX_PATH_TEXT_BYTES) or any(
        not fits(part, MAX_COMPONENT_BYTES) for part in value.split("/")
    ):
        raise vcs_refused()
    return value


def no_nul(value: str) -> str:
    if "\x00" in value or utf8_size(value) is None:
        raise vcs_refused()
    return value


def bounded_diff_stat(value: str) -> str:
    if not fits(no_nul(value), MAX_DIFF_STAT_BYTES):
        raise vcs_refused()
    return value


def bounded_argument(value: str) -> str:
    if not fits(no_nul(value), MAX_ARGUMENT_BYTES):
        raise vcs_refused()
    return value


def is_message(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) > 0
        and "\x00" not in value
        and fits(value, MAX_COMMIT_MESSAGE_BYTES)
    )


def binding_text(value: str) -> str:
    if not is_text(value, MAX_BINDING_BYTES):
        raise vcs_refused()
    return value


def is_executor_set(value: object) -> bool:
    if not isinstance(value, frozenset):
        return False
    items = cast("frozenset[object]", value)
    return len(items) > 0 and all(item in PLAN_EXECUTORS for item in items)


def is_bool(value: object) -> bool:
    return isinstance(value, bool)


StrictText = Annotated[str, StringConstraints(strict=True, min_length=1)]
RepoText = Annotated[StrictText, bounded_text(MAX_REPO_BYTES)]
BranchText = Annotated[StrictText, bounded_text(MAX_BRANCH_BYTES)]
RevisionText = Annotated[StrictText, bounded_text(MAX_REVISION_BYTES)]
LocationText = Annotated[StrictText, bounded_text(MAX_PATH_TEXT_BYTES)]
DiffStat = Annotated[
    str,
    StringConstraints(strict=True, max_length=MAX_DIFF_STAT_BYTES),
    AfterValidator(bounded_diff_stat),
]
RelativePath = Annotated[ChangedPath, AfterValidator(relative_path)]
Argument = Annotated[
    str, StringConstraints(strict=True), AfterValidator(bounded_argument)
]
Binding = Annotated[str, StringConstraints(strict=True), AfterValidator(binding_text)]
BindingRevision = Annotated[StrictInt, Field(ge=1, le=MAX_BINDING_REVISION)]
NamedItems = Annotated[tuple[Identifier, ...], Field(max_length=MAX_NAMED_ITEMS)]
StepEnv = Annotated[
    dict[EnvName, Argument],
    Field(max_length=MAX_MAP_ENTRIES),
]


@dataclass(frozen=True)
class VcsCapabilities:
    change_requests: bool
    review_comments: bool
    ci_status: bool
    head_sha_watch: bool
    reviewer_requests: bool
    plan_executors: frozenset[PlanExecutor]

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name != "plan_executors":
                require(is_bool(getattr(self, field.name)), f"{field.name} is a bool")
        require(
            is_executor_set(self.plan_executors),
            "plan_executors is a non-empty frozenset of runner and worker_host",
        )


@dataclass(frozen=True)
class CheckoutRequest:
    repo: str
    rev: str | VcsRef
    root: str
    attempt_id: str
    branch: str

    def __post_init__(self) -> None:
        require(is_text(self.repo, MAX_REPO_BYTES), "repo is bounded text")
        require(
            isinstance(self.rev, VcsRef) or is_text(self.rev, MAX_REVISION_BYTES),
            "rev is bounded text or a VcsRef",
        )
        require(is_text(self.root, MAX_PATH_TEXT_BYTES), "root is bounded text")
        require(is_identifier(self.attempt_id), "attempt_id is a bounded identifier")
        require(is_text(self.branch, MAX_BRANCH_BYTES), "branch is bounded text")


@dataclass(frozen=True)
class PublishRequest:
    repo: str
    root: str
    attempt_id: str
    branch: str
    commit_message: str

    def __post_init__(self) -> None:
        require(is_text(self.repo, MAX_REPO_BYTES), "repo is bounded text")
        require(is_text(self.root, MAX_PATH_TEXT_BYTES), "root is bounded text")
        require(is_identifier(self.attempt_id), "attempt_id is a bounded identifier")
        require(is_text(self.branch, MAX_BRANCH_BYTES), "branch is bounded text")
        require(is_message(self.commit_message), "commit_message is bounded text")


class PlanFile(BoundaryModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    area: Literal["control", "publication"]
    path: RelativePath
    content: Annotated[StrictBytes, Field(max_length=MAX_FILE_CONTENT_BYTES)]
    mode: Annotated[StrictInt, Field(ge=0, le=MAX_FILE_MODE)]


class PlanStep(BoundaryModel):
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    argv: Annotated[
        tuple[Argument, ...],
        Field(min_length=1, max_length=MAX_ARGV_ITEMS),
    ]
    cwd: Literal["candidate", "control", "publication"]
    env: StepEnv
    stdin: Annotated[StrictBytes, Field(max_length=MAX_STDIN_BYTES)] | None
    timeout: Annotated[timedelta, Field(strict=True)]
    capture: Capture | None
    failure_class: StepFailureClass | None = None

    @model_validator(mode="after")
    def bounded_step(self) -> Self:
        if sum(len(argument.encode("utf-8")) for argument in self.argv) > (
            MAX_ARGV_BYTES
        ):
            raise vcs_refused()
        if not timedelta(0) < self.timeout <= MAX_STEP_TIMEOUT:
            raise vcs_refused()
        return self


def step_size(step: PlanStep) -> int:
    stdin = 0 if step.stdin is None else len(step.stdin)
    argv = sum(len(argument.encode("utf-8")) for argument in step.argv)
    env = sum(
        len(name) + len(value.encode("utf-8")) for name, value in step.env.items()
    )
    return stdin + argv + env


def plan_content_size(files: tuple[PlanFile, ...], steps: tuple[PlanStep, ...]) -> int:
    return sum(len(file.content) for file in files) + sum(map(step_size, steps))


def check_plan(files: tuple[PlanFile, ...], steps: tuple[PlanStep, ...]) -> None:
    if len(steps) > MAX_PLAN_STEPS:
        raise vcs_refused()
    if len({(file.area, file.path) for file in files}) != len(files):
        raise vcs_refused()
    if plan_content_size(files, steps) > MAX_PLAN_CONTENT_BYTES:
        raise vcs_refused()


PlanFiles = Annotated[tuple[PlanFile, ...], Field(max_length=MAX_PLAN_FILES)]
PlanSteps = Annotated[tuple[PlanStep, ...], Field(max_length=MAX_PLAN_STEPS)]


class CheckoutPlan(BoundaryModel):
    required_tools: NamedItems
    credentials: Annotated[dict[EnvName, Identifier], Field(max_length=MAX_MAP_ENTRIES)]
    files: PlanFiles
    steps: PlanSteps

    @model_validator(mode="after")
    def bounded_plan(self) -> Self:
        check_plan(self.files, self.steps)
        return self


class PublishPlan(BoundaryModel):
    required_tools: NamedItems
    files: PlanFiles
    export: PlanSteps
    prepare: PlanSteps
    inspect: PlanSteps
    publish: PlanSteps

    @model_validator(mode="after")
    def trusted_phases(self) -> Self:
        trusted = (*self.prepare, *self.inspect, *self.publish)
        if any(step.cwd == "candidate" for step in trusted):
            raise vcs_refused()
        if any(step.cwd != "candidate" for step in self.export):
            raise vcs_refused()
        check_plan(self.files, (*self.export, *trusted))
        return self


class PublicationInput(BoundaryModel):
    attempt_id: Identifier
    format: Identifier
    location: LocationText
    digest: Digest


class InspectionContent(BoundaryModel):
    revision: RevisionText
    path: RelativePath | None
    kind: Literal["file", "symlink", "revision_metadata"]
    content_location: LocationText
    content_digest: Digest
    size_bytes: Count

    @model_validator(mode="after")
    def path_matches_kind(self) -> Self:
        if (self.kind == "revision_metadata") != (self.path is None):
            raise vcs_refused()
        return self


class InspectionManifest(BoundaryModel):
    prepared_digest: Digest
    revision: RevisionText
    content_index_location: LocationText
    content_index_digest: Digest
    complete: StrictBool


class PreparedPublication(BoundaryModel):
    attempt_id: Identifier
    operation_id: Identifier
    input_digest: Digest
    prepared_digest: Digest
    inspection_digest: Digest
    repo: RepoText
    branch: BranchText
    commit: Annotated[RevisionText, StringConstraints(max_length=255)]
    base_commit: Annotated[RevisionText, StringConstraints(max_length=255)]
    destination_digest: Digest
    changed_paths: Annotated[tuple[RelativePath, ...], Field(max_length=MAX_PATHS)]
    diff_stat: DiffStat

    @model_validator(mode="after")
    def fits_a_vcs_ref(self) -> Self:
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise vcs_refused()
        if sum(len(path.encode("utf-8")) for path in self.changed_paths) > (
            MAX_PATH_BYTES
        ):
            raise vcs_refused()
        if vcs_ref_size(self) > MAX_JSON_BYTES:
            raise vcs_refused()
        return self


def vcs_ref_size(prepared: PreparedPublication) -> int:
    projection: JsonObject = {
        "repo": prepared.repo,
        "branch": prepared.branch,
        "commit": prepared.commit,
        "base_commit": prepared.base_commit,
        "changed_paths": list(prepared.changed_paths),
        "diff_stat": prepared.diff_stat,
    }
    return canonical_size(projection) + PUSHED_BY_MEMBER_BYTES + WORST_PRINCIPAL_BYTES


class CredentialRequest(BoundaryModel):
    binding: Binding
    binding_revision: BindingRevision
    credential_mode: CredentialMode
    repo: RepoText
    purpose: Literal["checkout", "publish", "api"]
    attempt_id: Identifier | None
    operation_id: Identifier | None
    prepared_digest: Digest | None
    destination_digest: Digest | None

    @model_validator(mode="after")
    def fields_match_purpose(self) -> Self:
        bound = (self.operation_id, self.prepared_digest, self.destination_digest)
        if self.purpose == "publish":
            if self.attempt_id is None or None in bound:
                raise vcs_refused()
        elif any(value is not None for value in bound):
            raise vcs_refused()
        if self.purpose == "checkout" and self.attempt_id is None:
            raise vcs_refused()
        return self


class CredentialRights(BoundaryModel):
    resource_scope: Literal["repository", "repository_set", "account", "unknown"]
    native_permissions: NamedItems
    operations: frozenset[Literal["read", "write_refs", "change_requests", "comments"]]


def presence_matches_mode(
    mode: CredentialMode, value: SecretValue | None, principal: PrincipalRef
) -> bool:
    if mode == "local_credential_free":
        return value is None and principal.kind == "local_service"
    return value is not None and principal.kind != "local_service"


class IssuedCredential(BoundaryModel):
    binding: Binding
    binding_revision: BindingRevision
    credential_mode: CredentialMode
    value: SecretTransfer | None
    principal: PrincipalRef
    rights: CredentialRights | None
    expires_at: Instant | None
    replacement: Literal["reissue", "rotate", "static", "not_applicable"]
    revocation: Literal["native", "expiry_only", "manual", "not_applicable"]

    @model_validator(mode="after")
    def combination_matches_mode(self) -> Self:
        if not presence_matches_mode(self.credential_mode, self.value, self.principal):
            raise vcs_refused()
        free = self.credential_mode == "local_credential_free"
        not_applicable = (
            self.replacement == "not_applicable",
            self.revocation == "not_applicable",
        )
        if free and (
            self.rights is not None
            or self.expires_at is not None
            or not all(not_applicable)
        ):
            raise vcs_refused()
        if not free and (self.rights is None or any(not_applicable)):
            raise vcs_refused()
        return self


class Vcs(Protocol):
    name: str
    config_schema: JsonObject

    def capabilities(self) -> VcsCapabilities: ...

    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential: ...

    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan: ...

    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan: ...

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str: ...


def printable_path(value: str) -> str:
    if not value.isprintable():
        raise PydanticCustomError("path_invalid", "the path is invalid")
    return value


PathText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=4096),
    AfterValidator(printable_path),
]


class WorkspaceTarget(BoundaryModel):
    root: PathText
    host_path: PathText | None


class WorkspaceRef(BoundaryModel):
    attempt_id: Identifier
    root: PathText
    repo: PathText
    commit: Identifier
    base_commit: Identifier
    branch: Identifier | None
