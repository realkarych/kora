import asyncio
import copy
import json
import pickle
from collections.abc import Callable
from typing import cast

import pytest
from pydantic_core import PydanticSerializationError

from kora.conformance.capture import Capture
from kora.conformance.common import (
    check,
    context_for,
    fail,
    kora_capture,
    raised_protocol_error,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import ERROR_CODE
from kora.protocols.secret_value import SecretTransfer, SecretValue, registered_forms
from kora.protocols.secrets import SecretsProvider

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
    "test_secrets_missing_reference_is_a_bounded_error",
    "test_secrets_resolution_returns_a_masked_value",
    "test_secrets_value_is_masked_in_a_captured_stream",
]


class TransferHolder(BoundaryModel):
    value: SecretTransfer


@pytest.fixture
def kora_provider(kora_secrets_provider: SecretsProvider) -> SecretsProvider:
    return kora_secrets_provider


@pytest.fixture
def kora_exercise(kora_secrets_exercise: object) -> object:
    return kora_secrets_exercise


def refuses(action: Callable[[], object]) -> bool:
    try:
        action()
    except TypeError:
        return True
    return False


def renderings(value: SecretValue) -> tuple[str, ...]:
    return (repr(value), str(value), format(value), f"{value}", f"{value!r}")


def check_never_rendered(value: SecretValue, plain: str) -> None:
    for rendered in renderings(value):
        check(plain not in rendered, "no rendering shows the secret")
    check(refuses(lambda: pickle.dumps(value)), "pickling a secret is refused")
    check(refuses(lambda: copy.copy(value)), "copying a secret is refused")
    check(refuses(lambda: copy.deepcopy(value)), "deep-copying a secret is refused")
    holder = TransferHolder.model_construct(value=value)
    check(plain not in repr(holder), "a model's repr does not show the secret")
    check(
        refuses_serialisation(holder),
        "serialising a secret without transfer is refused",
    )


def refuses_serialisation(holder: TransferHolder) -> bool:
    try:
        holder.model_dump()
    except PydanticSerializationError:
        return True
    return False


def record_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        items = cast("dict[object, object]", value)
        return [text for item in items.items() for text in record_strings(item)]
    if isinstance(value, list | tuple):
        sequence = cast("list[object]", value)
        return [text for item in sequence for text in record_strings(item)]
    return []


def check_absent_from_records(capture: Capture, plain: str) -> None:
    records = capture.finish()
    raw = "\n".join(capture.reader.lines())
    for form in registered_forms(plain):
        check(form not in raw, "no captured record carries the secret")
    for record in records:
        for text in record_strings(record):
            check(plain not in text, "no captured record carries the secret")


def resolved(provider: SecretsProvider, capture: Capture, reference: str) -> object:
    outcome: object = asyncio.run(provider.resolve(reference, context_for(capture)))
    return outcome


@pytest.mark.kora_conformance
def test_secrets_resolution_returns_a_masked_value(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_resolvable: tuple[str, str],
    kora_capture: Capture,
) -> None:
    reference, expected = kora_secrets_resolvable

    value = resolved(kora_secrets_provider, kora_capture, reference)

    if not isinstance(value, SecretValue):
        fail("resolve returns a SecretValue")
    check(value.reveal() == expected, "resolve returns the referenced value")
    check_never_rendered(value, expected)
    check_absent_from_records(kora_capture, expected)


@pytest.mark.kora_conformance
def test_secrets_missing_reference_is_a_bounded_error(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_missing_reference: str,
    kora_capture: Capture,
) -> None:
    context = context_for(kora_capture)

    error = raised_protocol_error(
        kora_secrets_provider.resolve(kora_secrets_missing_reference, context)
    )

    if error is None:
        fail("a missing reference raises KoraProtocolError")
    check(ERROR_CODE.fullmatch(error.code) is not None, "the code is bounded")
    check(error.retryable is False, "a missing reference is not retryable")


@pytest.mark.kora_conformance
def test_secrets_value_is_masked_in_a_captured_stream(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_resolvable: tuple[str, str],
    kora_capture: Capture,
) -> None:
    reference, expected = kora_secrets_resolvable
    resolved(kora_secrets_provider, kora_capture, reference)
    escaped = (json.dumps(expected), json.dumps(expected, ensure_ascii=False))
    stream = f"before {expected} middle {escaped[0]} and {escaped[1]} after"

    masked = kora_capture.diagnostics.registry.mask(stream)

    for form in registered_forms(expected):
        check(form not in masked, "every escaped form of the secret is masked")
