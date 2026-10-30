import asyncio
from collections.abc import Callable
from functools import partial

import pytest

from kora.conformance import artifact_type as checks
from kora.conformance.capture import captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    awaited,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    context_for,
    utc_now,
)
from kora.ir.json import JsonObject
from kora.observability import Level
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import ArtifactType
from kora.protocols.common import ProtocolContext
from tests.conformance.artifact_types import SelectedFieldArtifactType
from tests.conformance.artifact_types import inputs as selected_inputs
from tests.conformance.test_shared_secrets_checks import assert_capture_released
from tests.plugins.artifact_type.support import binding, inputs

CHECKS: tuple[
    Callable[
        [ArtifactType, checks.ArtifactTypeConformanceInputs, ProtocolContext], None
    ],
    ...,
] = (
    checks.check_artifact_type_accepts_declared_values,
    checks.check_artifact_type_rejects_declared_values,
    checks.check_artifact_type_digest_fields_are_stable,
    checks.check_artifact_type_summaries_are_detached,
    checks.check_artifact_type_invalid_extraction_is_refused,
)


def test_artifact_type_shared_checks_accept_vcs_ref() -> None:
    provider = VcsRefArtifactType({}, binding())
    exercises = inputs()
    with captured(
        CONFORMANCE_COMPONENT,
        checks.artifact_type_capture_settings(),
        "shared-vcs",
        utc_now,
    ) as capture:
        context = context_for(capture)

        for check in CHECKS:
            check(provider, exercises, context)
        asyncio.run(awaited(exercises.configuration_effect(provider, {}, context)))

    assert_capture_released(capture)


def test_artifact_type_shared_checks_accept_selected_field_summary() -> None:
    provider = SelectedFieldArtifactType()
    exercises = selected_inputs()
    with captured(
        CONFORMANCE_COMPONENT,
        checks.artifact_type_capture_settings(),
        "shared-selected",
        utc_now,
    ) as capture:
        context = context_for(capture)

        for check in CHECKS:
            check(provider, exercises, context)
        asyncio.run(
            awaited(
                exercises.configuration_effect(provider, {"selected": "right"}, context)
            )
        )

    assert len(exercises.expected_violations) == 2
    assert_capture_released(capture)


@pytest.mark.parametrize(
    ("defect", "check"),
    [
        ("accept_invalid", checks.check_artifact_type_rejects_declared_values),
        ("wrong_selection", checks.check_artifact_type_digest_fields_are_stable),
        ("wrong_summary", checks.check_artifact_type_summaries_are_detached),
        ("extract_invalid", checks.check_artifact_type_invalid_extraction_is_refused),
        ("mutate_validation_input", checks.check_artifact_type_accepts_declared_values),
        ("mutate_selection_input", checks.check_artifact_type_digest_fields_are_stable),
        ("mutate_summary_input", checks.check_artifact_type_summaries_are_detached),
        ("reuse_selection", checks.check_artifact_type_digest_fields_are_stable),
        ("alias_summary", checks.check_artifact_type_summaries_are_detached),
        ("reuse_summary", checks.check_artifact_type_summaries_are_detached),
    ],
)
def test_artifact_type_shared_checks_refuse_broken_implementations(
    defect: str,
    check: Callable[
        [ArtifactType, checks.ArtifactTypeConformanceInputs, ProtocolContext], None
    ],
) -> None:
    provider = SelectedFieldArtifactType(defect=defect)
    exercises = selected_inputs()
    with captured(
        CONFORMANCE_COMPONENT,
        checks.artifact_type_capture_settings(),
        "shared-broken",
        utc_now,
    ) as capture:
        context = context_for(capture)

        with pytest.raises(pytest.fail.Exception):
            check(provider, exercises, context)

    assert_capture_released(capture)


def test_artifact_type_debug_logs_reach_common_capture() -> None:
    provider = VcsRefArtifactType({}, binding())
    exercises = inputs()
    configuration: JsonObject = {}
    with captured(
        CONFORMANCE_COMPONENT,
        checks.artifact_type_capture_settings(),
        "shared-logging",
        utc_now,
    ) as capture:
        context = context_for(capture)
        exercise = partial(exercises.failure, provider, configuration)

        asyncio.run(
            check_failure_is_a_bounded_protocol_error(
                exercise, context, "private-marker"
            )
        )
        asyncio.run(
            check_provider_logs_through_its_context(
                exercise, capture, context, "private-marker"
            )
        )

    assert capture.reader.records()
    assert all(record["level"] == "DEBUG" for record in capture.reader.records())
    assert checks.artifact_type_capture_settings().component_levels == {
        CONFORMANCE_COMPONENT: Level.DEBUG
    }
    assert_capture_released(capture)
