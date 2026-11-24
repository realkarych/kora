import asyncio
import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kora.conformance.capture import Capture, captured
from kora.conformance.common import CONFORMANCE_COMPONENT, NAMESPACE_ID, context_for
from kora.ir import PrincipalRef, VcsRef
from kora.observability import Level, LoggingSettings
from kora.plugins.secrets.file import FileSecretsConfiguration, FileSecretsProvider
from kora.plugins.vcs.git.github import GitHubChangeRequests
from kora.plugins.vcs.git.github_app import (
    AppCredentialRequest,
    GitHubAppConfiguration,
    GitHubAppIssuer,
)
from kora.plugins.vcs.git.github_model import Access, GitHubConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.secret_value import SecretRevealRefused, SecretScope
from kora.protocols.time import SystemClock
from kora.protocols.vcs import (
    ChangeRequestCall,
    ChangeRequestDispatch,
    ChangeRequestGrant,
    ChangeRequestIdentity,
    ChangeRequestIntent,
    ChangeRequestResult,
    ChangeRequestSearch,
    CredentialRequest,
)
from tests.plugins.secrets.file_support import private_directory, write_secret
from tests.support.github_app_host import (
    ACCESS_PATH,
    APP_EXCHANGE,
    APP_ID,
    APP_PATH,
    CLIENT_ID,
    FIXTURE_TIME,
    INSTALLATION_ID,
    INSTALLATION_PATH,
    MINT_PATH,
    TOKEN_EXPIRY,
    AppHost,
    AppPullRequests,
    HostCase,
    app_host,
)
from tests.support.github_host import (
    BOT_LOGIN,
    HEAD_BRANCH,
    HOST_TEXT_MARKER,
    NUMBER,
    REPOSITORY_ID,
    REQUEST_BODY,
    REQUEST_TITLE,
    ChangeRequestCase,
    PublishedGit,
    Role,
    host_configuration,
    published_git,
)
from tests.support.https_fixture import Authority, create_authority

ATTEMPT = "controlled-app-attempt"
DISPATCHER = "controlled-app-dispatcher"
KEY_NAMESPACE = "controlled-app-signing"
KEY_NAME = "APP_KEY"
KEY_REFERENCE = "secret://file/" + KEY_NAME
CALLER_MARKER = "caller-scope-fixture-sentinel"
ISSUER_EVENT = "vcs.git.github_app_credential"
Refusal = Literal[
    "binding",
    "revision",
    "repository",
    "purpose",
    "mode",
    "attempt",
    "role",
    "namespace",
    "unsealed",
    "dispatcher",
    "admission",
    "scope",
    "released",
    "deadline",
]


@dataclass
class FixtureClock(SystemClock):
    wall: datetime = FIXTURE_TIME

    def now(self) -> datetime:
        return self.wall


def deadline(at: datetime) -> ExecutionDeadline:
    return ExecutionDeadline(at, "attempt", ATTEMPT, timedelta(), DEADLINE_GRACE)


@dataclass
class IssuerFixture:
    capture: Capture
    authority: Authority
    signing_key: rsa.RSAPublicKey
    pem: bytes
    key_directory: Path
    clock: FixtureClock = field(default_factory=FixtureClock)
    scopes: list[SecretScope] = field(default_factory=list[SecretScope])

    def private_scope(self) -> SecretScope:
        scope = SecretScope(self.capture.diagnostics)
        self.scopes.append(scope)
        return scope

    def caller_scope(self) -> SecretScope:
        return SecretScope(self.capture.diagnostics)

    def context(self, caller: SecretScope) -> ProtocolContext:
        logger = self.capture.diagnostics.logger("vcs.git")
        return replace(
            context_for(self.capture),
            attempt_id=ATTEMPT,
            clock=self.clock,
            logger=logger.bind(namespace_id=NAMESPACE_ID, attempt_id=ATTEMPT),
            secret_admission=caller.admission(),
            deadline=deadline(FIXTURE_TIME + timedelta(minutes=3)),
        )

    def configured(self, host: AppHost) -> GitHubAppConfiguration:
        destination = GitHubConfiguration.model_validate(
            host_configuration(host.origin, self.authority.bundle)
        )
        return GitHubAppConfiguration(
            destination=destination,
            app_id=APP_ID,
            client_id=CLIENT_ID,
            installation_id=INSTALLATION_ID,
            namespace_id=NAMESPACE_ID,
            key_namespace_id=KEY_NAMESPACE,
            private_key_reference=KEY_REFERENCE,
            bot_display_name=BOT_LOGIN,
        )

    def issuer(self, config: GitHubAppConfiguration) -> GitHubAppIssuer:
        keys = FileSecretsProvider(
            FileSecretsConfiguration.model_validate(
                {
                    "directory": str(self.key_directory),
                    "namespace_prefixes": {KEY_NAMESPACE: ["APP_"]},
                }
            )
        )
        return GitHubAppIssuer(config, keys, self.private_scope)

    def registered(self) -> tuple[int, int]:
        registry = self.capture.diagnostics.registry
        return registry.count(), registry.retained_bytes()


@pytest.fixture(scope="module")
def app_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def fixture(tmp_path: Path, app_key: rsa.RSAPrivateKey) -> Iterator[IssuerFixture]:
    authority = create_authority(tmp_path)
    directory = private_directory(tmp_path)
    pem = app_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    write_secret(directory, KEY_NAME, pem)
    with captured(
        CONFORMANCE_COMPONENT,
        LoggingSettings(level=Level.DEBUG),
        "github-app",
        FixtureClock().now,
    ) as capture:
        yield IssuerFixture(capture, authority, app_key.public_key(), pem, directory)


def request_for(config: GitHubAppConfiguration, role: Access) -> AppCredentialRequest:
    destination = config.destination
    return AppCredentialRequest(
        CredentialRequest(
            binding=destination.binding,
            binding_revision=destination.binding_revision,
            credential_mode="credential_required",
            repo=destination.repo,
            purpose="api",
            attempt_id=ATTEMPT,
            operation_id=None,
            prepared_digest=None,
            destination_digest=None,
        ),
        role,
        DISPATCHER,
    )


def principal_for(config: GitHubConfiguration) -> PrincipalRef:
    return PrincipalRef(
        provider_instance=config.provider_instance,
        stable_id=f"account:{config.bot_account_id}",
        kind="app",
        display_name=BOT_LOGIN,
    )


def intent_for(
    config: GitHubConfiguration, published: PublishedGit
) -> ChangeRequestIntent:
    output = VcsRef.model_validate(
        {
            "repo": config.repo,
            "branch": HEAD_BRANCH,
            "commit": published.head,
            "base_commit": published.base,
            "changed_paths": ["file.txt"],
            "diff_stat": "file.txt | 2 +-",
            "pushed_by": principal_for(config),
        }
    )
    return ChangeRequestIntent(
        config.binding,
        config.binding_revision,
        str(REPOSITORY_ID),
        config.base_branch,
        output,
    )


def assert_released(fixture: IssuerFixture, exchanges: int) -> None:
    assert len(fixture.scopes) == 2 * exchanges
    assert len({id(scope) for scope in fixture.scopes}) == len(fixture.scopes)
    assert all(scope.released for scope in fixture.scopes)
    assert all(scope.registered_count() == 0 for scope in fixture.scopes)


def assert_closed(host: AppHost) -> None:
    assert host.peers_opened == host.peers_closed == len(host.paths)


def assert_diagnostics(
    fixture: IssuerFixture, host: AppHost, outcomes: list[str]
) -> list[dict[str, object]]:
    records = fixture.capture.finish()
    relevant = [record for record in records if record.get("event") == ISSUER_EVENT]
    assert [record.get("outcome") for record in relevant] == outcomes
    assert all(record.get("attempt_id") == ATTEMPT for record in relevant)
    serialized = json.dumps(records)
    assert HOST_TEXT_MARKER not in serialized
    assert KEY_REFERENCE not in serialized
    assert all(part not in serialized for part in fixture.pem.decode().splitlines())
    assert all(assertion not in serialized for assertion in host.assertions)
    assert all(minted.token not in serialized for minted in host.minted)
    return records


@dataclass(frozen=True)
class AdapterEvidence:
    precreate: ChangeRequestResult
    created: ChangeRequestResult
    recovered: ChangeRequestResult
    repeated: ChangeRequestResult
    grants: tuple[ChangeRequestGrant, ...]


async def adapter_evidence(
    fixture: IssuerFixture, host: AppHost, published: PublishedGit
) -> AdapterEvidence:
    config = fixture.configured(host)
    issuer = fixture.issuer(config)
    adapter = GitHubChangeRequests(config.destination, config.destination.tls())
    intent = intent_for(config.destination, published)

    with ExitStack() as owners:

        async def acquire(role: Access) -> tuple[ChangeRequestCall, ProtocolContext]:
            caller = fixture.caller_scope()
            owners.callback(caller.release)
            context = fixture.context(caller)
            if role == "historical":
                context = replace(
                    context, deadline=deadline(FIXTURE_TIME - timedelta(seconds=1))
                )
            request = request_for(config, role)
            grant = await issuer.issue(request, caller, context)
            assert grant.request == request.credential
            assert grant.namespace_id == NAMESPACE_ID
            assert grant.dispatcher_id == DISPATCHER
            assert grant.scope is caller
            assert grant.repository_id == str(REPOSITORY_ID)
            assert grant.issued.value is not None
            assert grant.issued.value.belongs_to(caller)
            minted = host.minted[-1]
            assert caller.result_spellings() == (minted.token,)
            assert minted.token not in repr(grant)
            return ChangeRequestCall(intent, grant, DISPATCHER), context

        precreate, ctx = await acquire("precreate")
        before = await adapter.lookup(ChangeRequestSearch(precreate, "precreate"), ctx)
        dispatch, ctx = await acquire("create")
        created = await adapter.create(
            ChangeRequestDispatch(dispatch, REQUEST_TITLE, REQUEST_BODY), ctx
        )
        historical, ctx = await acquire("historical")
        recovered = await adapter.lookup(
            ChangeRequestSearch(historical, "historical"), ctx
        )
        repeated, ctx = await acquire("historical")
        replayed = await adapter.lookup(
            ChangeRequestSearch(repeated, "historical", NUMBER), ctx
        )
        return AdapterEvidence(
            before,
            created,
            recovered,
            replayed,
            (precreate.grant, dispatch.grant, historical.grant, repeated.grant),
        )


@pytest.mark.parametrize(
    ("publication", "response_mode"),
    [
        (ChangeRequestCase.CREATE, HostCase.EXACT),
        (ChangeRequestCase.RESPONSE_LOSS, HostCase.CHECK_REPOSITORIES),
    ],
)
def test_github_app_roles_issue_and_drive_exact_pr_creation_and_recovery(
    tmp_path: Path,
    fixture: IssuerFixture,
    publication: ChangeRequestCase,
    response_mode: HostCase,
) -> None:
    git = published_git(tmp_path)
    pulls = AppPullRequests(git, publication)
    roles: tuple[Role, ...] = ("precreate", "dispatch", "recovery", "recovery")
    baseline = fixture.registered()

    async def exercise() -> tuple[AppHost, AdapterEvidence]:
        async with app_host(
            fixture.authority, fixture.signing_key, roles, response_mode, pulls
        ) as host:
            observations = await adapter_evidence(fixture, host, git)
        return host, observations

    host, observations = asyncio.run(exercise())

    assert observations.precreate == ChangeRequestResult("absent")
    assert observations.repeated == observations.recovered
    assert observations.recovered.outcome == "observed"
    fact = observations.recovered.fact
    assert fact is not None
    assert fact.identity == ChangeRequestIdentity(str(REPOSITORY_ID), NUMBER)
    assert (fact.head, fact.base) == (git.head, git.base)
    assert fact.opener == principal_for(fixture.configured(host).destination)
    if publication is ChangeRequestCase.CREATE:
        assert observations.created == ChangeRequestResult(
            "created", (fact.identity,), fact, True
        )
    else:
        assert observations.created == ChangeRequestResult("unresolved")
    assert pulls.active.counts() == (1, 1)
    assert tuple(minted.role for minted in host.minted) == roles
    assert len({minted.token for minted in host.minted}) == len(roles)
    assert len(host.assertions) == 3 * len(roles)
    assert host.paths.count(ACCESS_PATH) == (
        len(roles) if response_mode is HostCase.CHECK_REPOSITORIES else 0
    )
    canonical_permissions = (
        {"metadata:read", "pull_requests:read", "contents:read"},
        {"metadata:read", "pull_requests:write"},
        {"metadata:read", "pull_requests:read"},
        {"metadata:read", "pull_requests:read"},
    )
    for grant, expected, operation in zip(
        observations.grants,
        canonical_permissions,
        ("read", "change_requests", "read", "read"),
        strict=True,
    ):
        rights = grant.issued.rights
        assert rights is not None
        assert rights.resource_scope == "repository"
        assert set(rights.native_permissions) == expected
        assert rights.operations == {operation}
        assert grant.issued.expires_at == TOKEN_EXPIRY
        assert grant.issued.principal == fact.opener
        assert grant.issued.replacement == "reissue"
        assert grant.issued.revocation == "native"
        assert grant.issued.value is not None
        with pytest.raises(SecretRevealRefused):
            grant.issued.value.reveal()
    assert_released(fixture, len(roles))
    assert fixture.registered() == baseline
    assert_closed(host)
    assert_diagnostics(fixture, host, ["issued"] * len(roles))


@pytest.mark.parametrize(
    ("case", "paths", "reason"),
    [
        (HostCase.WRONG_APP, [APP_PATH], "app_identity_mismatch"),
        (HostCase.WRONG_CLIENT, [APP_PATH], "app_identity_mismatch"),
        (
            HostCase.WRONG_INSTALLATION,
            [APP_PATH, INSTALLATION_PATH],
            "installation_mismatch",
        ),
        (
            HostCase.INSTALLATION_APP,
            [APP_PATH, INSTALLATION_PATH],
            "installation_mismatch",
        ),
        (HostCase.SUSPENDED, [APP_PATH, INSTALLATION_PATH], "installation_mismatch"),
        (HostCase.ENTERPRISE, [APP_PATH, INSTALLATION_PATH], "installation_mismatch"),
        (HostCase.WRONG_REPOSITORY, list(APP_EXCHANGE), "repository_scope_mismatch"),
        (HostCase.BROAD_REPOSITORIES, list(APP_EXCHANGE), "repository_scope_mismatch"),
        (HostCase.ALL_REPOSITORIES, list(APP_EXCHANGE), "repository_scope_mismatch"),
        (HostCase.MISSING_PERMISSIONS, list(APP_EXCHANGE), "permission_scope_mismatch"),
        (HostCase.EXCESS_PERMISSIONS, list(APP_EXCHANGE), "permission_scope_mismatch"),
        (HostCase.EXPIRED, list(APP_EXCHANGE), "token_expiry_invalid"),
        (HostCase.LONG_LIVED, list(APP_EXCHANGE), "token_expiry_invalid"),
        (HostCase.PAGINATED, [*APP_EXCHANGE, ACCESS_PATH], "repository_scope_mismatch"),
        (
            HostCase.BAD_ACCESS_COUNT,
            [*APP_EXCHANGE, ACCESS_PATH],
            "repository_scope_mismatch",
        ),
        (
            HostCase.WRONG_ACCESS_REPOSITORY,
            [*APP_EXCHANGE, ACCESS_PATH],
            "repository_scope_mismatch",
        ),
        (HostCase.LOST_MINT, list(APP_EXCHANGE), "issuer_unavailable"),
    ],
)
def test_github_app_refuses_host_evidence_without_changing_the_caller_scope(
    fixture: IssuerFixture, case: HostCase, paths: list[str], reason: str
) -> None:
    caller = fixture.capture.scope
    stored = caller.admit(CALLER_MARKER)
    baseline = fixture.registered()

    async def exercise() -> tuple[AppHost, KoraProtocolError]:
        async with app_host(
            fixture.authority, fixture.signing_key, ("recovery",), case
        ) as host:
            config = fixture.configured(host)
            with pytest.raises(KoraProtocolError) as raised:
                await fixture.issuer(config).issue(
                    request_for(config, "historical"), caller, fixture.context(caller)
                )
        return host, raised.value

    host, error = asyncio.run(exercise())

    assert error.code == "github_app_credential_refused"
    assert error.details == {"reason": reason}
    assert error.retryable is False
    assert host.paths == paths
    assert host.paths.count(MINT_PATH) <= 1
    assert caller.result_spellings() == (CALLER_MARKER,)
    assert stored.reveal() == CALLER_MARKER
    assert_released(fixture, 1)
    assert fixture.registered() == baseline
    assert_closed(host)
    assert_diagnostics(fixture, host, ["refused"])


def altered(
    request: AppCredentialRequest, context: ProtocolContext, bad: Refusal
) -> tuple[AppCredentialRequest, ProtocolContext]:
    match bad:
        case "binding":
            request = replace(
                request,
                credential=request.credential.model_copy(
                    update={"binding": "old-binding"}
                ),
            )
        case "revision":
            request = replace(
                request,
                credential=request.credential.model_copy(
                    update={"binding_revision": request.credential.binding_revision - 1}
                ),
            )
        case "repository":
            request = replace(
                request,
                credential=request.credential.model_copy(
                    update={"repo": "different-repository"}
                ),
            )
        case "purpose":
            request = replace(
                request,
                credential=request.credential.model_copy(
                    update={"purpose": "checkout"}
                ),
            )
        case "mode":
            request = replace(
                request,
                credential=request.credential.model_copy(
                    update={"credential_mode": "local_credential_free"}
                ),
            )
        case "attempt":
            context = replace(context, attempt_id="another-app-attempt")
        case "role":
            request = replace(request, role=cast("Access", "unadmitted"))
        case "namespace":
            context = replace(context, namespace_id="different-namespace")
        case "unsealed":
            context = replace(context, request_digest=None)
        case "dispatcher":
            request = replace(request, dispatcher_id="")
        case "admission":
            context = replace(context, secret_admission=None)
        case "deadline":
            request = replace(request, role="precreate")
            context = replace(
                context, deadline=deadline(FIXTURE_TIME - timedelta(seconds=1))
            )
        case "scope":
            pass
        case "released":
            pass
    return request, context


@pytest.mark.parametrize(
    "bad",
    [
        "binding",
        "revision",
        "repository",
        "purpose",
        "mode",
        "attempt",
        "role",
        "namespace",
        "unsealed",
        "dispatcher",
        "admission",
        "scope",
        "released",
        "deadline",
    ],
)
def test_github_app_validates_original_binding_and_role_before_resolving_keys(
    fixture: IssuerFixture, bad: Refusal
) -> None:
    caller = fixture.capture.scope
    caller.admit(CALLER_MARKER)
    supplied = fixture.caller_scope() if bad in {"scope", "released"} else caller
    context = fixture.context(supplied if bad == "released" else caller)
    if bad == "released":
        supplied.release()
    baseline = fixture.registered()

    async def exercise() -> tuple[AppHost, KoraProtocolError]:
        async with app_host(fixture.authority, fixture.signing_key, ()) as host:
            config = fixture.configured(host)
            request, changed = altered(request_for(config, "historical"), context, bad)
            with pytest.raises(KoraProtocolError) as raised:
                await fixture.issuer(config).issue(request, supplied, changed)
        return host, raised.value

    try:
        host, error = asyncio.run(exercise())
    finally:
        if supplied is not caller:
            supplied.release()

    reason = "issuance_expired" if bad == "deadline" else "binding_or_role_mismatch"
    assert error.details == {"reason": reason}
    assert host.paths == []
    assert caller.result_spellings() == (CALLER_MARKER,)
    assert fixture.registered() == baseline
    assert fixture.scopes == []
    records = assert_diagnostics(fixture, host, ["refused"])
    assert all(
        not str(record.get("event")).startswith("secrets.file.") for record in records
    )


@pytest.mark.parametrize("key_ref", ["secret://file/APP_ABSENT", KEY_REFERENCE])
def test_github_app_private_key_failure_is_content_free_and_local(
    fixture: IssuerFixture, key_ref: str
) -> None:
    write_secret(fixture.key_directory, KEY_NAME, HOST_TEXT_MARKER.encode())
    caller = fixture.capture.scope
    baseline = fixture.registered()

    async def exercise() -> tuple[AppHost, KoraProtocolError]:
        async with app_host(fixture.authority, fixture.signing_key, ()) as host:
            config = fixture.configured(host).model_copy(
                update={"private_key_reference": key_ref}
            )
            with pytest.raises(KoraProtocolError) as raised:
                await fixture.issuer(config).issue(
                    request_for(config, "historical"), caller, fixture.context(caller)
                )
        return host, raised.value

    host, error = asyncio.run(exercise())

    assert error.details == {
        "reason": (
            "signing_key_invalid"
            if key_ref == KEY_REFERENCE
            else "signing_key_unavailable"
        )
    }
    assert HOST_TEXT_MARKER not in repr(error)
    assert host.paths == []
    assert_released(fixture, 1)
    assert fixture.registered() == baseline
    assert_diagnostics(fixture, host, ["refused"])


@pytest.mark.parametrize("case", [HostCase.HOLD_MINT, HostCase.HOLD_ACCESS])
def test_github_app_cancellation_closes_the_peer_and_releases_private_scopes(
    fixture: IssuerFixture, case: HostCase
) -> None:
    caller = fixture.capture.scope
    caller.admit(CALLER_MARKER)
    baseline = fixture.registered()

    async def exercise() -> AppHost:
        async with app_host(
            fixture.authority, fixture.signing_key, ("precreate",), case
        ) as host:
            config = fixture.configured(host)
            async with asyncio.TaskGroup() as tasks:
                task = tasks.create_task(
                    fixture.issuer(config).issue(
                        request_for(config, "precreate"),
                        caller,
                        fixture.context(caller),
                    )
                )
                await host.held.wait()
                signer, validation = fixture.scopes
                assert signer is not caller
                assert validation is not caller
                assert signer is not validation
                assert fixture.pem.decode().strip() in signer.result_spellings()
                assert host.assertions[-1] in signer.result_spellings()
                token = host.minted[-1].token
                expected = (token,) if case is HostCase.HOLD_ACCESS else ()
                assert validation.result_spellings() == expected
                assert caller.result_spellings() == (CALLER_MARKER,)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await host.held_closed.wait()
        return host

    host = asyncio.run(exercise())

    assert host.paths == (
        [*APP_EXCHANGE, ACCESS_PATH]
        if case is HostCase.HOLD_ACCESS
        else list(APP_EXCHANGE)
    )
    assert caller.result_spellings() == (CALLER_MARKER,)
    assert_released(fixture, 1)
    assert fixture.registered() == baseline
    assert_closed(host)
    assert_diagnostics(fixture, host, ["refused"])


def test_github_app_rechecks_expiry_after_the_authenticated_scope_probe(
    fixture: IssuerFixture,
) -> None:
    caller = fixture.capture.scope
    caller.admit(CALLER_MARKER)
    baseline = fixture.registered()

    async def exercise() -> tuple[AppHost, KoraProtocolError]:
        async with app_host(
            fixture.authority,
            fixture.signing_key,
            ("recovery",),
            HostCase.DEFER_ACCESS,
        ) as host:
            config = fixture.configured(host)
            issuer = fixture.issuer(config)

            async def issue() -> KoraProtocolError:
                with pytest.raises(KoraProtocolError) as raised:
                    await issuer.issue(
                        request_for(config, "historical"),
                        caller,
                        fixture.context(caller),
                    )
                return raised.value

            async with asyncio.TaskGroup() as tasks:
                task = tasks.create_task(issue())
                await host.held.wait()
                fixture.clock.wall = TOKEN_EXPIRY
                host.complete_access.set()
        return host, task.result()

    host, error = asyncio.run(exercise())

    assert error.details == {"reason": "issuance_expired"}
    assert host.paths == [*APP_EXCHANGE, ACCESS_PATH]
    assert caller.result_spellings() == (CALLER_MARKER,)
    assert_released(fixture, 1)
    assert fixture.registered() == baseline
    assert_closed(host)
    assert_diagnostics(fixture, host, ["refused"])
