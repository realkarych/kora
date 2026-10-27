import pytest

from kora.conformance.session import CURRENT_SESSION


def test_configured_provider(
    kora_conformance_adapter: object, request: pytest.FixtureRequest
) -> None:
    CURRENT_SESSION.get().run(kora_conformance_adapter, request)
