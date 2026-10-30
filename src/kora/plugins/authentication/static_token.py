import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, Protocol, cast

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as DatabaseTimeout

from kora.db.connection import Database, DatabaseClosedError, Dialect
from kora.db.tables import api_token
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.authentication import (
    AuthenticationCapabilities,
    Principal,
    RequestInfo,
)
from kora.protocols.boundary import BoundaryModel
from kora.protocols.common import ProtocolContext, is_identifier
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Instant, utc_instant
from kora.protocols.metadata import ProtocolName, ProviderMetadata

REVOKED: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
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
UNAVAILABLE_SQLITE: Final[frozenset[int]] = frozenset(
    (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PROTOCOL,
    )
)


class StoredAuthority(BoundaryModel):
    principal: Principal
    expires_at: Instant | None


class PostgresDriver(Protocol):
    PostgresError: type[Exception]


class PostgresAdapter(Protocol):
    asyncpg: PostgresDriver


def database_errors(database: Database) -> tuple[type[Exception], ...]:
    errors: tuple[type[Exception], ...] = (
        SQLAlchemyError,
        OSError,
        DatabaseClosedError,
    )
    if database.dialect is Dialect.POSTGRESQL:
        adapter = cast("PostgresAdapter", database.writer_engine.dialect.loaded_dbapi)
        return (*errors, adapter.asyncpg.PostgresError)
    return errors


def unavailable(error: Exception) -> bool:
    if isinstance(error, OSError | DatabaseClosedError | DatabaseTimeout):
        return True
    if isinstance(error, DBAPIError):
        if error.connection_invalidated:
            return True
        original = error.orig
    else:
        original = error
    sqlite_code: object = getattr(original, "sqlite_errorcode", None)
    sqlstate: object = getattr(original, "sqlstate", None)
    return (
        isinstance(sqlite_code, int) and sqlite_code & 255 in UNAVAILABLE_SQLITE
    ) or (
        isinstance(sqlstate, str)
        and (
            sqlstate.startswith(("08", "53")) or sqlstate in {"57P01", "57P02", "57P03"}
        )
    )


def credential_digest(request: RequestInfo) -> str | None:
    header = request.authorization
    match = None if header is None else BEARER.fullmatch(header)
    return None if match is None else sha256(match[1].encode("ascii")).hexdigest()


def checked_authority(row: Mapping[str, object]) -> StoredAuthority | None:
    try:
        return StoredAuthority.model_validate(
            {
                "principal": {
                    key: value for key, value in row.items() if key != "expires_at"
                },
                "expires_at": row["expires_at"],
            }
        )
    except ValidationError:
        return None


def refuse(ctx: ProtocolContext, operation: str, code: str) -> KoraProtocolError:
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

    def __init__(self, configuration: JsonObject, database: Database) -> None:
        if type(configuration) is not dict or configuration:
            raise KoraProtocolError(
                "authentication_configuration_invalid",
                "The authentication configuration is invalid.",
                False,
                {},
            )
        self.config_schema: JsonObject = dict(CONFIG_SCHEMA)
        self._database: Database = database
        self._errors: tuple[type[Exception], ...] = database_errors(database)

    def capabilities(self) -> AuthenticationCapabilities:
        return AuthenticationCapabilities()

    async def authenticate(
        self, request: RequestInfo, ctx: ProtocolContext
    ) -> Principal:
        digest = credential_digest(request)
        if digest is None:
            raise refuse(ctx, "authenticate", "authentication_rejected")
        failure: str | None = None
        row: Mapping[str, object] | None = None
        try:
            async with self._database.read_transaction() as connection:
                result = await connection.execute(
                    select(
                        api_token.c.id.label("token_id"),
                        api_token.c.kind,
                        api_token.c.owner,
                        api_token.c.namespaces,
                        api_token.c.approve,
                        api_token.c.platform,
                        api_token.c.runtime,
                        api_token.c.expires_at,
                    ).where(api_token.c.digest == digest)
                )
                selected = result.mappings().one_or_none()
                row = (
                    None
                    if selected is None
                    else {str(key): value for key, value in selected.items()}
                )
        except self._errors as error:
            failure = (
                "authentication_unavailable"
                if unavailable(error)
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

    async def revoke(self, token_id: str, ctx: ProtocolContext) -> None:
        if type(token_id) is not str or not is_identifier(token_id):
            raise refuse(ctx, "revoke", "authentication_failed")
        failure: str | None = None
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    update(api_token)
                    .where(
                        api_token.c.id == token_id,
                        or_(
                            api_token.c.expires_at.is_(None),
                            api_token.c.expires_at != REVOKED,
                        ),
                    )
                    .values(expires_at=REVOKED)
                )
        except self._errors as error:
            failure = (
                "authentication_unavailable"
                if unavailable(error)
                else "authentication_failed"
            )
        if failure is not None:
            raise refuse(ctx, "revoke", failure)
        ctx.logger.emit(OUTCOME, operation="revoke", outcome="completed")


def create_provider(
    configuration: JsonObject, database: Database
) -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator(configuration, database)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.AUTHENTICATION, "static_token", create_provider, CONFIG_SCHEMA, "1"
    )
