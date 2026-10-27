import os

import pytest

from kora_tooling_fixture.configured_vcs import Adapter


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter(os.environ.get("KORA_FIXTURE_MODE", "normal"))
