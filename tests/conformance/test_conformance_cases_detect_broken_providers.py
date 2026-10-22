import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

import kora.conformance.common as common_cases
import kora.conformance.secrets as secrets_cases
from kora.conformance.capture import (
    LEFTOVERS,
    Capture,
    CaptureOverflow,
    Leftover,
    Pipe,
    captured,
)
from kora.conformance.common import CONFORMANCE_COMPONENT, MARKER, utc_now
from kora.observability import LoggingSettings, configure_process, current_diagnostics
from tests.conformance.providers import (
    MISSING,
    REFERENCE,
    RESOLVING,
    VALUE,
    EchoingSecretsProvider,
    LeakingSecretsProvider,
    MutableCapabilitiesProvider,
    ReferenceSecretsProvider,
    WrongValueSecretsProvider,
    echoing_exercise,
    exercise,
    silent_exercise,
)
from tests.support.observability import run_python

CYCLES: int = 20
WARM_UP: int = 2
PROBE: Path = Path(__file__).with_name("stuck_capture_probe.py")


def run_case(case: Callable[[Capture], None]) -> None:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(), "selftest", utc_now
    ) as capture:
        case(capture)


def descriptor_count() -> int:
    return len(os.listdir("/dev/fd"))


def resolution(capture: Capture) -> None:
    secrets_cases.test_secrets_resolution_returns_a_masked_value(
        ReferenceSecretsProvider(), (REFERENCE, VALUE), capture
    )


def bounded_failure(capture: Capture) -> None:
    common_cases.test_failure_is_a_bounded_protocol_error(exercise, capture)


def mutable_capabilities(capture: Capture) -> None:
    common_cases.test_capabilities_are_frozen_and_consistent(
        MutableCapabilitiesProvider()
    )


def array_config_schema(capture: Capture) -> None:
    common_cases.test_config_schema_is_an_object_schema(MutableCapabilitiesProvider())


def echoing_failure(capture: Capture) -> None:
    common_cases.test_failure_is_a_bounded_protocol_error(echoing_exercise, capture)


def silent_failure(capture: Capture) -> None:
    common_cases.test_provider_logs_through_its_context(silent_exercise, capture)


def echoing_missing_reference(capture: Capture) -> None:
    secrets_cases.test_secrets_missing_reference_is_a_bounded_error(
        EchoingSecretsProvider(), MISSING, capture
    )


def wrong_value_resolution(capture: Capture) -> None:
    secrets_cases.test_secrets_resolution_returns_a_masked_value(
        WrongValueSecretsProvider(), (REFERENCE, VALUE), capture
    )


def leaking_resolution(capture: Capture) -> None:
    secrets_cases.test_secrets_resolution_returns_a_masked_value(
        LeakingSecretsProvider(), (REFERENCE, VALUE), capture
    )


def unmasked_stream(capture: Capture) -> None:
    secrets_cases.test_secrets_value_is_masked_in_a_captured_stream(
        WrongValueSecretsProvider(), (REFERENCE, VALUE), capture
    )


BROKEN_CASES: tuple[Callable[[Capture], None], ...] = (
    mutable_capabilities,
    array_config_schema,
    echoing_failure,
    silent_failure,
    echoing_missing_reference,
    wrong_value_resolution,
    leaking_resolution,
    unmasked_stream,
)


@pytest.mark.parametrize("case", BROKEN_CASES, ids=lambda case: case.__name__)
def test_cases_fail_for_a_broken_provider(case: Callable[[Capture], None]) -> None:
    failing = pytest.raises(pytest.fail.Exception)

    with failing:
        run_case(case)

    assert current_diagnostics() is None


def test_a_capture_past_its_cap_fails_and_releases_everything() -> None:
    threads = threading.active_count()
    descriptors = descriptor_count()

    with pytest.raises(CaptureOverflow):
        with captured(
            CONFORMANCE_COMPONENT, LoggingSettings(), "overflow", utc_now, max_bytes=64
        ) as capture:
            capture.diagnostics.logger(CONFORMANCE_COMPONENT).emit(
                RESOLVING, detail=MARKER
            )

    assert threading.active_count() == threads
    assert descriptor_count() == descriptors
    assert current_diagnostics() is None


def test_a_finished_leftover_is_cleared_by_the_next_capture() -> None:
    threads = threading.active_count()
    descriptors = descriptor_count()
    pipe = Pipe.open()
    diagnostics = configure_process(
        CONFORMANCE_COMPONENT, LoggingSettings(), "leftover", utc_now, pipe.write_end
    )
    pipe.reader.start()
    diagnostics.shutdown()
    LEFTOVERS.held.append(Leftover(diagnostics, pipe))

    with captured(CONFORMANCE_COMPONENT, LoggingSettings(), "next", utc_now):
        held = len(LEFTOVERS.held)

    assert held == 0
    assert threading.active_count() == threads
    assert descriptor_count() == descriptors


def test_cases_release_every_resource_after_repeated_runs() -> None:
    for _ in range(WARM_UP):
        run_case(resolution)
    threads = threading.active_count()
    descriptors = descriptor_count()

    for _ in range(CYCLES):
        run_case(resolution)
        run_case(bounded_failure)

    assert threading.active_count() == threads
    assert descriptor_count() == descriptors
    assert current_diagnostics() is None


def test_a_stuck_capture_is_bounded_and_reported() -> None:
    result = run_python(["-I", str(PROBE)], {})

    assert result.code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "timed_out": True,
        "refused": True,
        "held": 1,
    }
