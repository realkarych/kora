import asyncio
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, NoReturn, Self

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import model_validator
from pydantic_core import PydanticCustomError

from kora.ir import PrincipalRef
from kora.ir.json import JsonObject, JsonValue, fits_utf8
from kora.observability import (
    CATALOG,
    EventSpec,
    FieldKind,
    Level,
    SecretAdmissionError,
)
from kora.plugins.vcs.git.github import useful_seconds
from kora.plugins.vcs.git.github_http import (
    GitHubTransportError,
    HostResponse,
    github_exchange,
)
from kora.plugins.vcs.git.github_model import (
    Access,
    GitHubConfiguration,
    HostId,
    api_operation,
    api_permissions,
    repository_id,
)
from kora.plugins.vcs.local import DisplayText
from kora.protocols.common import BoundaryModel, ProtocolContext, is_identifier
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Identifier, instant_input, utc_instant
from kora.protocols.secret_value import (
    MAX_TRANSFER_BYTES,
    TRANSFER_SCOPE_KEY,
    ScopeAdmission,
    SecretRevealRefused,
    SecretScope,
)
from kora.protocols.secrets import SecretsProvider
from kora.protocols.vcs import (
    ChangeRequestGrant,
    CredentialRequest,
    CredentialRights,
    IssuedCredential,
)

MAX_TOKEN_TTL: Final[timedelta] = timedelta(hours=1, minutes=1)
ASSERTION_AGE: Final[timedelta] = timedelta(minutes=5)
CLOCK_SKEW: Final[timedelta] = timedelta(minutes=1)
CLIENT_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]{1,128}")
BEARER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~+/-]+=*")
CREDENTIAL_ISSUANCE = CATALOG.register(
    EventSpec(
        "vcs.git.github_app_credential",
        "vcs.git",
        Level.DEBUG,
        "an App credential exchange finished",
        {"outcome": FieldKind.STRING},
    )
)


class GitHubAppConfiguration(BoundaryModel):
    destination: GitHubConfiguration
    app_id: HostId
    client_id: DisplayText
    installation_id: HostId
    namespace_id: Identifier
    key_namespace_id: Identifier
    private_key_reference: DisplayText
    bot_display_name: DisplayText

    @model_validator(mode="after")
    def require_private_key_binding(self) -> Self:
        if (
            self.namespace_id == self.key_namespace_id
            or CLIENT_ID.fullmatch(self.client_id) is None
            or not self.private_key_reference.isprintable()
            or not self.bot_display_name.isprintable()
        ):
            raise ValueError("the App issuer binding is invalid")
        return self


@dataclass(frozen=True)
class AppCredentialRequest:
    credential: CredentialRequest
    role: Access
    dispatcher_id: str


def refused(reason: str) -> NoReturn:
    raise KoraProtocolError(
        "github_app_credential_refused",
        "the GitHub App credential was refused",
        False,
        {"reason": reason},
    ) from None


def assertion(value: str, client_id: str, now: datetime) -> str:
    if not value.isascii() or not fits_utf8(value, MAX_TRANSFER_BYTES):
        refused("signing_key_invalid")
    try:
        key = load_pem_private_key(value.encode("ascii"), password=None)
        if not isinstance(key, RSAPrivateKey) or key.key_size not in (2048, 3072, 4096):
            refused("signing_key_invalid")
        return jwt.encode(
            {
                "iss": client_id,
                "iat": int((now - CLOCK_SKEW).timestamp()),
                "exp": int((now + ASSERTION_AGE).timestamp()),
            },
            key,
            algorithm="RS256",
        )
    except (TypeError, ValueError, jwt.PyJWTError):
        refused("signing_key_invalid")


def response_object(response: HostResponse, status: int) -> JsonObject:
    if response.status != status or not isinstance(response.value, dict):
        refused("host_response_invalid")
    return response.value


def exact_repository(items: JsonValue, pinned: int) -> bool:
    return (
        isinstance(items, list)
        and len(items) == 1
        and repository_id(items[0]) == pinned
    )


def valid_expiry(raw: JsonValue, now: datetime) -> datetime:
    try:
        expiry = utc_instant(instant_input(raw))
    except PydanticCustomError:
        refused("token_expiry_invalid")
    if not timedelta() < expiry - now <= MAX_TOKEN_TTL:
        refused("token_expiry_invalid")
    return expiry


class GitHubAppIssuer:
    def __init__(
        self,
        config: GitHubAppConfiguration,
        keys: SecretsProvider,
        scope_factory: Callable[[], SecretScope],
    ) -> None:
        self._config = config
        self._keys = keys
        self._scope_factory = scope_factory
        self._tls = config.destination.tls()
        self._principal = PrincipalRef(
            provider_instance=config.destination.provider_instance,
            stable_id=f"account:{config.destination.bot_account_id}",
            kind="app",
            display_name=config.bot_display_name,
        )

    def _admit(
        self,
        request: AppCredentialRequest,
        caller_scope: SecretScope,
        ctx: ProtocolContext,
    ) -> None:
        credential, config = request.credential, self._config
        destination = config.destination
        admission = ctx.secret_admission
        if not (
            request.role in ("historical", "precreate", "create")
            and is_identifier(request.dispatcher_id)
            and credential.binding == destination.binding
            and credential.binding_revision == destination.binding_revision
            and credential.credential_mode == "credential_required"
            and credential.repo == destination.repo
            and credential.purpose == "api"
            and credential.attempt_id is not None
            and credential.attempt_id == ctx.attempt_id
            and ctx.namespace_id == config.namespace_id
            and ctx.request_digest is not None
            and isinstance(admission, ScopeAdmission)
            and admission.binds(caller_scope)
        ):
            refused("binding_or_role_mismatch")

    def _private_scope(self, *excluded: SecretScope) -> SecretScope:
        scope = self._scope_factory()
        if scope in excluded or scope.released:
            refused("private_scope_unavailable")
        return scope

    async def _mint(
        self, permissions: Mapping[str, str], parent: SecretScope, ctx: ProtocolContext
    ) -> JsonObject:
        config = self._config
        private_context = replace(
            ctx,
            namespace_id=config.key_namespace_id,
            secrets=None,
            secret_admission=parent.admission(),
        )
        try:
            key = await self._keys.resolve(
                config.private_key_reference, private_context
            )
        except KoraProtocolError:
            refused("signing_key_unavailable")
        if not key.belongs_to(parent):
            refused("signing_key_unavailable")
        signed = parent.admit(
            assertion(key.reveal(), config.client_id, ctx.clock.now())
        )
        path = f"/app/installations/{config.installation_id}"
        async with github_exchange(
            config.destination.api_origin, self._tls, signed.reveal()
        ) as host:
            app = response_object(await host.get("/app"), 200)
            if (
                type(app.get("id")) is not int
                or app["id"] != config.app_id
                or app.get("client_id") != config.client_id
            ):
                refused("app_identity_mismatch")
            install = response_object(await host.get(path), 200)
            if (
                type(install.get("id")) is not int
                or install["id"] != config.installation_id
                or type(install.get("app_id")) is not int
                or install["app_id"] != config.app_id
                or install.get("target_type") not in ("User", "Organization")
                or "suspended_at" not in install
                or install["suspended_at"] is not None
            ):
                refused("installation_mismatch")
            return response_object(
                await host.post(
                    path + "/access_tokens",
                    {
                        "repository_ids": [config.destination.repository_id],
                        "permissions": dict(permissions),
                    },
                ),
                201,
            )

    async def _verify_repositories(self, minted: JsonObject, token: str) -> None:
        destination = self._config.destination
        if (
            "repository_selection" in minted
            and minted["repository_selection"] != "selected"
        ):
            refused("repository_scope_mismatch")
        if "repositories" in minted:
            if not exact_repository(minted["repositories"], destination.repository_id):
                refused("repository_scope_mismatch")
            return
        async with github_exchange(destination.api_origin, self._tls, token) as host:
            response = await host.get("/installation/repositories", {"per_page": "2"})
            visible = response_object(response, 200)
            if (
                response.has_link
                or type(visible.get("total_count")) is not int
                or visible["total_count"] != 1
                or not exact_repository(
                    visible.get("repositories"), destination.repository_id
                )
            ):
                refused("repository_scope_mismatch")

    async def _issued(
        self,
        request: AppCredentialRequest,
        caller_scope: SecretScope,
        parent: SecretScope,
        verification: SecretScope,
        ctx: ProtocolContext,
    ) -> ChangeRequestGrant:
        permissions = api_permissions(request.role)
        minted = await self._mint(permissions, parent, ctx)
        if minted.get("permissions") != permissions:
            refused("permission_scope_mismatch")
        expiry = valid_expiry(minted.get("expires_at"), ctx.clock.now())
        token = minted.get("token")
        if not (
            isinstance(token, str)
            and 0 < len(token) <= MAX_TRANSFER_BYTES
            and BEARER.fullmatch(token) is not None
        ):
            refused("token_value_invalid")
        observed_token = verification.admit(token)
        await self._verify_repositories(minted, observed_token.reveal())
        self._admit(request, caller_scope, ctx)
        if expiry <= ctx.clock.now() or useful_seconds(ctx, request.role) <= 0:
            refused("issuance_expired")
        source = request.credential
        rights = CredentialRights(
            resource_scope="repository",
            native_permissions=tuple(
                f"{name}:{value}" for name, value in permissions.items()
            ),
            operations=frozenset({api_operation(request.role)}),
        )
        verified = IssuedCredential.model_validate(
            {
                "binding": source.binding,
                "binding_revision": source.binding_revision,
                "credential_mode": source.credential_mode,
                "value": observed_token,
                "principal": self._principal,
                "rights": rights,
                "expires_at": expiry,
                "replacement": "reissue",
                "revocation": "native",
            },
            context={TRANSFER_SCOPE_KEY: verification},
        )
        issued = verified.model_copy(
            update={"value": caller_scope.admit(observed_token.reveal())}
        )
        return ChangeRequestGrant(
            source,
            issued,
            ctx.namespace_id,
            request.dispatcher_id,
            str(self._config.destination.repository_id),
            caller_scope,
        )

    async def issue(
        self,
        request: AppCredentialRequest,
        caller_scope: SecretScope,
        ctx: ProtocolContext,
    ) -> ChangeRequestGrant:
        outcome = "refused"
        try:
            self._admit(request, caller_scope, ctx)
            seconds = useful_seconds(ctx, request.role)
            if seconds <= 0:
                refused("issuance_expired")
            with ExitStack() as scopes:
                parent = self._private_scope(caller_scope)
                scopes.callback(parent.release)
                verification = self._private_scope(caller_scope, parent)
                scopes.callback(verification.release)
                async with asyncio.timeout(seconds):
                    issued = await self._issued(
                        request, caller_scope, parent, verification, ctx
                    )
                outcome = "issued"
                return issued
        except (
            GitHubTransportError,
            TimeoutError,
            SecretAdmissionError,
            SecretRevealRefused,
        ):
            refused("issuer_unavailable")
        finally:
            ctx.logger.emit(CREDENTIAL_ISSUANCE, outcome=outcome)
