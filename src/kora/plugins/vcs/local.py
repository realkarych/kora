import getpass
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal, NoReturn, Self
from urllib.parse import unquote

from pydantic import Field, StringConstraints, model_validator

from kora.executor.entry import Destination
from kora.ir import PrincipalRef
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import (
    Binding,
    BindingRevision,
    CredentialMode,
    CredentialRequest,
    IssuedCredential,
    RepoText,
    VcsCapabilities,
)

SLICE_ONE_MODE: Final[str] = "local_credential_free"
ADDRESS_SPACE_BYTES: Final[int] = 2 * 1024**3
FILE_SIZE_BYTES: Final[int] = 1024**3
FILE_SCHEME: Final[str] = "file://"
BAD_ESCAPE: Final[re.Pattern[str]] = re.compile(r"%(?![0-9A-Fa-f]{2})")
URL_SPECIAL: Final[frozenset[str]] = frozenset("?#")
TRANSPORT: Final[JsonObject] = {"transport": "file", "push": "create_only"}
REFUSALS: Final[dict[str, tuple[str, bool]]] = {
    "vcs_config_invalid": ("the VCS provider configuration is invalid", False),
    "vcs_repository_unknown": ("the repository is not configured", False),
    "vcs_binding_mismatch": ("the credential binding does not match", False),
    "vcs_purpose_unsupported": ("the credential purpose is unsupported", False),
    "vcs_request_invalid": ("the VCS request is invalid", False),
    "vcs_destination_changed": ("the destination repository changed", False),
    "vcs_ref_missing": ("the ref does not exist", False),
    "vcs_remote_unreachable": ("the remote repository is unreachable", True),
}
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


class LocalConfiguration(BoundaryModel):
    remotes: Annotated[dict[RepoText, RemoteConfiguration], Field(min_length=1)]
    tool_path: Annotated[tuple[PathEntry, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def one_binding_per_remote(self) -> Self:
        bindings = [remote.binding for remote in self.remotes.values()]
        if len(set(bindings)) != len(bindings):
            raise ValueError("each remote has its own binding")
        return self


def needs_prlimit() -> bool:
    return sys.platform == "linux"


def limited_argv(
    prlimit: str | None, tool: str, path: str, arguments: Sequence[str]
) -> tuple[str, ...]:
    if prlimit is None:
        return (tool, *arguments)
    return (
        "prlimit",
        f"--as={ADDRESS_SPACE_BYTES}",
        f"--fsize={FILE_SIZE_BYTES}",
        "--",
        path,
        *arguments,
    )


class RemoteRefused(ValueError):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


@dataclass(frozen=True)
class RemotePin:
    device: int
    inode: int

    @property
    def text(self) -> str:
        return f"{self.device}:{self.inode}"


def decoded_url(configured: str) -> str:
    rest = configured[len(FILE_SCHEME) :]
    if not rest.startswith("/") or URL_SPECIAL & set(rest):
        raise RemoteRefused("remote_url_refused")
    if BAD_ESCAPE.search(rest) is not None:
        raise RemoteRefused("remote_url_malformed")
    try:
        return unquote(rest, errors="strict")
    except UnicodeDecodeError:
        raise RemoteRefused("remote_url_malformed") from None


def remote_path(configured: str) -> str:
    if configured.startswith(FILE_SCHEME):
        path = decoded_url(configured)
    elif configured.startswith("/"):
        path = configured
    else:
        raise RemoteRefused("remote_form_refused")
    if "\x00" in path or not path.isprintable():
        raise RemoteRefused("remote_path_refused")
    parts = path.split("/")[1:]
    if any(part in (".", "..") for part in parts):
        raise RemoteRefused("remote_traversal_refused")
    normalized = os.path.normpath(path)
    if os.path.realpath(normalized) != normalized:
        raise RemoteRefused("remote_path_aliased")
    return normalized


def pin_of(path: str) -> RemotePin:
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        raise RemoteRefused("remote_missing") from None
    if not stat.S_ISDIR(status.st_mode):
        raise RemoteRefused("remote_not_bare")
    return RemotePin(status.st_dev, status.st_ino)


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
    configuration: RemoteConfiguration, path: str, identities: Iterable[str]
) -> None:
    forbidden = {
        configuration.binding,
        configuration.path,
        path,
        getpass.getuser(),
        *identities,
    }
    if configuration.principal.stable_id in forbidden:
        configuration_refused("principal_not_stable")


def located_remote(
    repo: str,
    remote: RemoteConfiguration,
    check_repository: Callable[[str], None],
    identities: Iterable[str],
) -> Remote:
    if remote.credential_mode != SLICE_ONE_MODE:
        configuration_refused("credential_mode_unsupported")
    try:
        path = remote_path(remote.path)
        check_repository(path)
        located = Remote(repo, path, remote)
    except RemoteRefused as refused:
        configuration_refused(refused.rule)
    check_principal(remote, path, identities)
    return located


@dataclass(frozen=True)
class LocalEvents:
    refused: EventSpec
    planned: EventSpec
    issued: EventSpec
    head_read: EventSpec


def local_events(component: str, provider: str) -> LocalEvents:
    CATALOG.register_component(component)

    def event(suffix: str, level: Level, message: str, field: str) -> EventSpec:
        return CATALOG.register(
            EventSpec(
                f"{component}.{suffix}",
                component,
                level,
                message,
                {field: FieldKind.STRING},
            )
        )

    return LocalEvents(
        refused=event(
            "refused",
            Level.WARNING,
            f"a {provider} provider call was refused",
            "reason",
        ),
        planned=event("planned", Level.DEBUG, f"a {provider} plan was built", "kind"),
        issued=event(
            "local_binding_used",
            Level.DEBUG,
            "a local credential-free binding was returned",
            "purpose",
        ),
        head_read=event("head_read", Level.DEBUG, "a remote head was read", "outcome"),
    )


class LocalVcs:
    def __init__(
        self, name: str, events: LocalEvents, remotes: dict[str, Remote]
    ) -> None:
        self.name: str = name
        self.events = events
        self.remotes = remotes

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
        ctx.logger.emit(self.events.refused, reason=code)
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
        ctx.logger.emit(self.events.issued, purpose=request.purpose)
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

    def destination(self, repo: str) -> Destination:
        remote = self.remotes.get(repo)
        if remote is None:
            raise refusal("vcs_repository_unknown")
        state = remote.state()
        if state == "missing":
            raise refusal("vcs_remote_unreachable")
        if state == "changed":
            raise refusal("vcs_destination_changed")
        return Destination(
            provider=self.name,
            remote=remote.path,
            pin=remote.pin.text,
            transport=dict(TRANSPORT),
        )
