import os
from collections.abc import Generator

import pytest

from kora_tooling_fixture.adverse_outcomes import skip_fixture, xfail_fixture
from kora_tooling_fixture.configured_adapter import Adapter


def adverse() -> None:
    mode = os.environ["KORA_FIXTURE_MODE"]
    if mode.endswith("skip"):
        skip_fixture()
    xfail_fixture()


@pytest.fixture
def kora_conformance_adapter() -> Generator[Adapter]:
    phase = os.environ["KORA_FIXTURE_MODE"].split("_", 1)[0]
    if phase == "setup":
        adverse()
    adapter = Adapter(exercise=adverse if phase == "exercise" else None)

    yield adapter

    assert adapter.cleaned
    if phase == "teardown":
        adverse()
