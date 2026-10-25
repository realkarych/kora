from pathlib import Path

import pytest
from pydantic import MISSING

from kora.conformance.capture import Capture
from kora.conformance.vcs import Kept, authorization, grant, publish_request, run
from kora.executor.authorization import PinnedPublication, TrustedBinding
from kora.executor.entry import PublishPermit
from kora.executor.failures import ExecutorError
from kora.ir import PrincipalRef
from kora.ir.node import ErrorClass
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import PublicationAuthorization
from kora.protocols.secret_value import TRANSFER_SCOPE_KEY
from tests.plugins.vcs.git_support import (
    configuration,
    git,
    make_world,
    mapping,
    write,
)


def test_local_publication_credential_and_principal(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"local.txt": "local\n"})
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)

    ref = outcome.ref
    assert ref is not None
    assert ref.pushed_by == world.binding().binding.principal
    assert ref.pushed_by.kind == "local_service"
    assert (ref.url, ref.opened_by, ref.commit_url, ref.compare_url) == (
        MISSING,
        MISSING,
        MISSING,
        MISSING,
    )
    assert "url" not in ref.model_dump(exclude_unset=True)


def test_configured_credential_required_binding_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    remote = tmp_path / "remote.git"
    gone = tmp_path / "gone.git"
    for repository in (remote, gone):
        git(home, "init", "-q", "--bare", str(repository))
    document = configuration(remote, gone)
    mapping(mapping(document["remotes"])["local"])["credential_mode"] = (
        "credential_required"
    )

    with pytest.raises(KoraProtocolError) as raised:
        GitVcs(GitConfiguration.model_validate(document))

    assert raised.value.code == "vcs_config_invalid"


def test_requested_credential_required_binding_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"required.txt": "required\n"})
    context = world.context()
    remote = world.remote_fixture()
    plan = run(world.provider.publish_plan(publish_request(remote, checkout), context))
    workspace = checkout.workspace()
    exported = run(world.executor.export(plan, workspace, checkout.areas, context))
    sealed = run(world.executor.seal(exported, workspace, checkout.areas, context))
    app = PrincipalRef.model_validate(
        {
            "provider_instance": "code-host",
            "stable_id": "app-1",
            "kind": "app",
            "display_name": "app",
        }
    )
    required = PinnedPublication(
        attempt_id="conformance-attempt",
        execution_domain_id="conformance-domain",
        output="vcs_ref",
        binding=TrustedBinding(
            binding="local-binding",
            binding_revision=1,
            credential_mode="credential_required",
            principal=app,
        ),
    )

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_and_inspect(
                plan,
                sealed,
                workspace,
                publish_request(remote, checkout),
                required,
                checkout.areas,
                (),
                (),
                Kept(),
                context,
            )
        )

    assert raised.value.failure_class is ErrorClass.CONFIG_ERROR
    assert raised.value.code == "credential_mode_unsupported"


def test_an_authorization_carrying_a_value_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"valued.txt": "valued\n"})
    prepared = world.prepare(checkout)
    result = prepared.result
    matching = authorization(result)
    app = PrincipalRef.model_validate(
        {
            "provider_instance": "code-host",
            "stable_id": "app-1",
            "kind": "app",
            "display_name": "app",
        }
    )
    valued = PublicationAuthorization.model_validate(
        {
            **matching.model_dump(exclude={"credential", "principal"}),
            "credential_mode": "credential_required",
            "principal": app,
            "credential": world.capture.scope.admit("publish-token-9c1d"),
        },
        context={TRANSFER_SCOPE_KEY: world.capture.scope},
    )

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.publish(
                result,
                prepared.plan,
                valued,
                grant(),
                PublishPermit(result.publication.operation_id),
                world.context(),
            )
        )

    assert raised.value.code == "publication_authorization_mismatch"
    assert world.remote_head() is None
