from collections.abc import Callable

import pytest

from kora.conformance.session import CURRENT_SESSION
from kora.protocols.storage import ArtifactStorage


@pytest.fixture
def kora_provider() -> object:
    return CURRENT_SESSION.get().provider()


@pytest.fixture
def kora_runtime_provider() -> object:
    return CURRENT_SESSION.get().provider()


@pytest.fixture
def kora_secrets_provider() -> object:
    return CURRENT_SESSION.get().provider()


@pytest.fixture
def kora_policy_provider() -> object:
    return CURRENT_SESSION.get().provider()


@pytest.fixture
def kora_harness_provider() -> object:
    return CURRENT_SESSION.get().provider()


@pytest.fixture
def kora_vcs_provider() -> object:
    return CURRENT_SESSION.get().provider()


@pytest.fixture
def kora_artifact_storage_factory() -> Callable[[], ArtifactStorage]:
    return CURRENT_SESSION.get().storage_factory
