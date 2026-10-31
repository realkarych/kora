from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import insert, update

from kora.conformance.authentication import (
    AuthenticationCase,
    AuthenticationConformanceInputs,
)
from kora.db.connection import Database
from kora.db.tables import api_token
from kora.ir.json import JsonObject
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import (
    AuthenticationContext,
    Authenticator,
    Principal,
    RequestInfo,
)
from kora.protocols.time import SystemClock
from tests.db.seed import START, Seeder


class AuthorityClock(SystemClock):
    def __init__(self, moment: datetime = START) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


async def seed_case(
    database: Database,
    name: str,
    kind: str = "user",
    expires_at: datetime | None = None,
) -> AuthenticationCase:
    credential = "private-authentication-credential-" + name
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace(name)
        token_id = seed.new_id()
        source: dict[str, object] = {
            "token_id": token_id,
            "kind": kind,
            "owner": "private-authentication-owner-" + name,
            "namespaces": [
                {"namespace_id": namespace_id, "transcripts": kind == "user"}
            ],
            "approve": kind == "user",
            "platform": kind == "user",
            "runtime": "local" if kind == "service" else None,
        }
        principal = Principal.model_validate(source)
        await connection.execute(
            insert(api_token).values(
                id=token_id,
                digest=sha256(credential.encode()).hexdigest(),
                expires_at=expires_at,
                **{
                    key: value
                    for key, value in principal.model_dump(mode="json").items()
                    if key != "token_id"
                },
                **seed.stamps(),
            )
        )
    return AuthenticationCase(RequestInfo("Bearer " + credential), principal)


async def fixture_inputs(database: Database) -> AuthenticationConformanceInputs:
    user = await seed_case(database, "user")
    service = await seed_case(database, "service", "service")
    changing = await seed_case(database, "changing")
    revocable = await seed_case(database, "revocable")
    expired = await seed_case(database, "expired", expires_at=START - timedelta(days=1))
    unexpected = await seed_case(database, "unexpected")
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == unexpected.principal.token_id)
            .values(kind="private-corrupt-kind")
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
        ctx: AuthenticationContext,
        marker: str,
    ) -> object:
        return await provider.authenticate(RequestInfo("Bearer " + marker), ctx)

    async def effect(
        provider: Authenticator, configuration: JsonObject, ctx: AuthenticationContext
    ) -> None:
        assert type(provider) is StaticTokenAuthenticator
        assert configuration == {}
        assert await provider.authenticate(user.request, ctx) == user.principal

    return AuthenticationConformanceInputs(
        (user, service),
        (
            RequestInfo(None),
            RequestInfo("Basic private-authentication-marker"),
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
