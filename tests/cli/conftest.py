from pathlib import Path

import pytest

from tests.support.provider_tooling import copy_core_wheel, prepare_wheelhouse


@pytest.fixture(scope="session")
def provider_tooling_wheelhouse(
    tmp_path_factory: pytest.TempPathFactory, core_wheel: Path
) -> Path:
    output = tmp_path_factory.mktemp("provider-wheelhouse")
    prepare_wheelhouse(output)
    copy_core_wheel(core_wheel, output)
    return output
