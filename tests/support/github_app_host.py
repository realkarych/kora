import asyncio
import json
from base64 import urlsafe_b64decode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.parser import BytesHeaderParser
from enum import StrEnum
from http import HTTPStatus
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kora.ir.json import JsonObject, JsonValue, parse_json_object
from tests.support.github_host import (
    HOST_TEXT_MARKER,
    REPOSITORY_ID,
    REQUEST_BYTES,
    ChangeRequestCase,
    ExerciseState,
    GitHubState,
    PublishedGit,
    Reply,
    Role,
    refused,
    response,
)
from tests.support.https_fixture import Authority

APP_ID = 37
CLIENT_ID = "controlled-app-client"
INSTALLATION_ID = 53
FIXTURE_TIME = datetime(2026, 11, 24, 12, tzinfo=UTC)
TOKEN_EXPIRY = FIXTURE_TIME + timedelta(minutes=10)
APP_PATH = "/app"
INSTALLATION_PATH = f"/app/installations/{INSTALLATION_ID}"
MINT_PATH = INSTALLATION_PATH + "/access_tokens"
ACCESS_PATH = "/installation/repositories"
APP_EXCHANGE = (APP_PATH, INSTALLATION_PATH, MINT_PATH)


class HostCase(StrEnum):
    EXACT = "exact"
    CHECK_REPOSITORIES = "check_repositories"
    WRONG_APP = "wrong_app"
    WRONG_CLIENT = "wrong_client"
    WRONG_INSTALLATION = "wrong_installation"
    INSTALLATION_APP = "installation_app"
    SUSPENDED = "suspended"
    ENTERPRISE = "enterprise"
    WRONG_REPOSITORY = "wrong_repository"
    BROAD_REPOSITORIES = "broad_repositories"
    ALL_REPOSITORIES = "all_repositories"
    EXCESS_PERMISSIONS = "excess_permissions"
    MISSING_PERMISSIONS = "missing_permissions"
    EXPIRED = "expired"
    LONG_LIVED = "long_lived"
    PAGINATED = "paginated"
    BAD_ACCESS_COUNT = "bad_access_count"
    WRONG_ACCESS_REPOSITORY = "wrong_access_repository"
    LOST_MINT = "lost_mint"
    HOLD_MINT = "hold_mint"
    HOLD_ACCESS = "hold_access"
    DEFER_ACCESS = "defer_access"


def decode_segment(encoded: str) -> bytes:
    return urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def verify_assertion(authorization: str, public_key: rsa.RSAPublicKey) -> None:
    assert authorization.startswith("Bearer ")
    encoded_header, encoded_claims, signature = authorization[7:].split(".")
    public_key.verify(
        decode_segment(signature),
        f"{encoded_header}.{encoded_claims}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    header = parse_json_object(json.loads(decode_segment(encoded_header)))
    claims = parse_json_object(json.loads(decode_segment(encoded_claims)))
    assert header.get("alg") == "RS256"
    assert claims.get("iss") == CLIENT_ID
    issued_at, expires = claims.get("iat"), claims.get("exp")
    now = int(FIXTURE_TIME.timestamp())
    assert type(issued_at) is int
    assert now - 60 <= issued_at <= now
    assert type(expires) is int
    assert now < expires <= now + 600


def permissions_for(role: Role) -> JsonObject:
    permissions: JsonObject = {"metadata": "read", "pull_requests": "read"}
    if role == "precreate":
        permissions["contents"] = "read"
    if role == "dispatch":
        permissions["pull_requests"] = "write"
    return permissions


@dataclass(frozen=True)
class Minted:
    role: Role
    token: str


class AppPullRequests(GitHubState):
    def __init__(self, published: PublishedGit, case: ChangeRequestCase) -> None:
        super().__init__(published)
        self.active = self.initial_state(case)
        self.minted: list[Minted] = []

    def request(self, authorization: str) -> tuple[ExerciseState, Role] | None:
        for minted in self.minted:
            if authorization == f"Bearer {minted.token}":
                return self.active, minted.role
        return None


@dataclass(frozen=True)
class WireRequest:
    method: str
    path: str
    authorization: str
    payload: JsonObject | None

    @classmethod
    async def read(cls, reader: asyncio.StreamReader) -> "WireRequest":
        raw = await reader.readuntil(b"\r\n\r\n")
        request_line, _, headers = raw.partition(b"\r\n")
        method, path, version = request_line.decode("ascii").split()
        assert method in ("GET", "POST")
        assert version == "HTTP/1.1"
        fields = BytesHeaderParser().parsebytes(headers)
        authorization = str(fields["Authorization"])
        if method == "GET":
            return cls(method, path, authorization, None)
        size = int(str(fields["Content-Length"]))
        assert 0 < size <= REQUEST_BYTES
        payload = parse_json_object(json.loads(await reader.readexactly(size)))
        return cls(method, path, authorization, payload)


class AppHost:
    def __init__(
        self,
        public_key: rsa.RSAPublicKey,
        roles: tuple[Role, ...],
        case: HostCase,
        pulls: AppPullRequests | None,
    ) -> None:
        self.public_key = public_key
        self.roles = roles
        self.case = case
        self.pulls = pulls
        self.origin = ""
        self.paths: list[str] = []
        self.assertions: list[str] = []
        self.minted: list[Minted] = pulls.minted if pulls is not None else []
        self.held = asyncio.Event()
        self.held_closed = asyncio.Event()
        self.complete_access = asyncio.Event()
        self.peers_opened = 0
        self.peers_closed = 0

    def issue_token(self, payload: JsonObject) -> Reply | None:
        index = len(self.minted)
        role = self.roles[index]
        permissions = permissions_for(role)
        assert payload == {
            "repository_ids": [REPOSITORY_ID],
            "permissions": permissions,
        }
        token = "fixture-opaque." + "1aA_" * 123 + f".mint-{index}"
        minted = Minted(role, token)
        self.minted.append(minted)
        if self.case is HostCase.LOST_MINT:
            return None
        if self.case is HostCase.EXCESS_PERMISSIONS:
            permissions["administration"] = "read"
        if self.case is HostCase.MISSING_PERMISSIONS:
            del permissions["pull_requests"]
        expires = TOKEN_EXPIRY
        if self.case is HostCase.EXPIRED:
            expires = FIXTURE_TIME
        if self.case is HostCase.LONG_LIVED:
            expires = FIXTURE_TIME + timedelta(days=1)
        body: JsonObject = {
            "token": token,
            "expires_at": expires.isoformat(),
            "permissions": permissions,
            "repository_selection": (
                "all" if self.case is HostCase.ALL_REPOSITORIES else "selected"
            ),
            "fixture_text": HOST_TEXT_MARKER,
        }
        if self.case not in {
            HostCase.CHECK_REPOSITORIES,
            HostCase.PAGINATED,
            HostCase.BAD_ACCESS_COUNT,
            HostCase.WRONG_ACCESS_REPOSITORY,
            HostCase.HOLD_ACCESS,
            HostCase.DEFER_ACCESS,
        }:
            repositories: list[JsonValue] = [{"id": REPOSITORY_ID}]
            if self.case is HostCase.WRONG_REPOSITORY:
                repositories = [{"id": REPOSITORY_ID + 1}]
            if self.case is HostCase.BROAD_REPOSITORIES:
                repositories.append({"id": REPOSITORY_ID + 1})
            body["repositories"] = repositories
        return response(201, body)

    def check_repositories(self, request: WireRequest) -> Reply:
        assert request.method == "GET"
        assert self.minted
        assert request.authorization == f"Bearer {self.minted[-1].token}"
        repository = REPOSITORY_ID + int(self.case is HostCase.WRONG_ACCESS_REPOSITORY)
        count = 2 if self.case is HostCase.BAD_ACCESS_COUNT else 1
        links = (
            (f'<{self.origin}{ACCESS_PATH}?page=2>; rel="next"',)
            if self.case is HostCase.PAGINATED
            else ()
        )
        return response(
            200, {"total_count": count, "repositories": [{"id": repository}]}, links
        )

    def handle(self, request: WireRequest, route: str) -> Reply | None:
        self.paths.append(route)
        if route in APP_EXCHANGE:
            verify_assertion(request.authorization, self.public_key)
            self.assertions.append(request.authorization[7:])
        if route == APP_PATH:
            assert request.method == "GET"
            return response(
                200,
                {
                    "id": APP_ID + int(self.case is HostCase.WRONG_APP),
                    "client_id": "wrong-app-client"
                    if self.case is HostCase.WRONG_CLIENT
                    else CLIENT_ID,
                },
            )
        if route == INSTALLATION_PATH:
            assert request.method == "GET"
            return response(
                200,
                {
                    "id": INSTALLATION_ID
                    + int(self.case is HostCase.WRONG_INSTALLATION),
                    "app_id": APP_ID + int(self.case is HostCase.INSTALLATION_APP),
                    "target_type": "Enterprise"
                    if self.case is HostCase.ENTERPRISE
                    else "Organization",
                    "suspended_at": FIXTURE_TIME.isoformat()
                    if self.case is HostCase.SUSPENDED
                    else None,
                },
            )
        if route == MINT_PATH:
            assert request.method == "POST"
            assert request.payload is not None
            return self.issue_token(request.payload)
        if route == ACCESS_PATH:
            return self.check_repositories(request)
        if self.pulls is None:
            return refused(404)
        if request.method == "GET":
            return self.pulls.get(request.path, request.authorization, self.origin)
        assert request.payload is not None
        return self.pulls.post(request.path, request.authorization, request.payload)

    async def serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.peers_opened += 1
        held = False
        try:
            request = await WireRequest.read(reader)
            path = urlsplit(request.path).path
            reply = self.handle(request, path)
            held = (self.case, path) in {
                (HostCase.HOLD_MINT, MINT_PATH),
                (HostCase.HOLD_ACCESS, ACCESS_PATH),
            }
            if self.case is HostCase.DEFER_ACCESS and path == ACCESS_PATH:
                self.held.set()
                await self.complete_access.wait()
            if reply is not None:
                await write_reply(writer, reply, held)
                if held:
                    self.held.set()
                await reader.read()
        except ConnectionResetError:
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
            self.peers_closed += 1
            if held:
                self.held_closed.set()


async def write_reply(writer: asyncio.StreamWriter, reply: Reply, held: bool) -> None:
    status = HTTPStatus(reply.status)
    headers = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(reply.body)}\r\n"
        + "".join(f"Link: {link}\r\n" for link in reply.links)
    )
    writer.write(
        headers.encode("ascii") + b"\r\n" + (reply.body[:1] if held else reply.body)
    )
    await writer.drain()


@asynccontextmanager
async def app_host(
    authority: Authority,
    public_key: rsa.RSAPublicKey,
    roles: tuple[Role, ...],
    case: HostCase = HostCase.EXACT,
    pulls: AppPullRequests | None = None,
) -> AsyncGenerator[AppHost]:
    peer = AppHost(public_key, roles, case, pulls)
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:

        def connected(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            tasks.create_task(peer.serve(reader, writer))

        server = await asyncio.start_server(
            connected, "127.0.0.1", 0, ssl=authority.context
        )
        port = server.sockets[0].getsockname()[1]
        peer.origin = f"https://127.0.0.1:{port}"
        async with server:
            yield peer
