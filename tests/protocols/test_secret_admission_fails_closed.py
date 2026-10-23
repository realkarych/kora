import pytest

from kora.observability import LoggingSettings, SecretAdmissionError
from kora.observability.settings import (
    MAX_SECRET_BYTES,
    MAX_SECRET_VALUE_BYTES,
    MAX_SECRETS,
)
from kora.protocols.secret_value import SecretScope, registered_forms
from tests.protocols.conftest import StartCapture
from tests.protocols.secret_support import ESCAPED, PLAIN, registry_state


def utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


FILLER_BYTES: int = MAX_SECRET_VALUE_BYTES - 128


def fill_bytes(scope: SecretScope, total: int) -> None:
    count = -(-total // FILLER_BYTES)
    sizes = [total // count] * count
    sizes[-1] += total - sum(sizes)
    for index, size in enumerate(sizes):
        scope.admit(f"{index:08d}".ljust(size, "f"))


def assert_refused_without_content(scope: SecretScope, value: str) -> None:
    with pytest.raises(SecretAdmissionError) as raised:
        scope.admit(value)
    rendered = f"{raised.value} {raised.value!r}"
    assert value not in rendered
    assert raised.value.__cause__ is None


def test_admission_without_diagnostics_is_refused() -> None:
    scope = SecretScope(None)

    assert_refused_without_content(scope, PLAIN)


def test_admission_below_the_minimum_is_refused(start_capture: StartCapture) -> None:
    capture = start_capture(LoggingSettings())

    assert_refused_without_content(capture.scope, "short")
    assert registry_state(capture.diagnostics) == (0, 0)


def test_admission_of_text_that_is_not_utf8_is_refused(
    start_capture: StartCapture,
) -> None:
    capture = start_capture(LoggingSettings())

    assert_refused_without_content(capture.scope, "surrogate-\ud800-" + PLAIN)
    assert registry_state(capture.diagnostics) == (0, 0)


def test_admission_into_a_full_registry_is_refused(
    start_capture: StartCapture,
) -> None:
    capture = start_capture(LoggingSettings())
    filler = SecretScope(capture.diagnostics)
    for number in range(MAX_SECRETS):
        filler.admit(f"filler-{number:04d}")

    assert_refused_without_content(capture.scope, PLAIN)
    assert registry_state(capture.diagnostics)[0] == MAX_SECRETS
    filler.release()


@pytest.mark.parametrize("refused_form", [0, 1, 2])
def test_admission_rolls_back_when_any_form_is_refused(
    start_capture: StartCapture, refused_form: int
) -> None:
    capture = start_capture(LoggingSettings())
    forms = registered_forms(ESCAPED)
    room = sum(utf8_size(form) for form in forms[:refused_form])
    filler = SecretScope(capture.diagnostics)
    fill_bytes(filler, MAX_SECRET_BYTES - room - 1)
    before = registry_state(capture.diagnostics)

    assert_refused_without_content(capture.scope, ESCAPED)

    assert len(forms) == 3
    assert registry_state(capture.diagnostics) == before
    assert capture.scope.registered_count() == 0
    filler.release()
