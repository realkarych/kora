import pytest

from kora_tooling_fixture.configured_harness import Adapter


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
