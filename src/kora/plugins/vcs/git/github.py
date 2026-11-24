import asyncio
import ssl
from dataclasses import dataclass, replace
from typing import Final
from urllib.parse import quote

from kora.ir.json import JsonValue, fits_utf8
from kora.plugins.vcs.git.github_http import (
    GitHubExchange,
    GitHubTransportError,
    github_exchange,
)
from kora.plugins.vcs.git.github_model import (
    MAX_PR_NUMBER,
    Access,
    GitHubConfiguration,
    admit_call,
    contains_registered,
    identity_of,
    repository_id,
    verify_fact,
)
from kora.plugins.vcs.local import refusal
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import (
    NANOSECONDS_PER_SECOND,
    ClockSample,
    remaining_ns,
    thresholds,
)
from kora.protocols.vcs import (
    ChangeRequestCall,
    ChangeRequestDispatch,
    ChangeRequestIdentity,
    ChangeRequestIntent,
    ChangeRequestResult,
    ChangeRequestSearch,
)

OPERATION_SECONDS: Final[float] = 15.0
PAGE_SIZE: Final[int] = 100


def useful_seconds(context: ProtocolContext, access: Access) -> float:
    if context.deadline is None or access == "historical":
        return OPERATION_SECONDS
    sample = ClockSample(context.clock.now(), context.clock.monotonic_ns())
    edge = thresholds(context.deadline, sample).interrupt_at_ns
    remaining = remaining_ns(edge, context.clock.monotonic_ns())
    return min(OPERATION_SECONDS, remaining / NANOSECONDS_PER_SECOND)


@dataclass(frozen=True)
class GitHubChangeRequests:
    config: GitHubConfiguration
    tls: ssl.SSLContext

    @property
    def repo_path(self) -> str:
        return f"/repos/{self.config.owner}/{self.config.repository}"

    @property
    def pulls_path(self) -> str:
        return f"{self.repo_path}/pulls"

    def identities(self, items: list[JsonValue]) -> tuple[ChangeRequestIdentity, ...]:
        known = (
            identity
            for item in items
            if (identity := identity_of(item, self.config.repository_id)) is not None
        )
        return tuple(dict.fromkeys(known))

    def classify(
        self, items: list[JsonValue], call: ChangeRequestCall, created: bool = False
    ) -> ChangeRequestResult:
        identities = self.identities(items)
        if not identities:
            return ChangeRequestResult("unresolved", acknowledged_create=created)
        fact = (
            verify_fact(
                items[0], identities[0], call.intent, self.config, call.grant.scope
            )
            if len(items) == 1
            else None
        )
        if fact is not None:
            if not created:
                return ChangeRequestResult("observed", identities, fact)
            principal = call.grant.issued.principal
            if (
                fact.opener.identity() == principal.identity()
                and not contains_registered(call.grant.scope, (principal.display_name,))
            ):
                return ChangeRequestResult(
                    "created", identities, replace(fact, opener=principal), True
                )
        return ChangeRequestResult("conflict", identities, acknowledged_create=created)

    async def read(
        self,
        exchange: GitHubExchange,
        request: ChangeRequestSearch,
    ) -> ChangeRequestResult:
        repo = await exchange.get(self.repo_path)
        if repo.status != 200 or repository_id(repo.value) != self.config.repository_id:
            return ChangeRequestResult("unresolved")
        if request.known_number is not None:
            found = await exchange.get(f"{self.pulls_path}/{request.known_number}")
            match = identity_of(found.value, self.config.repository_id)
            if (
                found.status != 200
                or match is None
                or match.number != request.known_number
            ):
                return ChangeRequestResult("unresolved")
            return self.classify([found.value], request.call)
        intent = request.call.intent
        query = {
            "state": "all",
            "head": f"{self.config.owner}:{intent.published.branch}",
            "base": intent.base_branch,
            "per_page": str(PAGE_SIZE),
        }
        found = await exchange.get(self.pulls_path, {**query, "page": "1"})
        if found.status != 200 or not isinstance(found.value, list):
            return ChangeRequestResult("unresolved")
        if found.link is not None or len(found.value) > PAGE_SIZE:
            return ChangeRequestResult(
                "unresolved", self.identities(found.value[:PAGE_SIZE])
            )
        if found.value:
            return self.classify(found.value, request.call)
        if request.mode == "precreate":
            return await self.check_refs(exchange, intent)
        return ChangeRequestResult("unresolved")

    async def check_refs(
        self,
        exchange: GitHubExchange,
        intent: ChangeRequestIntent,
    ) -> ChangeRequestResult:
        ref = intent.published
        for branch, commit in (
            (ref.branch, ref.commit),
            (intent.base_branch, ref.base_commit),
        ):
            value = await exchange.get(
                f"{self.repo_path}/git/ref/heads/{quote(branch, safe='/')}"
            )
            if value.status != 200 or not isinstance(value.value, dict):
                return ChangeRequestResult("unresolved")
            obj = value.value.get("object")
            if value.value.get("ref") != f"refs/heads/{branch}" or not isinstance(
                obj, dict
            ):
                return ChangeRequestResult("unresolved")
            if obj.get("type") != "commit" or obj.get("sha") != commit:
                return ChangeRequestResult("conflict")
        return ChangeRequestResult("absent")

    async def lookup(
        self,
        request: ChangeRequestSearch,
        context: ProtocolContext,
    ) -> ChangeRequestResult:
        if request.known_number is not None and (
            request.mode != "historical"
            or isinstance(request.known_number, bool)
            or not 0 < request.known_number <= MAX_PR_NUMBER
        ):
            raise refusal("vcs_request_invalid")
        token = admit_call(request.call, context, self.config, request.mode)
        seconds = useful_seconds(context, request.mode)
        if seconds <= 0:
            return ChangeRequestResult("unresolved")
        try:
            async with (
                asyncio.timeout(seconds),
                github_exchange(self.config.api_origin, self.tls, token) as exchange,
            ):
                return await self.read(exchange, request)
        except (GitHubTransportError, TimeoutError):
            return ChangeRequestResult("unresolved")

    async def create(
        self,
        request: ChangeRequestDispatch,
        context: ProtocolContext,
    ) -> ChangeRequestResult:
        call = request.call
        token = admit_call(call, context, self.config, "create")
        content = (request.title, request.body)
        if (
            not request.title
            or not fits_utf8(request.title, 512)
            or not fits_utf8(request.body, 65_536)
            or any("\x00" in text for text in content)
            or contains_registered(call.grant.scope, content)
        ):
            raise refusal("vcs_request_invalid")
        seconds = useful_seconds(context, "create")
        if seconds <= 0:
            return ChangeRequestResult("unresolved")
        intent = call.intent
        try:
            async with (
                asyncio.timeout(seconds),
                github_exchange(self.config.api_origin, self.tls, token) as exchange,
            ):
                response = await exchange.post(
                    self.pulls_path,
                    {
                        "head": intent.published.branch,
                        "base": intent.base_branch,
                        "title": request.title,
                        "body": request.body,
                    },
                )
                if response.status == 201:
                    return self.classify([response.value], call, created=True)
                return ChangeRequestResult("unresolved")
        except (GitHubTransportError, TimeoutError):
            return ChangeRequestResult("unresolved")
