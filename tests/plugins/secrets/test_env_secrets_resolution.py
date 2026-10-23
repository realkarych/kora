import asyncio
import dataclasses
import json

import pytest
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.secrets import (
    kora_capture,
    kora_exercise,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
    test_secrets_forbidden_reference_is_refused_like_a_missing_one,
    test_secrets_list_names_returns_names_only,
    test_secrets_missing_reference_is_a_bounded_error,
    test_secrets_resolution_returns_a_masked_value,
    test_secrets_value_is_masked_in_a_captured_stream,
)
from kora.plugins.secrets.env import EnvSecretsConfiguration, EnvSecretsProvider
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.plugins.secrets.env_support import (
    CONFIGURATION,
    ENVIRONMENT,
    VALUE,
    provider,
    refusal_code,
    resolve_code,
)

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
    "test_secrets_forbidden_reference_is_refused_like_a_missing_one",
    "test_secrets_list_names_returns_names_only",
    "test_secrets_missing_reference_is_a_bounded_error",
    "test_secrets_resolution_returns_a_masked_value",
    "test_secrets_value_is_masked_in_a_captured_stream",
]


async def missing_exercise(ctx: ProtocolContext, marker: str) -> object:
    return await provider().resolve("secret://env/CONF_ABSENT", ctx)


@pytest.fixture
def kora_secrets_provider() -> EnvSecretsProvider:
    return provider()


@pytest.fixture
def kora_secrets_exercise() -> object:
    return missing_exercise


@pytest.fixture
def kora_secrets_resolvable() -> tuple[str, str]:
    return "secret://env/CONF_TOKEN", VALUE


@pytest.fixture
def kora_secrets_missing_reference() -> str:
    return "secret://env/CONF_ABSENT"


@pytest.fixture
def kora_secrets_forbidden_reference() -> str:
    return "secret://env/OTHER_TOKEN"


@pytest.fixture
def kora_secrets_listed_prefix() -> str:
    return "CONF_"


REFUSALS: dict[str, str] = {
    "env/CONF_TOKEN": "secret_reference_invalid",
    "secret://env/conf_token": "secret_reference_invalid",
    "secret://file/CONF_TOKEN": "secret_reference_invalid",
    "secret://env/OTHER_TOKEN": "secret_reference_not_allowed",
    "secret://env/OTHER_ABSENT": "secret_reference_not_allowed",
    "secret://env/WORKER_KEY": "secret_reference_not_allowed",
    "secret://env/CONF_ABSENT": "secret_missing",
    "secret://env/CONF_SHORT": "secret_admission_refused",
    "secret://env/CONF_WIDE": "secret_admission_refused",
}


@pytest.mark.parametrize("reference", sorted(REFUSALS))
def test_env_secrets_resolution_refuses_in_order(
    kora_capture: Capture, reference: str
) -> None:
    subject = provider()

    code = resolve_code(subject, kora_capture, reference)
    records = kora_capture.finish()

    assert code == REFUSALS[reference]
    rendered = json.dumps(records, ensure_ascii=False)
    assert [value for value in ENVIRONMENT.values() if value in rendered] == []
    assert kora_capture.scope.registered_count() == 0


ORDERED: list[tuple[str, str, str]] = [
    ("secret://env/OTHER_ABSENT", "request_digest", "secret_reference_not_allowed"),
    ("secret://env/CONF_ABSENT", "request_digest", "secret_resolution_unsealed"),
    ("secret://env/CONF_ABSENT", "secret_admission", "secret_admission_missing"),
    ("secret://env/CONF_ABSENT", "both", "secret_resolution_unsealed"),
]


@pytest.mark.parametrize(("reference", "unset", "expected"), ORDERED)
def test_refusals_keep_their_order_when_several_apply(
    kora_capture: Capture, reference: str, unset: str, expected: str
) -> None:
    fields = ("request_digest", "secret_admission") if unset == "both" else (unset,)
    context = dataclasses.replace(
        context_for(kora_capture), **dict.fromkeys(fields, None)
    )

    code = refusal_code(provider().resolve(reference, context))

    assert code == expected


def test_outcomes_are_logged_with_their_reason(kora_capture: Capture) -> None:
    subject = provider()
    context = context_for(kora_capture)

    asyncio.run(subject.resolve("secret://env/CONF_TOKEN", context))
    refusal_code(subject.resolve("secret://env/OTHER_TOKEN", context))
    records = kora_capture.finish()

    events = [(record.get("event"), record.get("reason")) for record in records]
    assert ("secrets.env.resolved", None) in events
    assert ("secrets.env.refused", "secret_reference_not_allowed") in events


def test_list_names_returns_at_most_a_thousand_names() -> None:
    environment = {f"CONF_NAME_{index:05d}": "value-unused" for index in range(1500)}

    listed = asyncio.run(provider(environment).list_names("CONF_"))

    assert listed == sorted(environment)[:1000]


def test_resolution_before_sealing_is_refused(kora_capture: Capture) -> None:
    context = dataclasses.replace(context_for(kora_capture), request_digest=None)

    code = refusal_code(provider().resolve("secret://env/CONF_TOKEN", context))

    assert code == "secret_resolution_unsealed"


def test_resolution_without_admission_is_refused(kora_capture: Capture) -> None:
    context = dataclasses.replace(context_for(kora_capture), secret_admission=None)

    code = refusal_code(provider().resolve("secret://env/CONF_TOKEN", context))

    assert code == "secret_admission_missing"


def test_refusals_carry_no_exception_chain(kora_capture: Capture) -> None:
    subject = provider()

    with pytest.raises(KoraProtocolError) as raised:
        asyncio.run(
            subject.resolve("secret://env/CONF_SHORT", context_for(kora_capture))
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.details == {}


UNSAFE_PREFIXES: list[list[str]] = [[""], ["KORA_"], ["A"], ["AWS_X"], ["WORKER"]]


@pytest.mark.parametrize(
    "prefixes", UNSAFE_PREFIXES, ids=lambda item: item[0] or "empty"
)
def test_the_configuration_refuses_unsafe_prefixes(prefixes: list[str]) -> None:
    document = {**CONFIGURATION, "namespace_prefixes": {"conformance": prefixes}}

    with pytest.raises(ValidationError):
        EnvSecretsConfiguration.model_validate(document)


def test_list_names_answers_only_configured_prefixes() -> None:
    subject = provider()

    listed = asyncio.run(subject.list_names("CONF_"))
    outside = asyncio.run(subject.list_names("OTH"))

    assert listed == ["CONF_SHORT", "CONF_TOKEN", "CONF_WIDE"]
    assert outside == []


def test_the_installed_entry_point_loads_the_env_provider(
    kora_capture: Capture,
) -> None:
    metadata = load_provider(ProtocolName.SECRETS, "env", kora_capture.diagnostics)

    created = metadata.implementation(CONFIGURATION)

    assert metadata.config_schema == EnvSecretsProvider.config_schema
    assert isinstance(created, EnvSecretsProvider)
