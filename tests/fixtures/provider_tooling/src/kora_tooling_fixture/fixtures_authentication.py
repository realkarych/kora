from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import insert, update

from kora.conformance.authentication import (
    AuthenticationCase,
    AuthenticationConformanceInputs,
)
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.db.connection import Database, parse_database_url
from kora.db.migrations import upgrade_database
from kora.db.tables import api_token
from kora.ir.json import JsonObject
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import Authenticator, Principal, RequestInfo
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime

NOW: datetime = datetime(2026, 10, 30, tzinfo=UTC)


async def seeded(
    database: Database,
    index: int,
    kind: str = "user",
    flags: bool = True,
    expired: bool = False,
) -> AuthenticationCase:
    token_id = f"{index:026d}"
    credential = "private-installed-authentication-credential-" + str(index)
    principal = Principal.model_validate(
        {
            "token_id": token_id,
            "kind": kind,
            "owner": "private-installed-authentication-owner",
            "namespaces": [{"namespace_id": "default", "transcripts": kind == "user"}],
            "approve": flags if kind == "user" else False,
            "platform": flags if kind == "user" else False,
            "runtime": "local" if kind == "service" else None,
        }
    )
    async with database.transaction() as connection:
        await connection.execute(
            insert(api_token).values(
                id=token_id,
                digest=sha256(credential.encode()).hexdigest(),
                expires_at=datetime(1960, 1, 1, tzinfo=UTC) if expired else None,
                created_at=NOW,
                updated_at=NOW,
                **{
                    key: value
                    for key, value in principal.model_dump(mode="json").items()
                    if key != "token_id"
                },
            )
        )
    return AuthenticationCase(RequestInfo("Bearer " + credential), principal)


class Adapter:
    def __init__(self) -> None:
        self.database: Database | None = None
        self.exercises: AuthenticationConformanceInputs

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        url = fixture_data.get(
            "database_url",
            "sqlite+aiosqlite:///" + str(Path.cwd() / "authentication.db"),
        )
        assert isinstance(url, str)
        return await self.open(url)

    async def open(self, url: str) -> PreparedConformance:
        settings = parse_database_url(url)
        await upgrade_database(settings)
        database = Database(settings)
        prepared = False
        try:
            user = await seeded(database, 1)
            mcp = await seeded(database, 2, flags=False)
            service = await seeded(database, 3, "service")
            changing = await seeded(database, 4)
            revocable = await seeded(database, 5)
            expired = await seeded(database, 6, expired=True)
            unexpected = await seeded(database, 7)
            async with database.transaction() as connection:
                await connection.execute(
                    update(api_token)
                    .where(api_token.c.id == unexpected.principal.token_id)
                    .values(kind="private-installed-corrupt-kind")
                )

            async def change() -> Principal:
                async with database.transaction() as connection:
                    await connection.execute(
                        update(api_token)
                        .where(api_token.c.id == changing.principal.token_id)
                        .values(namespaces=[], approve=False, platform=False)
                    )
                return Principal.model_validate(
                    {
                        **changing.principal.model_dump(),
                        "namespaces": (),
                        "approve": False,
                        "platform": False,
                    }
                )

            async def failure(
                provider: Authenticator,
                configuration: JsonObject,
                ctx: ProtocolContext,
                marker: str,
            ) -> object:
                return await provider.authenticate(RequestInfo("Bearer " + marker), ctx)

            async def effect(
                provider: Authenticator, configuration: JsonObject, ctx: ProtocolContext
            ) -> None:
                assert type(provider) is StaticTokenAuthenticator
                assert configuration == {}
                assert await provider.authenticate(user.request, ctx) == user.principal

            self.exercises = AuthenticationConformanceInputs(
                (user, mcp, service),
                (
                    RequestInfo(None),
                    RequestInfo("Bearer malformed value"),
                    RequestInfo("Bearer unknown"),
                    expired.request,
                ),
                changing,
                change,
                revocable,
                unexpected.request,
                database.close,
                failure,
                effect,
            )
            self.database = database
            prepared = True
            return self
        finally:
            if not prepared:
                await database.close()

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        assert self.database is not None
        return ConstructorArguments((config, self.database))

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or type(instance) is StaticTokenAuthenticator
        if self.database is not None:
            await self.database.close()


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
