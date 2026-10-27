from collections.abc import Callable

import pytest

from kora.conformance.session import CURRENT_SESSION
from kora.protocols.storage import ArtifactStorage


def session_provider(name: str) -> object:
    def provider() -> object:
        return CURRENT_SESSION.get().provider()

    return pytest.fixture(provider, name=name)


kora_provider: object = session_provider("kora_provider")
kora_runtime_provider: object = session_provider("kora_runtime_provider")
kora_secrets_provider: object = session_provider("kora_secrets_provider")
kora_policy_provider: object = session_provider("kora_policy_provider")
kora_harness_provider: object = session_provider("kora_harness_provider")
kora_vcs_provider: object = session_provider("kora_vcs_provider")


@pytest.fixture
def kora_artifact_storage_factory() -> Callable[[], ArtifactStorage]:
    return CURRENT_SESSION.get().storage_factory
