import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

from kora_tooling_fixture.configured_adapter import Adapter


@pytest.fixture
def kora_conformance_adapter() -> Generator[Adapter]:
    assert sys.flags.isolated == 1
    assert "tests" not in sys.modules
    assert not any(
        name in sys.modules
        for name in (
            "kora.conformance.policy",
            "kora.conformance.harness",
            "kora.conformance.vcs",
            "kora.conformance.artifact_storage",
            "kora_tooling_fixture.noisy_import",
        )
    )
    assert Path(__file__).is_relative_to(Path(sys.prefix))
    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert not any(
        name in os.environ
        for name in (
            "PYTEST_PLUGINS",
            "PYTEST_ADDOPTS",
            "PYTHONPATH",
            "PYTHONHOME",
        )
    )
    mode = os.environ.get("KORA_FIXTURE_MODE", "normal")
    if mode == "setup":
        raise ValueError("private-setup-marker")
    adapter = Adapter(mode)

    yield adapter

    if mode == "teardown":
        raise ValueError("private-teardown-marker")
    if mode not in {"prepare"}:
        assert adapter.cleaned
