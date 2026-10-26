from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from kora.conformance.common import (
    kora_capture,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.ir.json import JsonObject
from kora.protocols.common import ProtocolContext
from kora.protocols.runtime import Runtime

__all__ = [
    "RuntimeConformanceInputs",
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
]


@dataclass(frozen=True)
class RuntimeConformanceInputs:
    failure: Callable[[Runtime, JsonObject, ProtocolContext, str], Awaitable[object]]
    configuration_effect: Callable[
        [Runtime, JsonObject, ProtocolContext], Awaitable[None]
    ]


@pytest.fixture
def kora_provider(kora_runtime_provider: Runtime) -> Runtime:
    return kora_runtime_provider


@pytest.fixture
def kora_exercise(kora_runtime_exercise: object) -> object:
    return kora_runtime_exercise
