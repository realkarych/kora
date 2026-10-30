import asyncio
from collections.abc import Iterator

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
from kora.db.connection import Database, DatabaseSettings
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.plugins.authentication.support import fixture_inputs

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "kora_authentication_runner",
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
def kora_authentication_database(
    database_settings: DatabaseSettings, kora_authentication_runner: asyncio.Runner
) -> Iterator[Database]:
    database = Database(database_settings)
    try:
        yield database
    finally:
        kora_authentication_runner.run(database.close())


@pytest.fixture
def kora_authentication_provider(
    kora_authentication_database: Database,
) -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator({}, kora_authentication_database)


@pytest.fixture
def kora_authentication_inputs(
    kora_authentication_database: Database, kora_authentication_runner: asyncio.Runner
) -> AuthenticationConformanceInputs:
    return kora_authentication_runner.run(fixture_inputs(kora_authentication_database))
