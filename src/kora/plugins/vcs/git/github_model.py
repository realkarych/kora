import re
import ssl
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import certifi
from pydantic import Field, StrictInt, model_validator
from pydantic_core import PydanticCustomError

from kora.ir import PrincipalRef
from kora.ir.json import JsonValue, fits_utf8
from kora.plugins.vcs import local
from kora.plugins.vcs.git.plans import is_checkout_branch, is_object_id
from kora.protocols.common import BoundaryModel, ProtocolContext, is_identifier
from kora.protocols.identity import instant_input, utc_instant
from kora.protocols.secret_value import ScopeAdmission, SecretScope
from kora.protocols.vcs import (
    Binding,
    BindingRevision,
    ChangeRequestCall,
    ChangeRequestFact,
    ChangeRequestIdentity,
    ChangeRequestIntent,
    RepoText,
)

API_ORIGIN: Final[str] = "https://api.github.com"
WEB_ORIGIN: Final[str] = "https://github.com"
MAX_PR_NUMBER: Final[int] = 2**31 - 1
MAX_HOST_ID: Final[int] = 2**63 - 1
HostId = Annotated[StrictInt, Field(gt=0, le=MAX_HOST_ID)]
Access = Literal["precreate", "historical", "create"]
OBJECT_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]{1,100}")
LOGIN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,100}(?:\[bot\])?")
LOOPBACK: Final[re.Pattern[str]] = re.compile(r"https://127\.0\.0\.1:([1-9][0-9]{0,4})")


class GitHubConfiguration(BoundaryModel):
    repo: RepoText
    binding: Binding
    binding_revision: BindingRevision
    repository_id: HostId
    owner: local.DisplayText
    repository: local.DisplayText
    base_branch: local.DisplayText
    provider_instance: local.DisplayText
    bot_account_id: HostId
    api_origin: local.DisplayText = API_ORIGIN
    ca_file: local.PathEntry | None = None

    @model_validator(mode="after")
    def destination_is_bounded(self) -> Self:
        loopback = LOOPBACK.fullmatch(self.api_origin)
        if (
            LOGIN.fullmatch(self.owner) is None
            or "[" in self.owner
            or OBJECT_NAME.fullmatch(self.repository) is None
            or self.repository in {".", ".."}
            or not is_checkout_branch(self.base_branch)
            or not self.provider_instance.isprintable()
        ):
            raise ValueError("the GitHub destination is invalid")
        if self.api_origin == API_ORIGIN and self.ca_file is None:
            return self
        if (
            loopback is None
            or int(loopback[1]) > 65535
            or self.ca_file is None
            or not Path(self.ca_file).is_absolute()
        ):
            raise ValueError("the GitHub API origin is invalid")
        return self

    def tls(self) -> ssl.SSLContext:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(self.ca_file or certifi.where())
            return context
        except (OSError, ValueError):
            local.configuration_refused("github_tls_invalid")


def bound_id(value: JsonValue, maximum: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum:
        return value
    return None


def repository_id(value: JsonValue) -> int | None:
    if isinstance(value, dict):
        return bound_id(value.get("id"), MAX_HOST_ID)
    return None


def identity_of(item: JsonValue, bound: int) -> ChangeRequestIdentity | None:
    if not isinstance(item, dict):
        return None
    base = item.get("base")
    number = bound_id(item.get("number"), MAX_PR_NUMBER)
    if (
        number is None
        or not isinstance(base, dict)
        or repository_id(base.get("repo")) != bound
    ):
        return None
    return ChangeRequestIdentity(str(bound), number)


def ref_matches(value: JsonValue, repo: int, branch: str, commit: str) -> bool:
    return (
        isinstance(value, dict)
        and repository_id(value.get("repo")) == repo
        and value.get("ref") == branch
        and value.get("sha") == commit
    )


def opener(value: JsonValue, config: GitHubConfiguration) -> PrincipalRef | None:
    if not isinstance(value, dict):
        return None
    account = bound_id(value.get("id"), MAX_HOST_ID)
    login = value.get("login")
    actor_type = value.get("type")
    if account is None or not isinstance(login, str) or LOGIN.fullmatch(login) is None:
        return None
    if actor_type == "Bot":
        kind = "app" if account == config.bot_account_id else "bot_user"
    elif actor_type == "User" and account != config.bot_account_id:
        kind = "user"
    else:
        return None
    return PrincipalRef(
        provider_instance=config.provider_instance,
        stable_id=f"account:{account}",
        kind=kind,
        display_name=login,
    )


def contains_registered(scope: SecretScope, values: tuple[str, ...]) -> bool:
    return any(
        secret in value for secret in scope.result_spellings() for value in values
    )


def verify_fact(
    item: JsonValue,
    identity: ChangeRequestIdentity,
    intent: ChangeRequestIntent,
    config: GitHubConfiguration,
    scope: SecretScope,
) -> ChangeRequestFact | None:
    if not isinstance(item, dict):
        return None
    ref = intent.published
    if not (
        ref_matches(item.get("head"), config.repository_id, ref.branch, ref.commit)
        and ref_matches(
            item.get("base"), config.repository_id, intent.base_branch, ref.base_commit
        )
    ):
        return None
    actor = opener(item.get("user"), config)
    link = f"{WEB_ORIGIN}/{config.owner}/{config.repository}/pull/{identity.number}"
    state = item.get("state")
    if actor is None or item.get("html_url") != link or state not in ("open", "closed"):
        return None
    observed_state: Literal["open", "closed", "merged"] = (
        "open" if state == "open" else "closed"
    )
    if (merged_at := item.get("merged_at")) is not None:
        try:
            utc_instant(instant_input(merged_at))
        except PydanticCustomError:
            return None
        if observed_state != "closed":
            return None
        observed_state = "merged"
    values = (
        link,
        actor.display_name,
        actor.stable_id,
        actor.provider_instance,
        ref.commit,
        ref.base_commit,
    )
    if contains_registered(scope, values):
        return None
    return ChangeRequestFact(
        identity, observed_state, ref.commit, ref.base_commit, actor, link
    )


def admit_call(
    call: ChangeRequestCall,
    context: ProtocolContext,
    config: GitHubConfiguration,
    access: Access,
) -> str:
    intent, grant = call.intent, call.grant
    request, issued = grant.request, grant.issued
    token, rights = issued.value, issued.rights
    same_binding = (config.binding, config.binding_revision, "credential_required")
    expected_pull = (
        "pull_requests:write" if access == "create" else "pull_requests:read"
    )
    expected_native = {"metadata:read", expected_pull}
    if access == "precreate":
        expected_native.add("contents:read")
    expected_coarse = {"change_requests"} if access == "create" else {"read"}
    if not (
        (intent.binding, intent.binding_revision) == same_binding[:2]
        and (request.binding, request.binding_revision, request.credential_mode)
        == same_binding
        and (issued.binding, issued.binding_revision, issued.credential_mode)
        == same_binding
        and request.purpose == "api"
        and request.repo == intent.published.repo == config.repo
        and request.attempt_id == context.attempt_id
        and context.attempt_id is not None
        and grant.namespace_id == context.namespace_id
        and is_identifier(call.dispatcher_id)
        and grant.dispatcher_id == call.dispatcher_id
        and grant.repository_id == intent.repository_id == str(config.repository_id)
        and intent.base_branch == config.base_branch
        and intent.published.pushed_by.kind != "local_service"
        and is_checkout_branch(intent.published.branch)
        and fits_utf8(intent.published.branch, 1024)
        and is_object_id(intent.published.commit)
        and is_object_id(intent.published.base_commit)
        and issued.principal.provider_instance == config.provider_instance
        and issued.principal.kind == "app"
        and issued.principal.stable_id == f"account:{config.bot_account_id}"
        and issued.expires_at is not None
        and issued.expires_at > context.clock.now()
        and issued.replacement == "reissue"
        and issued.revocation == "native"
        and rights is not None
        and rights.resource_scope == "repository"
        and set(rights.native_permissions) == expected_native
        and expected_coarse <= rights.operations <= (expected_coarse | {"read"})
        and token is not None
        and token.belongs_to(grant.scope)
        and isinstance(context.secret_admission, ScopeAdmission)
        and context.secret_admission.owns(token)
    ):
        raise local.refusal("vcs_binding_mismatch")
    return token.reveal()
