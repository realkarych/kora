import json
import os
import socket
import ssl
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Final, Literal
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from kora.ir.json import JsonObject, parse_json_object
from tests.plugins.vcs.git_support import GIT, seeded_environment

OWNER: Final[str] = "fixture-owner"
REPOSITORY: Final[str] = "fixture-repository"
API_RESOURCE: Final[str] = f"/repos/{OWNER}/{REPOSITORY}"
WEB_RESOURCE: Final[str] = f"https://github.com/{OWNER}/{REPOSITORY}"
REPOSITORY_ID: Final[int] = 619
BOT_ID: Final[int] = 704
USER_ID: Final[int] = 928
BOT_LOGIN: Final[str] = "fixture-app[bot]"
USER_LOGIN: Final[str] = "actual-fixture-opener"
BASE_BRANCH: Final[str] = "main"
HEAD_BRANCH: Final[str] = "kora/fixture/pr"
ALT_BRANCH: Final[str] = "fixture-alternate"
NUMBER: Final[int] = 41
CREDENTIAL_MARKER: Final[str] = "private-github-installation-token"
HOST_TEXT_MARKER: Final[str] = "private-github-response-content"
REQUEST_TITLE: Final[str] = "private-github-presentation-title"
REQUEST_BODY: Final[str] = "private-github-presentation-body"
REQUEST_BYTES: Final[int] = 4096
SOCKET_SECONDS: Final[float] = 5.0


class ChangeRequestCase(StrEnum):
    CREATE = "create"
    PREEXISTING = "preexisting"
    MERGED = "merged"
    RESPONSE_LOSS = "response_loss"
    MULTIPLE = "multiple"
    MALFORMED = "malformed"
    INCOMPLETE = "incomplete"
    MALFORMED_LINK = "malformed_link"
    DUPLICATE_LINK = "duplicate_link"
    MOVED_HEAD = "moved_head"
    MOVED_BASE = "moved_base"
    REFUSED = "refused"
    ECHO = "echo"
    OTHER_REPOSITORY = "other_repository"
    OTHER_REPOSITORY_AT_DISPATCH = "other_repository_at_dispatch"
    OTHER_OPENER = "other_opener"
    COMPETING_WRITER = "competing_writer"


Role = Literal["precreate", "dispatch", "recovery"]


def token(case: ChangeRequestCase, role: Role) -> str:
    return f"{CREDENTIAL_MARKER}-{case.value}-{role}"


def host_configuration(origin: str, ca_file: Path) -> JsonObject:
    return {
        "repo": "fixture-code-host",
        "binding": "github-fixture-binding",
        "binding_revision": 2,
        "repository_id": REPOSITORY_ID,
        "owner": OWNER,
        "repository": REPOSITORY,
        "base_branch": BASE_BRANCH,
        "provider_instance": "github-fixture",
        "bot_account_id": BOT_ID,
        "api_origin": origin,
        "ca_file": str(ca_file),
    }


@dataclass(frozen=True)
class PublishedGit:
    root: Path
    head: str
    base: str
    alternate: str

    def revision(self, branch: str) -> str:
        return read_ref(self.root, branch)

    def move_ref(self, branch: str, revision: str, expected: str) -> None:
        git(
            self.root,
            "--git-dir=remote.git",
            "update-ref",
            f"refs/heads/{branch}",
            revision,
            expected,
        )


def git(root: Path, *arguments: str) -> str:
    return (
        subprocess.run(
            [GIT, *arguments],
            cwd=root,
            env=seeded_environment(root),
            capture_output=True,
            check=True,
            timeout=5,
        )
        .stdout.decode("ascii")
        .strip()
    )


def read_ref(root: Path, branch: str) -> str:
    return git(root, "--git-dir=remote.git", "rev-parse", f"refs/heads/{branch}")


def published_git(root: Path) -> PublishedGit:
    git(root, "init", "-q", "--bare", "remote.git")
    git(root, "init", "-q", "seed")
    for branch, content in (
        (BASE_BRANCH, "base"),
        (HEAD_BRANCH, "published"),
        (ALT_BRANCH, "alternate"),
    ):
        (root / "seed" / "file.txt").write_text(content + "\n")
        git(root, "-C", "seed", "add", "file.txt")
        git(root, "-C", "seed", "commit", "-q", "-m", content)
        git(
            root,
            "-C",
            "seed",
            "push",
            "-q",
            str(root / "remote.git"),
            f"HEAD:refs/heads/{branch}",
        )
    head, base, alternate = (
        read_ref(root, ref) for ref in (HEAD_BRANCH, BASE_BRANCH, ALT_BRANCH)
    )
    assert len({head, base, alternate}) == 3
    return PublishedGit(root, head, base, alternate)


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    state: Literal["open", "closed", "merged"] = "open"
    login: str | None = None
    different_opener: bool = False

    def view(self) -> JsonObject:
        human = self.state != "open" or self.different_opener
        return {
            "number": self.number,
            "state": "closed" if self.state == "merged" else self.state,
            "merged_at": "2026-10-20T12:00:00Z" if self.state == "merged" else None,
            "html_url": f"{WEB_RESOURCE}/pull/{self.number}",
            "user": {
                "id": USER_ID if human else BOT_ID,
                "login": self.login or (USER_LOGIN if human else BOT_LOGIN),
                "type": "User" if human else "Bot",
            },
            "head": self.ref(HEAD_BRANCH, self.head),
            "base": self.ref(BASE_BRANCH, self.base),
        }

    @staticmethod
    def ref(branch: str, revision: str) -> JsonObject:
        return {"ref": branch, "sha": revision, "repo": {"id": REPOSITORY_ID}}


@dataclass
class ExerciseState:
    case: ChangeRequestCase
    records: list[PullRequest] = field(default_factory=list[PullRequest])
    posts: int = 0
    list_reads: int = 0
    repository_reads: list[Role] = field(default_factory=list[Role])

    def counts(self) -> tuple[int, int]:
        return self.posts, len(self.records)


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    links: tuple[str, ...] = ()


def response(
    status: int, body: JsonObject | list[JsonObject], links: tuple[str, ...] = ()
) -> Reply:
    return Reply(status, json.dumps(body).encode("utf-8"), links)


def refused(status: int) -> Reply:
    return response(status, {"message": HOST_TEXT_MARKER})


class GitHubState:
    def __init__(self, published: PublishedGit, evidence: Path | None = None) -> None:
        self.published = published
        self.evidence = evidence
        self.lock = threading.Lock()
        self.cases: dict[ChangeRequestCase, ExerciseState] = {}
        self.posts = 0
        self.competing_posts = 0
        self.dispatch_pending = threading.Event()
        self.competing_settled = threading.Event()

    def record(self, active: ExerciseState) -> None:
        if self.evidence is None:
            return
        receipt = self.evidence / f"{active.case.value}.json"
        temporary = receipt.with_suffix(".new")
        posts, requests = active.counts()
        temporary.write_text(json.dumps({"posts": posts, "pull_requests": requests}))
        os.replace(temporary, receipt)

    def request(self, authorization: str) -> tuple[ExerciseState, Role] | None:
        for case in ChangeRequestCase:
            for role in ("precreate", "dispatch", "recovery"):
                if authorization == f"Bearer {token(case, role)}":
                    if case not in self.cases:
                        self.cases[case] = self.initial_state(case)
                    return self.cases[case], role
        return None

    def initial_state(self, case: ChangeRequestCase) -> ExerciseState:
        active = ExerciseState(case)
        if case in {
            ChangeRequestCase.PREEXISTING,
            ChangeRequestCase.MERGED,
            ChangeRequestCase.MULTIPLE,
            ChangeRequestCase.INCOMPLETE,
            ChangeRequestCase.MALFORMED_LINK,
            ChangeRequestCase.DUPLICATE_LINK,
        }:
            active.records.append(
                PullRequest(
                    NUMBER,
                    self.published.head,
                    self.published.base,
                    "merged" if case is ChangeRequestCase.MERGED else "closed",
                )
            )
        if case is ChangeRequestCase.MULTIPLE:
            active.records.append(
                PullRequest(NUMBER + 1, self.published.head, self.published.base)
            )
        self.record(active)
        return active

    def get(self, path: str, authorization: str, origin: str) -> Reply:
        with self.lock:
            authenticated = self.request(authorization)
            if authenticated is None:
                return refused(401)
            active, role = authenticated
            location = urlsplit(path)
            route = unquote(location.path)
            if route == API_RESOURCE:
                active.repository_reads.append(role)
                mismatch = active.case is ChangeRequestCase.OTHER_REPOSITORY or (
                    active.case is ChangeRequestCase.OTHER_REPOSITORY_AT_DISPATCH
                    and role == "dispatch"
                )
                return response(200, {"id": REPOSITORY_ID + int(mismatch)})
            if route.startswith(API_RESOURCE + "/git/ref/heads/"):
                branch = route.removeprefix(API_RESOURCE + "/git/ref/heads/")
                if branch not in {HEAD_BRANCH, BASE_BRANCH}:
                    return refused(404)
                if role != "precreate":
                    return refused(403)
                return response(
                    200,
                    {
                        "ref": f"refs/heads/{branch}",
                        "object": {
                            "type": "commit",
                            "sha": self.published.revision(branch),
                        },
                    },
                )
            if route == API_RESOURCE + "/pulls":
                return self.list_requests(active, location.query, origin)
            if route.startswith(API_RESOURCE + "/pulls/"):
                number = route.removeprefix(API_RESOURCE + "/pulls/")
                for record in active.records:
                    if str(record.number) == number:
                        return response(200, record.view())
            return refused(404)

    def list_requests(self, active: ExerciseState, query: str, origin: str) -> Reply:
        active.list_reads += 1
        selected = parse_qs(query)
        if (
            selected.get("state") != ["all"]
            or selected.get("head") != [f"{OWNER}:{HEAD_BRANCH}"]
            or selected.get("base") != [BASE_BRANCH]
            or selected.get("per_page") != ["100"]
            or selected.get("page") != ["1"]
        ):
            return refused(422)
        if active.case is ChangeRequestCase.MALFORMED:
            return Reply(200, b"[" + HOST_TEXT_MARKER.encode("utf-8"))
        objects = [record.view() for record in active.records]
        if active.case is ChangeRequestCase.MALFORMED_LINK:
            return response(200, objects, ("malformed continuation",))
        if active.case in {
            ChangeRequestCase.INCOMPLETE,
            ChangeRequestCase.DUPLICATE_LINK,
        }:
            next_query = urlencode({**selected, "page": ["2"]}, doseq=True)
            next_url = f"{origin}{API_RESOURCE}/pulls?{next_query}"
            link = f'<{next_url}>; rel="next"'
            links = (
                (link, link)
                if active.case is ChangeRequestCase.DUPLICATE_LINK
                else (link,)
            )
            return response(200, objects, links)
        return response(200, objects)

    def post(self, path: str, authorization: str, payload: JsonObject) -> Reply | None:
        with self.lock:
            self.posts += 1
            authenticated = self.request(authorization)
            if authenticated is None:
                return refused(401)
            active, role = authenticated
            active.posts += 1
            self.record(active)
            if role != "dispatch":
                return refused(403)
            if (
                path != API_RESOURCE + "/pulls"
                or payload.get("base") != BASE_BRANCH
                or payload.get("head") not in (HEAD_BRANCH, f"{OWNER}:{HEAD_BRANCH}")
                or payload.get("title") != REQUEST_TITLE
                or payload.get("body") != REQUEST_BODY
                or active.case is ChangeRequestCase.REFUSED
            ):
                return refused(422)
            if active.case is not ChangeRequestCase.COMPETING_WRITER:
                return self.commit(active)
            self.dispatch_pending.set()

        assert self.competing_settled.wait(SOCKET_SECONDS)
        return None

    def commit(self, active: ExerciseState) -> Reply | None:
        moved = {
            ChangeRequestCase.MOVED_HEAD: (HEAD_BRANCH, self.published.head),
            ChangeRequestCase.MOVED_BASE: (BASE_BRANCH, self.published.base),
        }.get(active.case)
        if moved is not None:
            branch, original = moved
            self.published.move_ref(branch, self.published.alternate, original)
        try:
            created = PullRequest(
                NUMBER + len(active.records),
                self.published.revision(HEAD_BRANCH),
                self.published.revision(BASE_BRANCH),
                login=(
                    token(active.case, "dispatch")
                    if active.case is ChangeRequestCase.ECHO
                    else None
                ),
                different_opener=active.case is ChangeRequestCase.OTHER_OPENER,
            )
            active.records.append(created)
            self.record(active)
        finally:
            if moved is not None:
                branch, original = moved
                self.published.move_ref(branch, original, self.published.alternate)
        if active.case is ChangeRequestCase.RESPONSE_LOSS:
            return None
        return response(201, created.view())

    def create_from_competing_dispatch(self) -> None:
        assert self.dispatch_pending.wait(SOCKET_SECONDS)
        with self.lock:
            active = self.cases[ChangeRequestCase.COMPETING_WRITER]
            assert active.counts() == (1, 0)
            self.competing_posts += 1
            self.commit(active)
            self.competing_settled.set()


class GitHubHandler(BaseHTTPRequestHandler):
    @property
    def host(self) -> "GitHubServer":
        assert isinstance(self.server, GitHubServer)
        return self.server

    def do_GET(self) -> None:
        self.send_reply(
            self.host.state.get(
                self.path, self.headers.get("Authorization", ""), self.host.origin
            )
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= REQUEST_BYTES:
            self.send_reply(refused(413))
            return
        payload = parse_json_object(json.loads(self.rfile.read(length)))
        self.send_reply(
            self.host.state.post(
                self.path, self.headers.get("Authorization", ""), payload
            )
        )

    def send_reply(self, reply: Reply | None) -> None:
        if reply is None:
            self.close_connection = True
            return
        self.send_response(reply.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply.body)))
        for link in reply.links:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(reply.body)

    def log_message(self, format: str, *args: object) -> None:
        return


class GitHubServer(HTTPServer):
    def __init__(self, state: GitHubState, chain: Path) -> None:
        self.state = state
        self.failed = False
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(chain)
        super().__init__(("127.0.0.1", 0), GitHubHandler)
        self.origin = f"https://127.0.0.1:{self.server_port}"
        self.worker = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.05}
        )

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        with connection:
            connection.settimeout(SOCKET_SECONDS)
            secured = self.tls.wrap_socket(connection, server_side=True)
        return secured, address

    def handle_error(self, request: object, client_address: object) -> None:
        self.failed = True

    def start(self) -> None:
        try:
            self.worker.start()
        except RuntimeError:
            self.server_close()
            raise

    def close(self) -> None:
        self.shutdown()
        self.worker.join()
        self.server_close()
        assert not self.worker.is_alive()
        assert self.socket.fileno() == -1
        assert not self.failed


@contextmanager
def github_host(state: GitHubState, chain: Path) -> Generator[GitHubServer]:
    host = GitHubServer(state, chain)
    host.start()
    try:
        yield host
    finally:
        host.close()
