from collections.abc import Callable
from datetime import timedelta
from typing import Final, Literal

from kora.conformance.github import ChangeRequestCounts, ChangeRequestExercise
from kora.ir import PrincipalRef, VcsRef
from kora.plugins.vcs.git.github_model import GitHubConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.secret_value import TRANSFER_SCOPE_KEY, SecretScope
from kora.protocols.vcs import (
    ChangeRequestCall,
    ChangeRequestDispatch,
    ChangeRequestFact,
    ChangeRequestGrant,
    ChangeRequestIdentity,
    ChangeRequestIntent,
    CredentialRequest,
    CredentialRights,
    IssuedCredential,
)

HEAD_BRANCH: Final[str] = "kora/fixture/pr"
ATTEMPT: Final[str] = "fixture-publication-attempt"
DISPATCHER: Final[str] = "fixture-publication-dispatcher"
NUMBER: Final[int] = 41
EXISTING_ID: Final[int] = 928
REQUEST_TITLE: Final[str] = "private-github-presentation-title"
REQUEST_BODY: Final[str] = "private-github-presentation-body"
Role = Literal["precreate", "dispatch", "recovery"]


def opener(config: GitHubConfiguration, *, existing: bool) -> PrincipalRef:
    account = EXISTING_ID if existing else config.bot_account_id
    return PrincipalRef(
        provider_instance=config.provider_instance,
        stable_id=f"account:{account}",
        kind="user" if existing else "app",
        display_name="actual-fixture-opener" if existing else "fixture-app[bot]",
    )


def call_for(
    context: ProtocolContext,
    intent: ChangeRequestIntent,
    principal: PrincipalRef,
    case: str,
    role: Role,
    scope: SecretScope,
) -> ChangeRequestCall:
    request = CredentialRequest(
        binding=intent.binding,
        binding_revision=intent.binding_revision,
        credential_mode="credential_required",
        repo=intent.published.repo,
        purpose="api",
        attempt_id=ATTEMPT,
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )
    permissions = (
        "metadata:read",
        "pull_requests:write" if role == "dispatch" else "pull_requests:read",
    )
    if role == "precreate":
        permissions = (*permissions, "contents:read")
    operation: Literal["change_requests", "read"] = (
        "change_requests" if role == "dispatch" else "read"
    )
    issued = IssuedCredential.model_validate(
        {
            "binding": request.binding,
            "binding_revision": request.binding_revision,
            "credential_mode": request.credential_mode,
            "value": scope.admit(f"private-github-installation-token-{case}-{role}"),
            "principal": principal,
            "rights": CredentialRights(
                resource_scope="repository",
                native_permissions=permissions,
                operations=frozenset({operation}),
            ),
            "expires_at": context.clock.now() + timedelta(minutes=10),
            "replacement": "reissue",
            "revocation": "native",
        },
        context={TRANSFER_SCOPE_KEY: scope},
    )
    return ChangeRequestCall(
        intent,
        ChangeRequestGrant(
            request,
            issued,
            context.namespace_id,
            DISPATCHER,
            intent.repository_id,
            scope,
        ),
        DISPATCHER,
    )


def exercise_for(
    context: ProtocolContext,
    config: GitHubConfiguration,
    head: str,
    base: str,
    case: str,
    counts: Callable[[], ChangeRequestCounts],
    new_scope: Callable[[], SecretScope],
) -> ChangeRequestExercise:
    app = opener(config, existing=False)
    intent = ChangeRequestIntent(
        binding=config.binding,
        binding_revision=config.binding_revision,
        repository_id=str(config.repository_id),
        base_branch=config.base_branch,
        published=VcsRef.model_validate(
            {
                "repo": config.repo,
                "branch": HEAD_BRANCH,
                "commit": head,
                "base_commit": base,
                "changed_paths": ["file.txt"],
                "diff_stat": "file.txt | 2 +-",
                "pushed_by": app,
            }
        ),
    )
    existing = case == "preexisting"
    web = f"https://github.com/{config.owner}/{config.repository}"
    expected = ChangeRequestFact(
        identity=ChangeRequestIdentity(intent.repository_id, NUMBER),
        state="closed" if existing else "open",
        head=head,
        base=base,
        opener=opener(config, existing=existing),
        url=f"{web}/pull/{NUMBER}",
    )
    return ChangeRequestExercise(
        dispatch=ChangeRequestDispatch(
            call_for(context, intent, app, case, "dispatch", new_scope()),
            REQUEST_TITLE,
            REQUEST_BODY,
        ),
        precreate=call_for(context, intent, app, case, "precreate", new_scope()),
        recovery=call_for(context, intent, app, case, "recovery", new_scope()),
        expected=expected,
        counts=counts,
    )
