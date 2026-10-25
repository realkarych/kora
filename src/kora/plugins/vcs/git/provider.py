import getpass
import tempfile
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Final, Literal, NoReturn, Self

from pydantic import Field, StringConstraints, model_validator

from kora.executor.entry import Destination, Destinations, ReadsHeads
from kora.executor.launcher import (
    Ending,
    LaunchSpec,
    ProcessLauncher,
    StepLauncher,
)
from kora.executor.scan import InspectionSource, InspectionSources
from kora.ir import PrincipalRef, VcsRef
from kora.ir.json import JsonObject, parse_json_object
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.vcs.git.environment import git_environment, trusted_options
from kora.plugins.vcs.git.plans import (
    CommitIdentity,
    PlanSettings,
    checkout_plan,
    is_branch,
    is_commit_message,
    is_full_ref,
    is_object_id,
    is_publish_branch,
    publish_plan,
)
from kora.plugins.vcs.git.remotes import (
    RemotePin,
    RemoteRefused,
    check_bare,
    pin_of,
    remote_path,
)
from kora.plugins.vcs.git.source import GitInspectionSources
from kora.plugins.vcs.git.tools import GitTools, ToolRefused, resolve_git_tools
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.vcs import (
    Binding,
    BindingRevision,
    CheckoutPlan,
    CheckoutRequest,
    CredentialMode,
    CredentialRequest,
    IssuedCredential,
    PublishPlan,
    PublishRequest,
    RepoText,
    VcsCapabilities,
)

VERSION: Final[str] = "1"
PROVIDER_NAME: Final[str] = "git"
SLICE_ONE_MODE: Final[str] = "local_credential_free"
HEAD_TIMEOUT_NS: Final[int] = 30 * NANOSECONDS_PER_SECOND
HEAD_OUTPUT: Final[int] = 65_536
MISSING_REF_EXIT: Final[int] = 2
REFUSALS: Final[dict[str, tuple[str, bool]]] = {
    "vcs_config_invalid": ("the git provider configuration is invalid", False),
    "vcs_repository_unknown": ("the repository is not configured", False),
    "vcs_binding_mismatch": ("the credential binding does not match", False),
    "vcs_purpose_unsupported": ("the credential purpose is unsupported", False),
    "vcs_request_invalid": ("the VCS request is invalid", False),
    "vcs_destination_changed": ("the destination repository changed", False),
    "vcs_ref_missing": ("the ref does not exist", False),
    "vcs_remote_unreachable": ("the remote repository is unreachable", True),
}
COMPONENT: Final[str] = CATALOG.register_component("vcs.git")
REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "vcs.git.refused",
        COMPONENT,
        Level.WARNING,
        "a git provider call was refused",
        {"reason": FieldKind.STRING},
    )
)
PLANNED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "vcs.git.planned",
        COMPONENT,
        Level.DEBUG,
        "a git plan was built",
        {"kind": FieldKind.STRING},
    )
)
ISSUED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "vcs.git.local_binding_used",
        COMPONENT,
        Level.DEBUG,
        "a local credential-free binding was returned",
        {"purpose": FieldKind.STRING},
    )
)
HEAD_READ: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "vcs.git.head_read",
        COMPONENT,
        Level.DEBUG,
        "a remote head was read",
        {"outcome": FieldKind.STRING},
    )
)
Identity = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[\x21-\x7e]{1,255}$")
]
DisplayText = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=255)
]


PathEntry = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=4096)
]
RemoteState = Literal["same", "missing", "changed"]


class LocalPrincipal(BoundaryModel):
    provider_instance: Identity
    stable_id: Identity
    display_name: DisplayText


class RemoteConfiguration(BoundaryModel):
    path: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
    binding: Binding
    binding_revision: BindingRevision
    credential_mode: CredentialMode
    principal: LocalPrincipal


class IdentityConfiguration(BoundaryModel):
    name: DisplayText
    email: DisplayText


class GitConfiguration(BoundaryModel):
    remotes: Annotated[dict[RepoText, RemoteConfiguration], Field(min_length=1)]
    identity: IdentityConfiguration
    tool_path: Annotated[tuple[PathEntry, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def one_binding_per_remote(self) -> Self:
        bindings = [remote.binding for remote in self.remotes.values()]
        if len(set(bindings)) != len(bindings):
            raise ValueError("each remote has its own binding")
        return self


CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    GitConfiguration.model_json_schema()
)


class Remote:
    def __init__(
        self, repo: str, path: str, configuration: RemoteConfiguration
    ) -> None:
        self.repo = repo
        self.path = path
        self.configuration = configuration
        self.pin: RemotePin = pin_of(path)

    @property
    def principal(self) -> PrincipalRef:
        principal = self.configuration.principal
        return PrincipalRef.model_validate(
            {
                "provider_instance": principal.provider_instance,
                "stable_id": principal.stable_id,
                "kind": "local_service",
                "display_name": principal.display_name,
            }
        )

    def state(self) -> RemoteState:
        try:
            return "same" if pin_of(self.path) == self.pin else "changed"
        except RemoteRefused:
            return "missing"


def refusal(code: str, details: JsonObject | None = None) -> KoraProtocolError:
    message, retryable = REFUSALS[code]
    return KoraProtocolError(code, message, retryable, details or {})


def configuration_refused(rule: str) -> NoReturn:
    raise refusal("vcs_config_invalid", {"rule": rule})


def check_principal(
    configuration: RemoteConfiguration, path: str, identity: IdentityConfiguration
) -> None:
    stable = configuration.principal.stable_id
    forbidden = {
        configuration.binding,
        configuration.path,
        path,
        getpass.getuser(),
        identity.name,
        identity.email,
    }
    if stable in forbidden:
        configuration_refused("principal_not_stable")


class GitVcs(Destinations, InspectionSources, ReadsHeads):
    name: str = PROVIDER_NAME
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(
        self,
        configuration: GitConfiguration,
        launcher: StepLauncher | None = None,
    ) -> None:
        self.launcher = launcher or ProcessLauncher()
        with tempfile.TemporaryDirectory(prefix="kora-git-") as home:
            try:
                self.git_tools: GitTools = resolve_git_tools(
                    configuration.tool_path, Path(home)
                )
            except ToolRefused as refused:
                configuration_refused(refused.rule)
            self.remotes: dict[str, Remote] = {
                repo: self.configured_remote(repo, remote, configuration, Path(home))
                for repo, remote in configuration.remotes.items()
            }
        self.identity = CommitIdentity(
            configuration.identity.name, configuration.identity.email
        )
        self.sources = GitInspectionSources(self.git_tools, self.launcher)

    def configured_remote(
        self,
        repo: str,
        remote: RemoteConfiguration,
        configuration: GitConfiguration,
        home: Path,
    ) -> Remote:
        if remote.credential_mode != SLICE_ONE_MODE:
            configuration_refused("credential_mode_unsupported")
        try:
            path = remote_path(remote.path)
            check_bare(path, self.git_tools.git, home)
            located = Remote(repo, path, remote)
        except RemoteRefused as refused:
            configuration_refused(refused.rule)
        check_principal(remote, path, configuration.identity)
        return located

    @property
    def tools(self) -> Mapping[str, str]:
        return self.git_tools.mapping()

    def capabilities(self) -> VcsCapabilities:
        return VcsCapabilities(
            change_requests=False,
            review_comments=False,
            ci_status=False,
            head_sha_watch=False,
            reviewer_requests=False,
            plan_executors=frozenset({"runner", "worker_host"}),
        )

    def refuse(self, ctx: ProtocolContext, code: str) -> NoReturn:
        ctx.logger.emit(REFUSED, reason=code)
        raise refusal(code)

    def remote(self, repo: str, ctx: ProtocolContext) -> Remote:
        remote = self.remotes.get(repo)
        if remote is None:
            self.refuse(ctx, "vcs_repository_unknown")
        self.require_same(remote, ctx)
        return remote

    def require_same(self, remote: Remote, ctx: ProtocolContext) -> None:
        state = remote.state()
        if state == "missing":
            self.refuse(ctx, "vcs_remote_unreachable")
        if state == "changed":
            self.refuse(ctx, "vcs_destination_changed")

    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        if request.purpose == "api":
            self.refuse(ctx, "vcs_purpose_unsupported")
        remote = self.remotes.get(request.repo)
        if remote is None:
            self.refuse(ctx, "vcs_binding_mismatch")
        configured = remote.configuration
        if (
            request.binding,
            request.binding_revision,
            request.credential_mode,
        ) != (
            configured.binding,
            configured.binding_revision,
            configured.credential_mode,
        ):
            self.refuse(ctx, "vcs_binding_mismatch")
        ctx.logger.emit(ISSUED, purpose=request.purpose)
        return IssuedCredential(
            binding=configured.binding,
            binding_revision=configured.binding_revision,
            credential_mode=configured.credential_mode,
            value=None,
            principal=remote.principal,
            rights=None,
            expires_at=None,
            replacement="not_applicable",
            revocation="not_applicable",
        )

    def settings(self, remote: Remote) -> PlanSettings:
        return PlanSettings(self.git_tools, remote.path, self.identity)

    async def checkout_plan(
        self, request: CheckoutRequest, ctx: ProtocolContext
    ) -> CheckoutPlan:
        remote = self.remote(request.repo, ctx)
        rev = request.rev
        if isinstance(rev, VcsRef):
            valid = is_object_id(rev.commit) and is_object_id(rev.base_commit)
        else:
            valid = is_object_id(rev) or is_full_ref(rev)
        if not valid or not is_branch(request.branch):
            self.refuse(ctx, "vcs_request_invalid")
        plan = checkout_plan(self.settings(remote), rev, request.branch)
        ctx.logger.emit(PLANNED, kind="checkout")
        return plan

    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        remote = self.remote(request.repo, ctx)
        if not is_publish_branch(request.branch) or not is_commit_message(
            request.commit_message
        ):
            self.refuse(ctx, "vcs_request_invalid")
        plan = publish_plan(
            self.settings(remote),
            request.branch,
            request.commit_message,
            ctx.clock.now(),
        )
        ctx.logger.emit(PLANNED, kind="publish")
        return plan

    def destination(self, repo: str) -> Destination:
        remote = self.remotes.get(repo)
        if remote is None:
            raise refusal("vcs_repository_unknown")
        state = remote.state()
        if state == "missing":
            raise refusal("vcs_remote_unreachable")
        if state == "changed":
            raise refusal("vcs_destination_changed")
        pin = remote.pin
        return Destination(
            provider=PROVIDER_NAME,
            remote=remote.path,
            pin=pin.text,
            transport={"transport": "file", "push": "create_only"},
        )

    def open(
        self, prepared_location: str
    ) -> AbstractAsyncContextManager[InspectionSource]:
        return self.sources.open(prepared_location)

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        remote = self.remote(ref.repo, ctx)
        if not is_branch(ref.branch):
            self.refuse(ctx, "vcs_request_invalid")
        name = f"refs/heads/{ref.branch}"
        home = tempfile.mkdtemp(prefix="kora-git-head-")
        try:
            outcome = await self.launcher.run(
                LaunchSpec(
                    argv=(
                        self.git_tools.git,
                        "ls-remote",
                        "--exit-code",
                        "--end-of-options",
                        remote.path,
                        name,
                    ),
                    cwd=Path(home),
                    env=git_environment(
                        self.git_tools.git, home, trusted_options(f"{home}/hooks", ())
                    ),
                    stdin=None,
                    timeout_ns=HEAD_TIMEOUT_NS,
                    stdout_limit=HEAD_OUTPUT,
                    stderr_limit=4096,
                )
            )
        finally:
            Path(home).rmdir()
        self.require_same(remote, ctx)
        if outcome.ending is Ending.EXITED and outcome.exit_code == MISSING_REF_EXIT:
            ctx.logger.emit(HEAD_READ, outcome="missing")
            raise refusal("vcs_ref_missing")
        if not outcome.succeeded or outcome.stdout_overflow:
            self.refuse(ctx, "vcs_remote_unreachable")
        return self.parsed_head(outcome.stdout, name, ctx)

    def parsed_head(self, output: bytes, name: str, ctx: ProtocolContext) -> str:
        matches = [
            line.split(b"\t", 1)[0].decode("ascii", "replace")
            for line in output.splitlines()
            if line.split(b"\t", 1)[1:] == [name.encode("utf-8")]
        ]
        if len(matches) != 1 or not is_object_id(matches[0]):
            self.refuse(ctx, "vcs_remote_unreachable")
        ctx.logger.emit(HEAD_READ, outcome="found")
        return matches[0]


def create_provider(configuration: JsonObject) -> GitVcs:
    return GitVcs(GitConfiguration.model_validate(configuration))


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.VCS, PROVIDER_NAME, create_provider, CONFIG_SCHEMA, VERSION
    )
