import os

import pytest

from kora_tooling_fixture.configured_adapter import Adapter
from kora_tooling_fixture.configured_storage import Adapter as StorageAdapter


@pytest.fixture
def kora_conformance_adapter() -> object:
    if os.environ["KORA_FIXTURE_MODE"] == "factory":
        return StorageAdapter()
    return object() if os.environ["KORA_FIXTURE_MODE"] == "adapter" else Adapter()


@pytest.fixture
def kora_provider(kora_provider: object) -> object:
    return object() if os.environ["KORA_FIXTURE_MODE"] == "generic" else kora_provider


@pytest.fixture
def kora_secrets_provider(kora_secrets_provider: object) -> object:
    return (
        object()
        if os.environ["KORA_FIXTURE_MODE"] == "selected"
        else kora_secrets_provider
    )


@pytest.fixture
def kora_artifact_storage_factory(kora_artifact_storage_factory: object) -> object:
    return (
        object()
        if os.environ["KORA_FIXTURE_MODE"] == "factory"
        else kora_artifact_storage_factory
    )
