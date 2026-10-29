from collections.abc import Generator

import pytest

from tests.support.local_runners import reap_runners


@pytest.fixture(scope="session", autouse=True)
def leaked_runners(tmp_path_factory: pytest.TempPathFactory) -> Generator[None]:
    base = tmp_path_factory.getbasetemp()
    reap_runners(base)
    yield
    assert reap_runners(base) == ()
