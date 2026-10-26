import getpass
from collections.abc import Callable
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.vcs import run
from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.errors import KoraProtocolError
from kora.protocols.vcs import CredentialRequest
from tests.plugins.vcs.git_support import (
    BINDING,
    GONE_REPO,
    REPO,
    configuration,
    git,
    make_world,
    mapping,
)

Change = Callable[[dict[str, object], Path], None]


def local(document: dict[str, object]) -> dict[str, object]:
    return mapping(mapping(document["remotes"])[REPO])


def principal(document: dict[str, object]) -> dict[str, object]:
    return mapping(local(document)["principal"])


def required_mode(document: dict[str, object], remote: Path) -> None:
    local(document)["credential_mode"] = "credential_required"


def stable_binding(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = BINDING


def stable_path(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = str(remote)


def stable_user(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = getpass.getuser()


def stable_identity(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = "kora@example.invalid"


def shared_binding(document: dict[str, object], remote: Path) -> None:
    mapping(mapping(document["remotes"])[GONE_REPO])["binding"] = BINDING


@pytest.mark.parametrize(
    "change",
    [
        required_mode,
        stable_binding,
        stable_path,
        stable_user,
        stable_identity,
        shared_binding,
    ],
)
def test_git_acquire_credential_is_local(tmp_path: Path, change: Change) -> None:
    home = tmp_path / "home"
    home.mkdir()
    remote, gone = tmp_path / "remote.git", tmp_path / "gone.git"
    for repository in (remote, gone):
        git(home, "init", "-q", "--bare", str(repository))
    document = configuration(remote, gone)
    change(document, remote)

    with pytest.raises((KoraProtocolError, ValueError)):
        GitVcs(GitConfiguration.model_validate(document))


def test_credential_lookup_is_keyed_by_repository(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    request = CredentialRequest(
        binding=BINDING,
        binding_revision=1,
        credential_mode="local_credential_free",
        repo=GONE_REPO,
        purpose="checkout",
        attempt_id="attempt",
        operation_id=None,
        prepared_digest=None,
        destination_digest=None,
    )

    with pytest.raises(KoraProtocolError) as raised:
        run(world.provider.acquire_credential(request, context_for(kora_capture)))

    assert raised.value.code == "vcs_binding_mismatch"
    assert not raised.value.retryable
