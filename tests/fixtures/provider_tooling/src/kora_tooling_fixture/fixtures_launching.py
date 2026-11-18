import pytest

from .launching_harness import Adapter


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
