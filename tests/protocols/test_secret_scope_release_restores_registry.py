import json

import pytest

from kora.observability import LoggingSettings, SecretAdmissionError
from kora.protocols.secret_value import (
    SecretRevealRefused,
    SecretScope,
    registered_forms,
)
from tests.protocols.conftest import StartCapture
from tests.protocols.secret_support import ESCAPED, PLAIN, registry_state

CYCLES: int = 50


def test_forms_per_value_are_counted_once() -> None:
    plain_forms = registered_forms(PLAIN)
    escaped_forms = registered_forms(ESCAPED)

    assert plain_forms == (PLAIN,)
    assert escaped_forms == (
        ESCAPED,
        json.dumps(ESCAPED, ensure_ascii=True)[1:-1],
        json.dumps(ESCAPED, ensure_ascii=False)[1:-1],
    )


def test_secret_scope_release_restores_registry(start_capture: StartCapture) -> None:
    capture = start_capture(LoggingSettings())
    baseline = registry_state(capture.diagnostics)

    for number in range(CYCLES):
        scope = SecretScope(capture.diagnostics)
        scope.admit(PLAIN)
        scope.admit(PLAIN)
        scope.admit(ESCAPED)
        if number % 5 == 0:
            with pytest.raises(SecretAdmissionError):
                scope.admit("short")
        assert scope.registered_count() == 4
        scope.release()

    assert registry_state(capture.diagnostics) == baseline


def test_a_released_scope_refuses_admission_and_reveal(
    start_capture: StartCapture,
) -> None:
    capture = start_capture(LoggingSettings())
    scope = SecretScope(capture.diagnostics)
    value = scope.admit(PLAIN)

    scope.release()

    with pytest.raises(SecretAdmissionError):
        scope.admit(ESCAPED)
    with pytest.raises(SecretRevealRefused):
        value.reveal()
