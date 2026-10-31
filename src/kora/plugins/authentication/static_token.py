import re
from hashlib import sha256
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.authentication import (
    AuthenticationCapabilities,
    AuthenticationContext,
    Principal,
    RequestInfo,
)
from kora.protocols.boundary import BoundaryModel
from kora.protocols.common import is_identifier
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Instant, utc_instant
from kora.protocols.metadata import ProtocolName, ProviderMetadata

if TYPE_CHECKING:
    from kora.db.connection import Database
    from kora.db.reads import TokenRow

BEARER: Final[re.Pattern[str]] = re.compile(
    r"(?i:Bearer) +([A-Za-z0-9._~+/-]+=*)", re.ASCII
)
CONFIG_SCHEMA: Final[JsonObject] = {"type": "object", "additionalProperties": False}
COMPONENT: Final[str] = CATALOG.register_component("authentication.static_token")
OUTCOME: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "authentication.static_token.outcome",
        COMPONENT,
        Level.DEBUG,
        "an authentication operation finished",
        {"operation": FieldKind.STRING, "outcome": FieldKind.STRING},
    )
)


class StoredAuthority(BoundaryModel):
    principal: Principal
    expires_at: Instant | None


def credential_digest(request: RequestInfo) -> str | None:
    header = request.authorization
    match = None if header is None else BEARER.fullmatch(header)
    return None if match is None else sha256(match[1].encode("ascii")).hexdigest()


def checked_authority(row: "TokenRow") -> StoredAuthority | None:
    try:
        return StoredAuthority.model_validate(
            {
                "principal": {
                    "token_id": row.id,
                    "kind": row.kind,
                    "owner": row.owner,
                    "namespaces": row.namespaces,
                    "approve": row.approve,
                    "platform": row.platform,
                    "runtime": row.runtime,
                },
                "expires_at": row.expires_at,
            }
        )
    except ValidationError:
        return None


def refuse(ctx: AuthenticationContext, operation: str, code: str) -> KoraProtocolError:
    ctx.logger.emit(OUTCOME, operation=operation, outcome=code)
    messages = {
        "authentication_rejected": "Authentication is required.",
        "authentication_unavailable": "Authentication is unavailable.",
        "authentication_failed": "Authentication failed.",
    }
    return KoraProtocolError(
        code, messages[code], code == "authentication_unavailable", {}
    )


class StaticTokenAuthenticator:
    name: str = "static_token"

    def __init__(self, configuration: JsonObject, database: "Database") -> None:
        from kora.db.tokens import authentication_errors

        if type(configuration) is not dict or configuration:
            raise KoraProtocolError(
                "authentication_configuration_invalid",
                "The authentication configuration is invalid.",
                False,
                {},
            )
        self.config_schema: JsonObject = dict(CONFIG_SCHEMA)
        self._database: Database = database
        self._errors: tuple[type[Exception], ...] = authentication_errors(database)

    def capabilities(self) -> AuthenticationCapabilities:
        return AuthenticationCapabilities()

    async def authenticate(
        self, request: RequestInfo, ctx: AuthenticationContext
    ) -> Principal:
        from kora.db.reads import token_by_digest
        from kora.db.tokens import REVOKED, authentication_unavailable

        digest = credential_digest(request)
        if digest is None:
            raise refuse(ctx, "authenticate", "authentication_rejected")
        failure: str | None = None
        row: TokenRow | None = None
        try:
            async with self._database.read_transaction() as connection:
                row = await token_by_digest(connection, digest)
        except self._errors as error:
            failure = (
                "authentication_unavailable"
                if authentication_unavailable(error)
                else "authentication_failed"
            )
        if failure is not None:
            raise refuse(ctx, "authenticate", failure)
        if row is None:
            raise refuse(ctx, "authenticate", "authentication_rejected")
        authority = checked_authority(row)
        if authority is None:
            raise refuse(ctx, "authenticate", "authentication_failed")
        if authority.expires_at is not None and (
            authority.expires_at == REVOKED
            or utc_instant(ctx.clock.now()) >= authority.expires_at
        ):
            raise refuse(ctx, "authenticate", "authentication_rejected")
        ctx.logger.emit(OUTCOME, operation="authenticate", outcome="accepted")
        return authority.principal

    async def revoke(self, token_id: str, ctx: AuthenticationContext) -> None:
        from kora.db.tokens import authentication_unavailable, revoke_token

        if type(token_id) is not str or not is_identifier(token_id):
            raise refuse(ctx, "revoke", "authentication_failed")
        failure: str | None = None
        try:
            async with self._database.transaction() as connection:
                await revoke_token(connection, token_id)
        except self._errors as error:
            failure = (
                "authentication_unavailable"
                if authentication_unavailable(error)
                else "authentication_failed"
            )
        if failure is not None:
            raise refuse(ctx, "revoke", failure)
        ctx.logger.emit(OUTCOME, operation="revoke", outcome="completed")


def create_provider(
    configuration: JsonObject, database: "Database"
) -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator(configuration, database)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.AUTHENTICATION, "static_token", create_provider, CONFIG_SCHEMA, "1"
    )
