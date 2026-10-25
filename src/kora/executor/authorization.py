from typing import Literal

from kora.executor.failures import Phase, misconfigured
from kora.ir import PrincipalRef
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.identity import GrantBinding, Identifier
from kora.protocols.runtime import PublicationAuthorization
from kora.protocols.vcs import (
    Binding,
    BindingRevision,
    CredentialMode,
    CredentialRequest,
    IssuedCredential,
    PreparedPublication,
    Vcs,
)

AUTHORIZATION_MISMATCH = "publication_authorization_mismatch"
CREDENTIAL_MISMATCH = "credential_binding_mismatch"
SLICE_ONE_MODE: CredentialMode = "local_credential_free"


class TrustedBinding(BoundaryModel):
    binding: Binding
    binding_revision: BindingRevision
    credential_mode: CredentialMode
    principal: PrincipalRef

    def tuple(self) -> tuple[str, int, str]:
        return self.binding, self.binding_revision, self.credential_mode


class PinnedPublication(BoundaryModel):
    attempt_id: Identifier
    execution_domain_id: Identifier
    output: Identifier
    binding: TrustedBinding


def require_supported(binding: TrustedBinding, phase: Phase) -> None:
    if binding.credential_mode != SLICE_ONE_MODE:
        misconfigured("credential_mode_unsupported", phase)


def credential_request(
    binding: TrustedBinding,
    repo: str,
    purpose: Literal["checkout", "publish"],
    attempt_id: str,
    prepared: PreparedPublication | None,
) -> CredentialRequest:
    return CredentialRequest(
        binding=binding.binding,
        binding_revision=binding.binding_revision,
        credential_mode=binding.credential_mode,
        repo=repo,
        purpose=purpose,
        attempt_id=attempt_id,
        operation_id=None if prepared is None else prepared.operation_id,
        prepared_digest=None if prepared is None else prepared.prepared_digest,
        destination_digest=None if prepared is None else prepared.destination_digest,
    )


def issued_matches(
    request: CredentialRequest, issued: IssuedCredential, declared: PrincipalRef
) -> bool:
    requested = (request.binding, request.binding_revision, request.credential_mode)
    returned = (issued.binding, issued.binding_revision, issued.credential_mode)
    return requested == returned and issued.principal.identity() == (
        declared.identity()
    )


async def acquire(
    vcs: Vcs,
    request: CredentialRequest,
    declared: PrincipalRef,
    ctx: ProtocolContext,
) -> IssuedCredential:
    issued = await vcs.acquire_credential(request, ctx)
    if not issued_matches(request, issued, declared):
        misconfigured(CREDENTIAL_MISMATCH, Phase.ACQUIRE)
    return issued


def authorization_matches(
    authorization: PublicationAuthorization,
    pinned: PinnedPublication,
    publication: PreparedPublication,
    grant: GrantBinding,
) -> bool:
    binding = pinned.binding
    return (
        authorization.attempt_id == pinned.attempt_id == publication.attempt_id
        and authorization.execution_domain_id
        == pinned.execution_domain_id
        == grant.execution_domain_id
        and authorization.dispatcher_id == grant.dispatcher_id
        and authorization.operation_id == publication.operation_id
        and authorization.prepared_digest == publication.prepared_digest
        and authorization.destination_digest == publication.destination_digest
        and (
            authorization.binding,
            authorization.binding_revision,
            authorization.credential_mode,
        )
        == binding.tuple()
        and authorization.principal.identity() == binding.principal.identity()
        and authorization.credential_mode == SLICE_ONE_MODE
        and authorization.credential is None
    )


def check_authorization(
    authorization: PublicationAuthorization,
    pinned: PinnedPublication,
    publication: PreparedPublication,
    grant: GrantBinding,
    deadline_due: bool,
) -> None:
    if deadline_due or not authorization_matches(
        authorization, pinned, publication, grant
    ):
        misconfigured(AUTHORIZATION_MISMATCH, Phase.PUBLISH)
