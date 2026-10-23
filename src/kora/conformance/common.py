import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Generator
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn

import pytest

from kora.conformance.capture import Capture, captured
from kora.observability import CATALOG, LoggingSettings
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext, ProviderProtocol
from kora.protocols.errors import ERROR_CODE, KoraProtocolError
from kora.protocols.time import SystemClock

CONFORMANCE_COMPONENT: Final[str] = CATALOG.register_component("conformance")
INSTANCE_ID: Final[str] = "conformance"
NAMESPACE_ID: Final[str] = "conformance"
MARKER: Final[str] = "kora-conformance-content-marker"
REQUEST_DIGEST: Final[str] = "conformance-sealed-request"

Exercise = Callable[[ProtocolContext, str], Awaitable[object]]


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def fail(message: str) -> NoReturn:
    pytest.fail(message, pytrace=False)


def utc_now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), INSTANCE_ID, utc_now
    ) as capture:
        yield capture


def context_for(capture: Capture) -> ProtocolContext:
    return ProtocolContext(
        namespace_id=NAMESPACE_ID,
        run_id=None,
        attempt_id=None,
        logger=capture.diagnostics.logger(CONFORMANCE_COMPONENT),
        secrets=None,
        secret_admission=capture.scope.admission(),
        request_digest=REQUEST_DIGEST,
        deadline=None,
        clock=SystemClock(),
        activity=NoActivity(),
    )


def raised_protocol_error(outcome: Awaitable[object]) -> KoraProtocolError | None:
    try:
        asyncio.run(awaited(outcome))
    except KoraProtocolError as error:
        return error
    return None


async def awaited(outcome: Awaitable[object]) -> object:
    return await outcome


def is_frozen_dataclass(value: object) -> bool:
    if not is_dataclass(value) or isinstance(value, type):
        return False
    parameters: object = getattr(type(value), "__dataclass_params__", None)
    return bool(getattr(parameters, "frozen", False))


def flag_values(value: object) -> tuple[object, ...]:
    if not is_dataclass(value) or isinstance(value, type):
        return ()
    return tuple(getattr(value, item.name) for item in fields(value))


@pytest.mark.kora_conformance
def test_capabilities_are_frozen_and_consistent(
    kora_provider: ProviderProtocol,
) -> None:
    first = kora_provider.capabilities()
    second = kora_provider.capabilities()

    check(is_frozen_dataclass(first), "capabilities() returns a frozen dataclass")
    check(flag_values(first) == flag_values(second), "capabilities() is consistent")


@pytest.mark.kora_conformance
def test_config_schema_is_an_object_schema(kora_provider: ProviderProtocol) -> None:
    schema = kora_provider.config_schema

    check(schema.get("type") == "object", "config_schema has type object")


@pytest.mark.kora_conformance
def test_failure_is_a_bounded_protocol_error(
    kora_exercise: Exercise, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    error = raised_protocol_error(kora_exercise(context, MARKER))

    if error is None:
        fail("the exercise's failing input raises KoraProtocolError")
    check(ERROR_CODE.fullmatch(error.code) is not None, "the code is bounded")
    check(MARKER not in error.message, "the message carries no input")
    check(MARKER not in json.dumps(error.details), "the details carry no input")


@pytest.mark.kora_conformance
def test_provider_logs_through_its_context(
    kora_exercise: Exercise, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)
    handlers = list(logging.getLogger().handlers)

    raised_protocol_error(kora_exercise(context, MARKER))
    after = list(logging.getLogger().handlers)
    records = kora_capture.finish()

    check(after == handlers, "the provider leaves root handlers alone")
    check(
        any(record.get("component") == CONFORMANCE_COMPONENT for record in records),
        "the provider logs through the context's logger",
    )
    check(
        all(MARKER not in json.dumps(record) for record in records),
        "no record carries the exercise's content",
    )
