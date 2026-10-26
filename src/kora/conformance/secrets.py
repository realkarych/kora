import asyncio
import copy
import json
import pickle
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic_core import PydanticSerializationError

from kora.conformance.capture import Capture
from kora.conformance.common import (
    await_protocol_error,
    awaited,
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
from kora.ir.json import JsonObject
from kora.observability.redaction import StreamMasker
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.errors import ERROR_CODE, KoraProtocolError
from kora.protocols.secret_value import (
    TRANSFER_SCOPE_KEY,
    SecretScope,
    SecretTransfer,
    SecretValue,
    registered_forms,
)
from kora.protocols.secrets import SecretsProvider

__all__ = [
    "SecretsConformanceInputs",
    "check_secrets_forbidden_reference_is_refused_like_a_missing_one",
    "check_secrets_list_names_returns_names_only",
    "check_secrets_missing_reference_is_a_bounded_error",
    "check_secrets_resolution_returns_a_masked_value",
    "check_secrets_value_is_masked_in_a_captured_stream",
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


@dataclass(frozen=True)
class SecretsConformanceInputs:
    failure: Callable[
        [SecretsProvider, JsonObject, ProtocolContext, str], Awaitable[object]
    ]
    resolvable: tuple[str, str]
    missing_reference: str
    forbidden_reference: str
    listed_prefix: str
    configuration_effect: Callable[
        [SecretsProvider, JsonObject, ProtocolContext], Awaitable[None]
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


def check_never_rendered(value: SecretValue, plain: str, scope: SecretScope) -> None:
    for rendered in renderings(value):
        check(plain not in rendered, "no rendering shows the secret")
    check(refuses(lambda: pickle.dumps(value)), "pickling a secret is refused")
    check(refuses(lambda: copy.copy(value)), "copying a secret is refused")
    check(refuses(lambda: copy.deepcopy(value)), "deep-copying a secret is refused")
    holder = TransferHolder.model_validate(
        {"value": value}, context={TRANSFER_SCOPE_KEY: scope}
    )
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


def masked_stream(capture: Capture, data: bytes) -> str:
    parts: list[str] = []
    with StreamMasker(capture.diagnostics.registry, parts.append) as masker:
        masker.feed(data)
    return "".join(parts)


def check_content_free(error: KoraProtocolError, reference: str) -> None:
    check(ERROR_CODE.fullmatch(error.code) is not None, "the code is bounded")
    check(error.retryable is False, "a refused reference is not retryable")
    name = reference.rsplit("/", 1)[-1]
    notes = " ".join(str(note) for note in getattr(error, "__notes__", ()))
    rendered = f"{error.message} {json.dumps(error.details)} {notes}"
    check(name not in rendered, "the message and details carry no reference")
    shown = error.__context__ is not None and not error.__suppress_context__
    check(error.__cause__ is None and not shown, "the error carries no chain")


async def check_secrets_resolution_returns_a_masked_value(
    provider: SecretsProvider,
    resolvable: tuple[str, str],
    capture: Capture,
    context: ProtocolContext,
) -> None:
    reference, expected = resolvable

    value = await awaited(provider.resolve(reference, context))

    check_resolved_value(value, expected, capture)


def check_resolved_value(value: object, expected: str, capture: Capture) -> None:
    if not isinstance(value, SecretValue):
        fail("resolve returns a SecretValue")
    check(value.reveal() == expected, "resolve returns the referenced value")
    check_never_rendered(value, expected, capture.scope)
    check_absent_from_records(capture, expected)


async def check_secrets_missing_reference_is_a_bounded_error(
    provider: SecretsProvider, reference: str, context: ProtocolContext
) -> None:
    error = await await_protocol_error(provider.resolve(reference, context))

    check_missing_reference_error(error, reference)


def check_missing_reference_error(
    error: KoraProtocolError | None, reference: str
) -> None:
    if error is None:
        fail("a missing reference raises KoraProtocolError")
    check_content_free(error, reference)


async def check_secrets_forbidden_reference_is_refused_like_a_missing_one(
    provider: SecretsProvider, reference: str, context: ProtocolContext
) -> None:
    error = await await_protocol_error(provider.resolve(reference, context))

    check_forbidden_reference_error(error, reference)


def check_forbidden_reference_error(
    error: KoraProtocolError | None, reference: str
) -> None:
    if error is None:
        fail("a reference outside the namespace's prefixes raises KoraProtocolError")
    check_content_free(error, reference)


async def check_secrets_list_names_returns_names_only(
    provider: SecretsProvider, prefix: str, resolvable: tuple[str, str]
) -> None:
    expected = resolvable[1]

    names = await awaited(provider.list_names(prefix))

    check_listed_names(names, prefix, expected)


def check_listed_names(names: object, prefix: str, expected: str) -> None:
    if not isinstance(names, list):
        fail("list_names returns a list")
    listed = cast("list[object]", names)
    check(bool(listed), "the listed prefix names at least one secret")
    check(all(isinstance(name, str) for name in listed), "every name is a string")
    check(
        all(str(name).startswith(prefix) for name in listed),
        "every name has the requested prefix",
    )
    check(all(expected not in str(name) for name in listed), "no name is a value")


async def check_secrets_value_is_masked_in_a_captured_stream(
    provider: SecretsProvider,
    resolvable: tuple[str, str],
    capture: Capture,
    context: ProtocolContext,
) -> None:
    reference, expected = resolvable
    await provider.resolve(reference, context)

    check_captured_stream(capture, expected)


def check_captured_stream(capture: Capture, expected: str) -> None:
    escaped = (json.dumps(expected), json.dumps(expected, ensure_ascii=False))
    stream = f"before {expected} middle {escaped[0]} and {escaped[1]} after"

    masked = masked_stream(capture, stream.encode())

    for form in registered_forms(expected):
        check(form not in masked, "every escaped form of the secret is masked")
    check(masked.startswith("before ") and masked.endswith(" after"), "the rest stays")


@pytest.mark.kora_conformance
def test_secrets_resolution_returns_a_masked_value(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_resolvable: tuple[str, str],
    kora_capture: Capture,
) -> None:
    reference, expected = kora_secrets_resolvable

    value = asyncio.run(
        awaited(kora_secrets_provider.resolve(reference, context_for(kora_capture)))
    )

    check_resolved_value(value, expected, kora_capture)


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

    check_missing_reference_error(error, kora_secrets_missing_reference)


@pytest.mark.kora_conformance
def test_secrets_forbidden_reference_is_refused_like_a_missing_one(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_forbidden_reference: str,
    kora_capture: Capture,
) -> None:
    context = context_for(kora_capture)

    error = raised_protocol_error(
        kora_secrets_provider.resolve(kora_secrets_forbidden_reference, context)
    )

    check_forbidden_reference_error(error, kora_secrets_forbidden_reference)


@pytest.mark.kora_conformance
def test_secrets_list_names_returns_names_only(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_listed_prefix: str,
    kora_secrets_resolvable: tuple[str, str],
) -> None:
    expected = kora_secrets_resolvable[1]

    names = asyncio.run(
        awaited(kora_secrets_provider.list_names(kora_secrets_listed_prefix))
    )

    check_listed_names(names, kora_secrets_listed_prefix, expected)


@pytest.mark.kora_conformance
def test_secrets_value_is_masked_in_a_captured_stream(
    kora_secrets_provider: SecretsProvider,
    kora_secrets_resolvable: tuple[str, str],
    kora_capture: Capture,
) -> None:
    reference, expected = kora_secrets_resolvable

    asyncio.run(kora_secrets_provider.resolve(reference, context_for(kora_capture)))

    check_captured_stream(kora_capture, expected)
