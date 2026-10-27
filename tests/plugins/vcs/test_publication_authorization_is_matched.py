from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Prepared, authorization, grant, run
from kora.executor.authorization import acquire, credential_request
from kora.executor.entry import Executor, PublishPermit, RemoteState
from kora.executor.failures import ExecutorError
from kora.ir import PrincipalRef
from kora.ir.node import ErrorClass
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.identity import GrantBinding
from kora.protocols.runtime import PublicationAuthorization
from kora.protocols.vcs import CredentialRequest, IssuedCredential
from tests.plugins.vcs.git_support import (
    REPO,
    ManualClock,
    World,
    configuration,
    git,
    make_world,
    mapping,
    write,
)
from tests.plugins.vcs.launchers import RecordingLauncher

Change = Callable[[PublicationAuthorization], PublicationAuthorization]


def changed(**update: object) -> Change:
    return lambda matching: matching.model_copy(update=update)


def other_principal(field: str) -> Change:
    def change(matching: PublicationAuthorization) -> PublicationAuthorization:
        principal = PrincipalRef.model_validate(
            {**matching.principal.model_dump(), field: "someone-else"}
        )
        return matching.model_copy(update={"principal": principal})

    return change


CHANGES: dict[str, Change] = {
    "binding": changed(binding="other-binding"),
    "binding_revision": changed(binding_revision=2),
    "provider_instance": other_principal("provider_instance"),
    "stable_id": other_principal("stable_id"),
    "dispatcher": changed(dispatcher_id="earlier-dispatcher"),
    "operation": changed(operation_id="another-operation"),
    "prepared_digest": changed(prepared_digest="sha256:" + "e" * 64),
    "destination_digest": changed(destination_digest="sha256:" + "f" * 64),
    "attempt": changed(attempt_id="another-attempt"),
    "domain": changed(execution_domain_id="another-domain"),
}


def prepared_world(
    tmp_path: Path, capture: Capture
) -> tuple[World, Prepared, RecordingLauncher]:
    launcher = RecordingLauncher()
    world = make_world(tmp_path, capture, launcher=launcher, clock=ManualClock())
    checkout = world.checkout()
    write(world.candidate(checkout), {"authorized.txt": "authorized\n"})
    return world, world.prepare(checkout), launcher


def due(world: World) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=world.clock.now() - timedelta(seconds=1),
        scope_kind="attempt",
        scope_id="attempt",
        clock_uncertainty=timedelta(0),
        grace=timedelta(seconds=30),
    )


def refused(
    world: World,
    prepared: Prepared,
    candidate: PublicationAuthorization,
    current: GrantBinding,
    ctx: ProtocolContext,
) -> ExecutorError:
    result = prepared.result
    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.publish(
                result,
                prepared.plan,
                candidate,
                current,
                PublishPermit(result.publication.operation_id),
                ctx,
            )
        )
    return raised.value


@pytest.mark.parametrize("name", list(CHANGES))
def test_publication_authorization_is_matched(
    tmp_path: Path, kora_capture: Capture, name: str
) -> None:
    world, prepared, launcher = prepared_world(tmp_path, kora_capture)
    candidate = CHANGES[name](authorization(prepared.result))
    current = grant()
    if name == "domain":
        current = current.model_copy(update={"execution_domain_id": "another-domain"})

    error = refused(world, prepared, candidate, current, world.context())

    assert error.code == "publication_authorization_mismatch"
    assert error.failure_class is ErrorClass.CONFIG_ERROR
    assert launcher.pushes() == 0
    assert world.remote_head() is None


def test_a_due_deadline_refuses_the_authorization(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world, prepared, launcher = prepared_world(tmp_path, kora_capture)

    error = refused(
        world,
        prepared,
        authorization(prepared.result),
        grant(),
        world.context(due(world)),
    )

    assert error.code == "publication_authorization_mismatch"
    assert launcher.pushes() == 0


def test_without_a_permit_an_unknown_outcome_stays_unknown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world, prepared, launcher = prepared_world(tmp_path, kora_capture)
    result = prepared.result

    outcome = run(
        world.executor.publish(
            result,
            prepared.plan,
            authorization(result),
            grant(),
            None,
            world.context(),
        )
    )

    assert outcome.state == "unknown"
    assert launcher.pushes() == 0


def rebound_executor(world: World, launcher: RecordingLauncher) -> Executor:
    gone = world.root / "gone.git"
    git(world.home, "init", "-q", "--bare", str(gone))
    document = configuration(world.remote, gone)
    mapping(mapping(document["remotes"])[REPO])["binding_revision"] = 2
    provider = GitVcs(GitConfiguration.model_validate(document), launcher)
    return Executor(provider.tools, launcher, provider, provider, provider)


def test_reconcile_is_read_only_after_expiry_and_binding_change(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world, prepared, launcher = prepared_world(tmp_path, kora_capture)
    absent = run(world.executor.reconcile(prepared.result, world.context(due(world))))
    world.publish(prepared)
    restarted = rebound_executor(world, launcher)

    after_change = run(restarted.reconcile(prepared.result, world.context()))

    assert absent is RemoteState.ABSENT
    assert after_change is RemoteState.EQUAL
    assert launcher.pushes() == 1


class ForeignTupleVcs(GitVcs):
    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        issued = await super().acquire_credential(request, ctx)
        return issued.model_copy(update={"binding_revision": 9})


class ForeignPrincipalVcs(GitVcs):
    async def acquire_credential(
        self, request: CredentialRequest, ctx: ProtocolContext
    ) -> IssuedCredential:
        issued = await super().acquire_credential(request, ctx)
        principal = PrincipalRef.model_validate(
            {**issued.principal.model_dump(), "stable_id": "foreign"}
        )
        return issued.model_copy(update={"principal": principal})


@pytest.mark.parametrize("provider_type", [ForeignTupleVcs, ForeignPrincipalVcs])
@pytest.mark.parametrize("purpose", ["checkout", "publish"])
def test_returned_credentials_must_match_their_request(
    tmp_path: Path,
    kora_capture: Capture,
    provider_type: type[GitVcs],
    purpose: str,
) -> None:
    world = make_world(tmp_path, kora_capture, provider_type=provider_type)
    binding = world.binding().binding
    request = credential_request(binding, REPO, "checkout", "attempt", None)
    if purpose == "publish":
        request = request.model_copy(
            update={
                "purpose": "publish",
                "operation_id": "operation",
                "prepared_digest": "sha256:" + "0" * 64,
                "destination_digest": "sha256:" + "1" * 64,
            }
        )

    with pytest.raises(ExecutorError) as raised:
        run(acquire(world.provider, request, binding.principal, world.context()))

    assert raised.value.code == "credential_binding_mismatch"
    assert raised.value.failure_class is ErrorClass.CONFIG_ERROR
