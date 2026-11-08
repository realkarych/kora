import enum
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from sqlalchemy import Table, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db import BOOTSTRAP_COMPLETED, DB_COMPONENT, DB_LOGGER_NAME
from kora.db.connection import Database, Dialect
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import api_token, bootstrap_initialization, namespace
from kora.db.types import JsonObject
from kora.observability import current_diagnostics

CREDENTIAL: re.Pattern[str] = re.compile(r"[A-Za-z0-9._~+/-]+=*", re.ASCII)
DEFAULT_NAMESPACE: str = "default"
DEFAULT_POLICY: JsonObject = {"concurrency": {"max_pending_runs": 1000}}
Clock = Callable[[], datetime]
Randomness = Callable[[int], bytes]
CredentialSource = Callable[[], str]


class BootstrapMaterialError(ValueError):
    pass


class BootstrapRefused(Exception):
    def __init__(self) -> None:
        super().__init__("the metadata registry is already occupied")


class BootstrapOutcome(enum.StrEnum):
    INITIALIZED = "initialized"
    ALREADY_INITIALIZED = "already_initialized"
    REFUSED = "refused"


@dataclass(frozen=True)
class IssuedBootstrap:
    namespace_id: str
    owner: str
    cli_token_id: str
    mcp_token_id: str
    cli_digest: str
    mcp_digest: str
    cli_credential: str
    mcp_credential: str
    at: datetime


@dataclass(frozen=True)
class BootstrapResult:
    outcome: BootstrapOutcome
    cli_credential: str | None
    mcp_credential: str | None

    def __post_init__(self) -> None:
        created = self.outcome is BootstrapOutcome.INITIALIZED
        present = self.cli_credential is not None and self.mcp_credential is not None
        if created is not present:
            raise RuntimeError("credentials belong only to a created initialization")


class TextWriter(Protocol):
    def write(self, data: str, /) -> int: ...


def default_credential() -> str:
    return secrets.token_hex(32)


def issue_bootstrap(
    clock: Clock,
    randomness: Randomness = secrets.token_bytes,
    credentials: CredentialSource = default_credential,
) -> IssuedBootstrap:
    moment = clock()
    generator = IdentifierGenerator(lambda: moment, randomness)
    namespace_id = generator.new()
    owner = generator.new()
    cli_token_id = generator.new()
    mcp_token_id = generator.new()
    if len({namespace_id, owner, cli_token_id, mcp_token_id}) != 4:
        raise BootstrapMaterialError("the bootstrap identifiers are not distinct")
    cli_credential = take_credential(credentials)
    mcp_credential = take_credential(credentials)
    if cli_credential == mcp_credential:
        raise BootstrapMaterialError("the bootstrap credentials are not distinct")
    cli_digest = digest_of(cli_credential)
    mcp_digest = digest_of(mcp_credential)
    if cli_digest == mcp_digest:
        raise BootstrapMaterialError("the bootstrap credentials are not distinct")
    return IssuedBootstrap(
        namespace_id,
        owner,
        cli_token_id,
        mcp_token_id,
        cli_digest,
        mcp_digest,
        cli_credential,
        mcp_credential,
        moment,
    )


def take_credential(source: CredentialSource) -> str:
    value = source()
    if type(value) is not str or CREDENTIAL.fullmatch(value) is None:
        raise BootstrapMaterialError("a bootstrap credential is not a bearer token")
    return value


def digest_of(credential: str) -> str:
    return sha256(credential.encode("ascii")).hexdigest()


async def initialize(
    database: Database,
    clock: Clock,
    randomness: Randomness = secrets.token_bytes,
    credentials: CredentialSource = default_credential,
) -> BootstrapResult:
    if await marker_present(database):
        return finish(BootstrapOutcome.ALREADY_INITIALIZED)
    issued = issue_bootstrap(clock, randomness, credentials)
    try:
        created = await database.run(
            lambda connection: persist_bootstrap(connection, database.dialect, issued)
        )
    except BootstrapRefused:
        return finish(BootstrapOutcome.REFUSED)
    except IntegrityError:
        if await marker_present(database):
            return finish(BootstrapOutcome.ALREADY_INITIALIZED)
        raise
    if not created:
        return finish(BootstrapOutcome.ALREADY_INITIALIZED)
    return finish(
        BootstrapOutcome.INITIALIZED, issued.cli_credential, issued.mcp_credential
    )


async def persist_bootstrap(
    connection: AsyncConnection, dialect: Dialect, issued: IssuedBootstrap
) -> bool:
    await lock_registry(connection, dialect)
    if await marker_loaded(connection):
        return False
    if await registry_occupied(connection):
        raise BootstrapRefused()
    await insert_namespace(connection, issued)
    await insert_token(
        connection,
        issued.cli_token_id,
        issued.cli_digest,
        issued,
        approve=True,
        platform=True,
    )
    await insert_token(
        connection,
        issued.mcp_token_id,
        issued.mcp_digest,
        issued,
        approve=False,
        platform=False,
    )
    await connection.execute(
        insert(bootstrap_initialization).values(
            singleton=1,
            namespace_id=issued.namespace_id,
            owner=issued.owner,
            cli_token_id=issued.cli_token_id,
            mcp_token_id=issued.mcp_token_id,
            created_at=issued.at,
            updated_at=issued.at,
        )
    )
    return True


def publish(result: BootstrapResult, writer: TextWriter) -> None:
    if result.outcome is not BootstrapOutcome.INITIALIZED:
        return
    cli_credential = result.cli_credential
    mcp_credential = result.mcp_credential
    if cli_credential is None or mcp_credential is None:
        raise RuntimeError("credentials belong only to a created initialization")
    writer.write("KORA_TOKEN=" + cli_credential + "\n")
    writer.write("KORA_MCP_TOKEN=" + mcp_credential + "\n")


def finish(
    outcome: BootstrapOutcome,
    cli_credential: str | None = None,
    mcp_credential: str | None = None,
) -> BootstrapResult:
    result = BootstrapResult(outcome, cli_credential, mcp_credential)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME).emit(
            BOOTSTRAP_COMPLETED, outcome=outcome.value
        )
    return result


async def marker_present(database: Database) -> bool:
    async with database.read_transaction() as connection:
        return await marker_loaded(connection)


async def marker_loaded(connection: AsyncConnection) -> bool:
    found = await connection.scalar(select(bootstrap_initialization.c.singleton))
    return found is not None


async def lock_registry(connection: AsyncConnection, dialect: Dialect) -> None:
    if dialect is not Dialect.POSTGRESQL:
        return
    await connection.execute(text("LOCK TABLE namespace IN SHARE ROW EXCLUSIVE MODE"))
    await connection.execute(text("LOCK TABLE api_token IN SHARE ROW EXCLUSIVE MODE"))
    await connection.execute(
        text("LOCK TABLE bootstrap_initialization IN SHARE ROW EXCLUSIVE MODE")
    )


async def registry_occupied(connection: AsyncConnection) -> bool:
    namespaces = await row_count(connection, namespace)
    tokens = await row_count(connection, api_token)
    return namespaces != 0 or tokens != 0


async def row_count(connection: AsyncConnection, table: Table) -> int:
    count = await connection.scalar(select(func.count()).select_from(table))
    if type(count) is not int:
        raise RuntimeError("the registry count is missing")
    return count


async def insert_namespace(
    connection: AsyncConnection, issued: IssuedBootstrap
) -> None:
    await connection.execute(
        insert(namespace).values(
            id=issued.namespace_id,
            name=DEFAULT_NAMESPACE,
            policy_revision=1,
            policy=DEFAULT_POLICY,
            created_at=issued.at,
            updated_at=issued.at,
        )
    )


async def insert_token(
    connection: AsyncConnection,
    token_id: str,
    digest: str,
    issued: IssuedBootstrap,
    approve: bool,
    platform: bool,
) -> None:
    await connection.execute(
        insert(api_token).values(
            id=token_id,
            kind="user",
            digest=digest,
            owner=issued.owner,
            namespaces=[{"namespace_id": issued.namespace_id, "transcripts": True}],
            approve=approve,
            platform=platform,
            runtime=None,
            expires_at=None,
            created_at=issued.at,
            updated_at=issued.at,
        )
    )
