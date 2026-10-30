import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from kora.conformance.authentication import (
    AuthenticationConformanceInputs,
    kora_authentication_runner,
    kora_capture,
    kora_exercise,
    kora_provider,
    test_authentication_preserves_principals,
    test_authentication_refusals_are_uniform,
    test_authentication_revalidates_current_authority,
    test_authentication_revocation_is_permanent,
    test_authentication_unavailability_grants_no_authority,
    test_authentication_unexpected_failure_grants_no_authority,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.plugins.authentication.static_token import (
    StaticTokenAuthenticator,
    create_provider,
)
from kora_tooling_fixture.fixtures_authentication import Adapter

__all__ = [
    "kora_authentication_runner",
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_authentication_preserves_principals",
    "test_authentication_refusals_are_uniform",
    "test_authentication_revalidates_current_authority",
    "test_authentication_revocation_is_permanent",
    "test_authentication_unavailability_grants_no_authority",
    "test_authentication_unexpected_failure_grants_no_authority",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
]


@pytest.fixture
def prepared(
    tmp_path: Path, kora_authentication_runner: asyncio.Runner
) -> Iterator[Adapter]:
    adapter = Adapter()
    kora_authentication_runner.run(
        adapter.open("sqlite+aiosqlite:///" + str(tmp_path / "authentication.db"))
    )
    try:
        yield adapter
    finally:
        kora_authentication_runner.run(adapter.cleanup(None))


@pytest.fixture
def kora_authentication_provider(prepared: Adapter) -> StaticTokenAuthenticator:
    assert prepared.database is not None
    return create_provider({}, prepared.database)


@pytest.fixture
def kora_authentication_inputs(prepared: Adapter) -> AuthenticationConformanceInputs:
    return prepared.exercises
