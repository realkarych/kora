import getpass
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

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


def stable_binding(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = BINDING


def stable_path(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = str(remote)


def stable_user(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = getpass.getuser()


def stable_identity(document: dict[str, object], remote: Path) -> None:
    principal(document)["stable_id"] = "kora@example.invalid"


def bare_repositories(root: Path) -> tuple[Path, Path]:
    home = root / "home"
    home.mkdir()
    remote, gone = root / "remote.git", root / "gone.git"
    for repository in (remote, gone):
        git(home, "init", "-q", "--bare", str(repository))
    return remote, gone


@pytest.mark.parametrize(
    "change", [stable_binding, stable_path, stable_user, stable_identity]
)
def test_git_acquire_credential_is_local(tmp_path: Path, change: Change) -> None:
    remote, gone = bare_repositories(tmp_path)
    document = configuration(remote, gone)
    change(document, remote)

    with pytest.raises(KoraProtocolError) as raised:
        GitVcs(GitConfiguration.model_validate(document))

    assert raised.value.code == "vcs_config_invalid"
    assert raised.value.details == {"rule": "principal_not_stable"}


def test_a_binding_shared_by_two_remotes_is_refused(tmp_path: Path) -> None:
    document = configuration(tmp_path / "remote.git", tmp_path / "gone.git")
    mapping(mapping(document["remotes"])[GONE_REPO])["binding"] = BINDING

    with pytest.raises(ValidationError) as raised:
        GitConfiguration.model_validate(document)

    assert [error["msg"] for error in raised.value.errors()] == [
        "Value error, each remote has its own binding"
    ]


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
