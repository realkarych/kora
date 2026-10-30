from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, replace

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    INSTANCE_ID,
    MARKER,
    await_protocol_error,
    check,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    context_for,
    fail,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    utc_now,
)
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from kora.protocols.authentication import Authenticator, Principal, RequestInfo
from kora.protocols.common import ProtocolContext

__all__ = [
    "AuthenticationCase",
    "AuthenticationConformanceInputs",
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


@dataclass(frozen=True)
class AuthenticationCase:
    request: RequestInfo
    principal: Principal


@dataclass(frozen=True)
class AuthenticationConformanceInputs:
    accepted: tuple[AuthenticationCase, ...]
    rejected: tuple[RequestInfo, ...]
    changing: AuthenticationCase
    change_authority: Callable[[], Awaitable[Principal]]
    revocable: AuthenticationCase
    unexpected: RequestInfo
    make_unavailable: Callable[[], Awaitable[None]]
    failure: Callable[
        [Authenticator, JsonObject, ProtocolContext, str], Awaitable[object]
    ]
    configuration_effect: Callable[
        [Authenticator, JsonObject, ProtocolContext], Awaitable[None]
    ]


def authentication_capture_settings() -> LoggingSettings:
    return LoggingSettings(level=Level.DEBUG)


@pytest.fixture
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT, authentication_capture_settings(), INSTANCE_ID, utc_now
    ) as capture:
        yield capture


def current(request: RequestInfo) -> RequestInfo:
    return replace(request, requires_current_authority=True)


async def expect_principal(
    provider: Authenticator, case: AuthenticationCase, ctx: ProtocolContext
) -> None:
    for request in (case.request, current(case.request)):
        received = await provider.authenticate(request, ctx)

        check(type(received) is Principal, "authentication returns a Principal")
        check(
            Principal.model_validate(received) == case.principal, "authority is exact"
        )


async def expect_refusal(
    provider: Authenticator,
    request: RequestInfo,
    ctx: ProtocolContext,
    unavailable: bool = False,
) -> str:
    code = "authentication_unavailable" if unavailable else "authentication_rejected"
    message: str | None = None
    for candidate in (request, current(request)):
        error = await await_protocol_error(provider.authenticate(candidate, ctx))

        if error is None:
            fail("authentication must refuse")
        check(
            (error.code, error.retryable, error.details) == (code, unavailable, {}),
            "refusal is exact",
        )
        if message is not None:
            check(error.message == message, "refusal text is fixed")
        message = error.message
        check(
            error.__cause__ is None and error.__context__ is None,
            "refusal has no exception chain",
        )
    return message


async def check_authentication_preserves_principals(
    provider: Authenticator,
    inputs: AuthenticationConformanceInputs,
    ctx: ProtocolContext,
) -> None:
    check(bool(inputs.accepted), "accepted authentication cases are supplied")
    for case in inputs.accepted:
        await expect_principal(provider, case, ctx)


async def check_authentication_refusals_are_uniform(
    provider: Authenticator,
    inputs: AuthenticationConformanceInputs,
    ctx: ProtocolContext,
) -> None:
    check(bool(inputs.rejected), "rejected authentication cases are supplied")
    message: str | None = None
    for request in inputs.rejected:
        received = await expect_refusal(provider, request, ctx)
        if message is not None:
            check(received == message, "invalidity categories are indistinguishable")
        message = received


async def check_authentication_revalidates_current_authority(
    provider: Authenticator,
    inputs: AuthenticationConformanceInputs,
    ctx: ProtocolContext,
) -> None:
    await expect_principal(provider, inputs.changing, ctx)

    changed = await inputs.change_authority()

    check(changed != inputs.changing.principal, "the authority change is effective")
    await expect_principal(
        provider, AuthenticationCase(inputs.changing.request, changed), ctx
    )


async def check_authentication_revocation_is_permanent(
    provider: Authenticator,
    inputs: AuthenticationConformanceInputs,
    ctx: ProtocolContext,
) -> None:
    await expect_principal(provider, inputs.revocable, ctx)

    await provider.revoke(inputs.revocable.principal.token_id, ctx)
    await provider.revoke(inputs.revocable.principal.token_id, ctx)

    await expect_refusal(provider, inputs.revocable.request, ctx)


async def check_authentication_unavailability_grants_no_authority(
    provider: Authenticator,
    inputs: AuthenticationConformanceInputs,
    ctx: ProtocolContext,
) -> None:
    case = inputs.accepted[0]
    await expect_principal(provider, case, ctx)

    await inputs.make_unavailable()

    await expect_refusal(provider, case.request, ctx, unavailable=True)


async def check_authentication_unexpected_failure_grants_no_authority(
    provider: Authenticator,
    inputs: AuthenticationConformanceInputs,
    ctx: ProtocolContext,
) -> None:
    for request in (inputs.unexpected, current(inputs.unexpected)):
        error = await await_protocol_error(provider.authenticate(request, ctx))

        if error is None:
            fail("unexpected authentication failure grants no authority")
        check(
            error.code not in {"authentication_rejected", "authentication_unavailable"},
            "unexpected failure is distinct",
        )
        check(
            not error.retryable and error.details == {},
            "unexpected failure is content free",
        )
        check(
            error.__cause__ is None and error.__context__ is None,
            "unexpected failure has no exception chain",
        )


@pytest.fixture
def kora_provider(kora_authentication_provider: Authenticator) -> Authenticator:
    return kora_authentication_provider


@pytest.fixture
def kora_exercise(
    kora_provider: Authenticator,
) -> Callable[[ProtocolContext, str], Awaitable[object]]:
    async def failure(ctx: ProtocolContext, marker: str) -> object:
        return await kora_provider.authenticate(RequestInfo("Bearer " + marker), ctx)

    return failure


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_failure_is_a_bounded_protocol_error(
    kora_exercise: Callable[[ProtocolContext, str], Awaitable[object]],
    kora_capture: Capture,
) -> None:
    await check_failure_is_a_bounded_protocol_error(
        kora_exercise, context_for(kora_capture), MARKER
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_provider_logs_through_its_context(
    kora_exercise: Callable[[ProtocolContext, str], Awaitable[object]],
    kora_capture: Capture,
) -> None:
    await check_provider_logs_through_its_context(
        kora_exercise, kora_capture, context_for(kora_capture), MARKER
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_authentication_unexpected_failure_grants_no_authority(
    kora_provider: Authenticator,
    kora_authentication_inputs: AuthenticationConformanceInputs,
    kora_capture: Capture,
) -> None:
    await check_authentication_unexpected_failure_grants_no_authority(
        kora_provider, kora_authentication_inputs, context_for(kora_capture)
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_authentication_preserves_principals(
    kora_provider: Authenticator,
    kora_authentication_inputs: AuthenticationConformanceInputs,
    kora_capture: Capture,
) -> None:
    await check_authentication_preserves_principals(
        kora_provider, kora_authentication_inputs, context_for(kora_capture)
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_authentication_refusals_are_uniform(
    kora_provider: Authenticator,
    kora_authentication_inputs: AuthenticationConformanceInputs,
    kora_capture: Capture,
) -> None:
    await check_authentication_refusals_are_uniform(
        kora_provider, kora_authentication_inputs, context_for(kora_capture)
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_authentication_revalidates_current_authority(
    kora_provider: Authenticator,
    kora_authentication_inputs: AuthenticationConformanceInputs,
    kora_capture: Capture,
) -> None:
    await check_authentication_revalidates_current_authority(
        kora_provider, kora_authentication_inputs, context_for(kora_capture)
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_authentication_revocation_is_permanent(
    kora_provider: Authenticator,
    kora_authentication_inputs: AuthenticationConformanceInputs,
    kora_capture: Capture,
) -> None:
    await check_authentication_revocation_is_permanent(
        kora_provider, kora_authentication_inputs, context_for(kora_capture)
    )


@pytest.mark.kora_conformance
@pytest.mark.asyncio
async def test_authentication_unavailability_grants_no_authority(
    kora_provider: Authenticator,
    kora_authentication_inputs: AuthenticationConformanceInputs,
    kora_capture: Capture,
) -> None:
    await check_authentication_unavailability_grants_no_authority(
        kora_provider, kora_authentication_inputs, context_for(kora_capture)
    )
