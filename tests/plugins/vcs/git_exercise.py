import tempfile
from pathlib import Path

from kora.plugins.vcs.git import GitConfiguration, GitVcs
from kora.protocols.common import ProtocolContext
from kora.protocols.vcs import CredentialRequest
from tests.plugins.vcs.git_support import configuration, git, seeded_remote


async def git_exercise(ctx: ProtocolContext, marker: str) -> object:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        home = root / "home"
        home.mkdir()
        remote, _, _ = seeded_remote(root, home)
        gone = root / "gone.git"
        git(home, "init", "-q", "--bare", str(gone))
        provider = GitVcs(GitConfiguration.model_validate(configuration(remote, gone)))
        request = CredentialRequest(
            binding="local-binding",
            binding_revision=1,
            credential_mode="local_credential_free",
            repo=f"/srv/{marker}",
            purpose="checkout",
            attempt_id="attempt",
            operation_id=None,
            prepared_digest=None,
            destination_digest=None,
        )
        return await provider.acquire_credential(request, ctx)
