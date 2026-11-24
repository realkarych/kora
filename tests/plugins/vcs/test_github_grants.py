import asyncio
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, context_for, utc_now
from kora.ir import PrincipalRef, VcsRef
from kora.observability import LoggingSettings
from kora.plugins.vcs.git.github import GitHubChangeRequests
from kora.plugins.vcs.git.github_model import GitHubConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import TRANSFER_SCOPE_KEY, SecretScope
from kora.protocols.time import SystemClock
from kora.protocols.vcs import (
    ChangeRequestCall,
    ChangeRequestDispatch,
    ChangeRequestGrant,
    ChangeRequestIntent,
    ChangeRequestResult,
    ChangeRequestSearch,
    CredentialRequest,
    CredentialRights,
    IssuedCredential,
)
from tests.support.https_fixture import Body, create_authority, https_server

FIXED_TIME = datetime(2026, 11, 24, 12, tzinfo=UTC)
CASE_NAME = "github-grant"
ATTEMPT = "github-grant-attempt"
DISPATCHER = "github-grant-dispatcher"
BEARER = "ghs-control-plane-fixture"
BadGrant = Literal[
    "repository",
    "namespace",
    "dispatcher",
    "purpose",
    "rights",
    "expiry",
    "registration",
    "local_publication",
]


class FrozenClock(SystemClock):
    def now(self) -> datetime:
        return FIXED_TIME


def host_configuration(origin: str, certificate: Path) -> GitHubConfiguration:
    return GitHubConfiguration(
        repo="logical-repository",
        binding="code-host-binding",
        binding_revision=2,
        repository_id=51,
        owner="owner",
        repository="repository",
        base_branch="main",
        provider_instance="github",
        bot_account_id=52,
        api_origin=origin,
        ca_file=str(certificate),
    )


def original_call(
    capture: Capture, configuration: GitHubConfiguration
) -> tuple[ProtocolContext, ChangeRequestCall]:
    ctx = replace(context_for(capture), attempt_id=ATTEMPT, clock=FrozenClock())
    repo_id = str(configuration.repository_id)
    actor = PrincipalRef(
        provider_instance=configuration.provider_instance,
        stable_id=f"account:{configuration.bot_account_id}",
        kind="app",
        display_name="fixture-app[bot]",
    )
    published = VcsRef.model_validate(
        {
            "repo": configuration.repo,
            "branch": "kora/branch",
            "commit": "b" * 40,
            "base_commit": "a" * 40,
            "changed_paths": ["change.txt"],
            "diff_stat": "one file",
            "pushed_by": actor.model_dump(),
        }
    )
    request = CredentialRequest(
        binding=configuration.binding,
        binding_revision=configuration.binding_revision,
        credential_mode="credential_required",
        repo=configuration.repo,
        purpose="api",
        attempt_id=ATTEMPT,
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )
    rights = CredentialRights(
        resource_scope="repository",
        native_permissions=("metadata:read", "pull_requests:read"),
        operations=frozenset({"read"}),
    )
    issued = IssuedCredential.model_validate(
        {
            "binding": request.binding,
            "binding_revision": request.binding_revision,
            "credential_mode": request.credential_mode,
            "value": capture.scope.admit(BEARER),
            "principal": actor,
            "rights": rights,
            "expires_at": ctx.clock.now() + timedelta(minutes=1),
            "replacement": "reissue",
            "revocation": "native",
        },
        context={TRANSFER_SCOPE_KEY: capture.scope},
    )
    intent = ChangeRequestIntent(
        request.binding, request.binding_revision, repo_id, "main", published
    )
    grant = ChangeRequestGrant(
        request, issued, ctx.namespace_id, DISPATCHER, repo_id, capture.scope
    )
    return ctx, ChangeRequestCall(intent, grant, DISPATCHER)


def altered_call(
    call: ChangeRequestCall, capture: Capture, invalid: BadGrant
) -> ChangeRequestCall:
    grant = call.grant
    match invalid:
        case "repository":
            changed = replace(grant, repository_id="900")
        case "namespace":
            changed = replace(grant, namespace_id="other")
        case "dispatcher":
            changed = replace(grant, dispatcher_id="another-dispatcher")
        case "purpose":
            request = grant.request.model_copy(update={"purpose": "checkout"})
            changed = replace(grant, request=request)
        case "rights":
            excessive = CredentialRights(
                resource_scope="repository",
                native_permissions=("metadata:read", "pull_requests:write"),
                operations=frozenset({"change_requests"}),
            )
            issued = grant.issued.model_copy(update={"rights": excessive})
            changed = replace(grant, issued=issued)
        case "expiry":
            issued = grant.issued.model_copy(update={"expires_at": FIXED_TIME})
            changed = replace(grant, issued=issued)
        case "registration":
            changed = replace(grant, scope=SecretScope(capture.diagnostics))
        case "local_publication":
            local = call.intent.published.pushed_by.model_copy(
                update={"kind": "local_service"}
            )
            published = call.intent.published.model_copy(update={"pushed_by": local})
            return replace(call, intent=replace(call.intent, published=published))
    return replace(call, grant=changed)


@pytest.mark.parametrize(
    "invalid",
    [
        "repository",
        "namespace",
        "dispatcher",
        "purpose",
        "rights",
        "expiry",
        "registration",
        "local_publication",
    ],
)
def test_github_refuses_mismatched_grants_before_network(
    tmp_path: Path, invalid: BadGrant
) -> None:
    authority = create_authority(tmp_path)
    with (
        https_server(authority, [Body(200, b"[]")]) as host,
        captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), CASE_NAME, utc_now
        ) as capture,
    ):
        config = host_configuration(host.url(""), authority.bundle)
        provider = GitHubChangeRequests(config, config.tls())
        ctx, call = original_call(capture, config)
        rejected = ChangeRequestSearch(
            altered_call(call, capture, invalid), "historical"
        )

        with pytest.raises(KoraProtocolError) as result:
            asyncio.run(provider.lookup(rejected, ctx))

        assert result.value.code == "vcs_binding_mismatch"
        assert host.requests == []


def test_github_historical_lookup_can_read_after_attempt_expiry(tmp_path: Path) -> None:
    authority = create_authority(tmp_path)
    with (
        https_server(authority, [Body(200, b'{"id":51}'), Body(200, b"[]")]) as host,
        captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), CASE_NAME, utc_now
        ) as capture,
    ):
        config = host_configuration(host.url(""), authority.bundle)
        provider = GitHubChangeRequests(config, config.tls())
        ctx, call = original_call(capture, config)
        expired = ExecutionDeadline(
            FIXED_TIME - timedelta(seconds=1),
            "attempt",
            ATTEMPT,
            timedelta(),
            DEADLINE_GRACE,
        )

        found = asyncio.run(
            provider.lookup(
                ChangeRequestSearch(call, "historical"), replace(ctx, deadline=expired)
            )
        )

        assert found == ChangeRequestResult("unresolved")
        assert len(host.requests) == 2
        assert all(request.authorization for request in host.requests)


def test_github_tls_does_not_create_an_ambient_key_log(tmp_path: Path) -> None:
    authority = create_authority(tmp_path)
    config = host_configuration("https://127.0.0.1:4443", authority.bundle)
    keylog = tmp_path / "tls-session-keys.txt"
    program = (
        "import sys\n"
        "from kora.plugins.vcs.git.github_model import GitHubConfiguration\n"
        "GitHubConfiguration.model_validate_json(sys.argv[1]).tls()\n"
    )

    async def initialize() -> int:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            config.model_dump_json(),
            env={**os.environ, "SSLKEYLOGFILE": str(keylog)},
        )
        try:
            return await asyncio.wait_for(child.wait(), 10)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    result = asyncio.run(initialize())

    assert result == 0
    assert not keylog.exists()


def test_github_deadline_preserves_201_when_its_body_stalls(tmp_path: Path) -> None:
    authority = create_authority(tmp_path)

    async def exercise(capture: Capture) -> tuple[ChangeRequestResult, int]:
        stopped = asyncio.Event()
        posts = 0

        async def serve(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal posts
            posted = False
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                posted = header.startswith(b"POST ")
                if posted:
                    posts += 1
                    writer.write(
                        b"HTTP/1.1 201 Created\r\nContent-Length: 128\r\n\r\n{"
                    )
                    await writer.drain()
                    await reader.read()
                else:
                    writer.write(
                        b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n{"id":51}'
                    )
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                if posted:
                    stopped.set()

        server = await asyncio.start_server(
            serve, "127.0.0.1", 0, ssl=authority.context
        )
        port = server.sockets[0].getsockname()[1]
        config = host_configuration(f"https://127.0.0.1:{port}", authority.bundle)
        provider = GitHubChangeRequests(config, config.tls())
        ctx, call = original_call(capture, config)
        rights = CredentialRights(
            resource_scope="repository",
            native_permissions=("metadata:read", "pull_requests:write"),
            operations=frozenset({"change_requests"}),
        )
        issued = call.grant.issued.model_copy(update={"rights": rights})
        call = replace(call, grant=replace(call.grant, issued=issued))
        deadline = ExecutionDeadline(
            FIXED_TIME + timedelta(seconds=2),
            "attempt",
            ATTEMPT,
            timedelta(),
            DEADLINE_GRACE,
        )

        async with asyncio.timeout(10), server:
            found = await provider.create(
                ChangeRequestDispatch(call, "fixture title", "fixture body"),
                replace(ctx, deadline=deadline),
            )
            await stopped.wait()
        return found, posts

    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), CASE_NAME, utc_now
    ) as capture:
        found, posts = asyncio.run(exercise(capture))

    assert found == ChangeRequestResult("unresolved", acknowledged_create=True)
    assert posts == 1
