import dataclasses
import json
import os
import threading
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest

import kora.conformance.common as common_cases
import kora.conformance.policy as policy_cases
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
from kora.conformance.policy import UNPROTECTED_PATH
from kora.observability import LoggingSettings, configure_process, current_diagnostics
from tests.conformance.harnesses import HARNESS_BROKEN_CASES
from tests.conformance.policies import (
    AllowOverridesDenyPolicy,
    AnyDenyPolicy,
    ArgumentAllowSkipsProtectionPolicy,
    ContradictingReasonPolicy,
    DriftingReasonPolicy,
    EchoingValidatePolicy,
    EditCachingPolicy,
    ExactProtectedPathPolicy,
    FirstDenyOnlyPolicy,
    FirstProtectedOnlyPolicy,
    FirstTwoDeniesPolicy,
    FirstTwoProtectedPolicy,
    FormlessReasonPolicy,
    GlobOnlyProtectionPolicy,
    HardcodedTestsPolicy,
    IdentityCachedDriftPolicy,
    LastDenyOnlyPolicy,
    LayersSkippedUnderProtectionPolicy,
    LooseDecisionPolicy,
    MappingValidatePolicy,
    MisattributedProtectionPolicy,
    NestedNodeSkippingPolicy,
    NodeAllowIgnoringPolicy,
    NodeDenyReplacesNamespacePolicy,
    NonBoolAllowPolicy,
    PathlessValidatePolicy,
    ProtectionBlindCachePolicy,
    ReferencePolicy,
    ShellOnlyRulesPolicy,
    SilentValidatePolicy,
    SingleEntryAllowOnlyPolicy,
    SpecificAllowWinsPolicy,
    SubagentProtectionSkippingPolicy,
    SubagentSkippingPolicy,
    ToolCachingPolicy,
    TupleValidatePolicy,
    WrongRuleIdPolicy,
    calls_with_edits,
    echoing_policy_exercise,
    edit_calls,
    policy_exercise,
    reference_calls,
)
from tests.conformance.providers import (
    FORBIDDEN,
    MISSING,
    REFERENCE,
    RESOLVING,
    VALUE,
    ChainedSecretsProvider,
    EchoingSecretsProvider,
    LeakingNamesSecretsProvider,
    LeakingSecretsProvider,
    MutableCapabilitiesProvider,
    NameEchoingSecretsProvider,
    ReferenceSecretsProvider,
    SilentNamesSecretsProvider,
    UnprefixedNamesSecretsProvider,
    WrongValueSecretsProvider,
    echoing_exercise,
    exercise,
    silent_exercise,
)
from tests.conformance.vcs_providers import VCS_BROKEN_CASES
from tests.support.heap import recurring_growth, snapshot
from tests.support.observability import run_python

CYCLES: int = 20
WARM_UP: int = 2
MAX_RETAINED_BYTES: int = 64 * 1024
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


def echoing_forbidden_reference(capture: Capture) -> None:
    secrets_cases.test_secrets_forbidden_reference_is_refused_like_a_missing_one(
        EchoingSecretsProvider(), FORBIDDEN, capture
    )


def chained_forbidden_reference(capture: Capture) -> None:
    secrets_cases.test_secrets_forbidden_reference_is_refused_like_a_missing_one(
        ChainedSecretsProvider(), FORBIDDEN, capture
    )


def name_echoing_missing_reference(capture: Capture) -> None:
    secrets_cases.test_secrets_missing_reference_is_a_bounded_error(
        NameEchoingSecretsProvider(), MISSING, capture
    )


def leaking_names(capture: Capture) -> None:
    secrets_cases.test_secrets_list_names_returns_names_only(
        LeakingNamesSecretsProvider(), "", (REFERENCE, VALUE)
    )


def silent_names(capture: Capture) -> None:
    secrets_cases.test_secrets_list_names_returns_names_only(
        SilentNamesSecretsProvider(), "", (REFERENCE, VALUE)
    )


def unprefixed_names(capture: Capture) -> None:
    secrets_cases.test_secrets_list_names_returns_names_only(
        UnprefixedNamesSecretsProvider(), "conf-", (REFERENCE, VALUE)
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


def ignored_node_allow_list(capture: Capture) -> None:
    policy_cases.test_policy_allow_list_restricts_in_either_layer(
        NodeAllowIgnoringPolicy(), reference_calls(), capture, "node"
    )


def any_deny_refuses(capture: Capture) -> None:
    policy_cases.test_policy_allows_what_no_rule_refuses(
        AnyDenyPolicy(), reference_calls(), capture
    )


def first_deny_only(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        FirstDenyOnlyPolicy(), reference_calls(), capture, "namespace"
    )


def subagent_skipped(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        SubagentSkippingPolicy(), reference_calls(), capture, "node"
    )


def allow_overrides_deny(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        AllowOverridesDenyPolicy(), reference_calls(), capture, "namespace"
    )


def cached_by_tool(capture: Capture) -> None:
    policy_cases.test_policy_decisions_are_deterministic(
        ToolCachingPolicy(), reference_calls(), capture
    )


def exact_protected_path(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        ExactProtectedPathPolicy(), reference_calls(), capture
    )


def silent_validate(capture: Capture) -> None:
    policy_cases.test_policy_validate_refuses_a_malformed_policy(
        SilentValidatePolicy(), reference_calls()
    )


def loose_decision(capture: Capture) -> None:
    policy_cases.test_policy_allows_what_no_rule_refuses(
        LooseDecisionPolicy(), reference_calls(), capture
    )


def echoing_policy_failure(capture: Capture) -> None:
    common_cases.test_failure_is_a_bounded_protocol_error(
        echoing_policy_exercise, capture
    )


def subagent_protection_skipped(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        SubagentProtectionSkippingPolicy(), reference_calls(), capture
    )


def shell_only_rules(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        ShellOnlyRulesPolicy(), reference_calls(), capture, "namespace"
    )


def hardcoded_tests(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        HardcodedTestsPolicy(), reference_calls(), capture
    )


def non_bool_allow(capture: Capture) -> None:
    policy_cases.test_policy_allows_what_no_rule_refuses(
        NonBoolAllowPolicy(), reference_calls(), capture
    )


def contradicting_reason(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        ContradictingReasonPolicy(), reference_calls(), capture, "node"
    )


def layers_skipped_under_protection(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        LayersSkippedUnderProtectionPolicy(), reference_calls(), capture, "node"
    )


def cached_by_edit_tool(capture: Capture) -> None:
    policy_cases.test_policy_decisions_are_deterministic(
        EditCachingPolicy(), reference_calls(), capture
    )


def first_protected_only(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        FirstProtectedOnlyPolicy(), reference_calls(), capture
    )


def wrong_rule_id(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        WrongRuleIdPolicy(), reference_calls(), capture, "namespace"
    )


def formless_reason(capture: Capture) -> None:
    policy_cases.test_policy_allows_what_no_rule_refuses(
        FormlessReasonPolicy(), reference_calls(), capture
    )


def drifting_reason(capture: Capture) -> None:
    policy_cases.test_policy_decisions_are_deterministic(
        DriftingReasonPolicy(), reference_calls(), capture
    )


def tuple_validate(capture: Capture) -> None:
    policy_cases.test_policy_validate_refuses_a_malformed_policy(
        TupleValidatePolicy(), reference_calls()
    )


def pathless_validate(capture: Capture) -> None:
    policy_cases.test_policy_validate_refuses_a_malformed_policy(
        PathlessValidatePolicy(), reference_calls()
    )


def echoing_validate(capture: Capture) -> None:
    policy_cases.test_policy_validate_refuses_a_malformed_policy(
        EchoingValidatePolicy(), reference_calls()
    )


def unmatrixed_harness(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        ReferencePolicy(),
        calls_with_edits(edit_calls(("Edit", "Edit", "acp"))),
        capture,
    )


def mislabelled_builder(capture: Capture) -> None:
    edits = edit_calls(
        ("Edit", "Edit", "claude"),
        ("Write", "Edit", "claude"),
        ("NotebookEdit", "NotebookEdit", "claude"),
    )
    policy_cases.test_policy_protected_path_is_denied(
        ReferencePolicy(), calls_with_edits(edits), capture
    )


def wrong_harness_label(capture: Capture) -> None:
    edits = edit_calls(
        ("Edit", "Edit", "claude"),
        ("Write", "Write", "claude"),
        ("NotebookEdit", "NotebookEdit", "codex"),
    )
    policy_cases.test_policy_protected_path_is_denied(
        ReferencePolicy(), calls_with_edits(edits), capture
    )


def missing_edit_tool(capture: Capture) -> None:
    edits = edit_calls(("Edit", "Edit", "claude"), ("Write", "Write", "claude"))
    policy_cases.test_policy_protected_path_is_denied(
        ReferencePolicy(), calls_with_edits(edits), capture
    )


def last_deny_only(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        LastDenyOnlyPolicy(), reference_calls(), capture, "node"
    )


def glob_only_protection(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        GlobOnlyProtectionPolicy(), reference_calls(), capture
    )


def protection_blind_cache(capture: Capture) -> None:
    policy_cases.test_policy_decisions_are_deterministic(
        ProtectionBlindCachePolicy(), reference_calls(), capture
    )


def misattributed_protection(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        MisattributedProtectionPolicy(), reference_calls(), capture
    )


def no_edit_calls(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        ReferencePolicy(), calls_with_edits(()), capture
    )


def specific_allow_wins(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        SpecificAllowWinsPolicy(), reference_calls(), capture, "namespace"
    )


def argument_allow_skips_protection(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        ArgumentAllowSkipsProtectionPolicy(), reference_calls(), capture
    )


def node_denies_replace_namespace(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        NodeDenyReplacesNamespacePolicy(), reference_calls(), capture, "namespace"
    )


def first_two_denies(capture: Capture) -> None:
    policy_cases.test_policy_deny_in_either_layer_denies(
        FirstTwoDeniesPolicy(), reference_calls(), capture, "node"
    )


def first_two_protected(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        FirstTwoProtectedPolicy(), reference_calls(), capture
    )


def single_entry_allow_only(capture: Capture) -> None:
    policy_cases.test_policy_allow_list_restricts_in_either_layer(
        SingleEntryAllowOnlyPolicy(), reference_calls(), capture, "namespace"
    )


def cached_by_identity(capture: Capture) -> None:
    policy_cases.test_policy_decisions_are_deterministic(
        IdentityCachedDriftPolicy(), reference_calls(), capture
    )


def mapping_validate(capture: Capture) -> None:
    policy_cases.test_policy_validate_refuses_a_malformed_policy(
        MappingValidatePolicy(), reference_calls()
    )


def unmatched_narrow_pattern(capture: Capture) -> None:
    calls = dataclasses.replace(reference_calls(), narrow="Bash(nothing)")
    policy_cases.test_policy_allows_what_no_rule_refuses(
        ReferencePolicy(), calls, capture
    )


def unprotected_naming(tool: str) -> Callable[[str], str]:
    def naming(path: str) -> str:
        return f"{tool}({UNPROTECTED_PATH})"

    return naming


def fixed_path_naming(capture: Capture) -> None:
    edits = tuple(
        dataclasses.replace(edit, naming=unprotected_naming(edit.tool))
        for edit in reference_calls().edits
    )
    policy_cases.test_policy_allows_what_no_rule_refuses(
        ReferencePolicy(), calls_with_edits(edits), capture
    )


def nested_node_skipped(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        NestedNodeSkippingPolicy(), reference_calls(), capture
    )


BROKEN_CASES: tuple[tuple[Callable[[Capture], None], str], ...] = (
    (mutable_capabilities, "capabilities() returns a frozen dataclass"),
    (array_config_schema, "config_schema has type object"),
    (echoing_failure, "the message carries no input"),
    (silent_failure, "the provider logs through the context's logger"),
    (echoing_missing_reference, "a refused reference is not retryable"),
    (echoing_forbidden_reference, "a refused reference is not retryable"),
    (chained_forbidden_reference, "the error carries no chain"),
    (name_echoing_missing_reference, "the message and details carry no reference"),
    (leaking_names, "no name is a value"),
    (unprefixed_names, "every name has the requested prefix"),
    (silent_names, "the listed prefix names at least one secret"),
    (wrong_value_resolution, "resolve returns the referenced value"),
    (leaking_resolution, "no captured record carries the secret"),
    (unmasked_stream, "every escaped form of the secret is masked"),
    (ignored_node_allow_list, "an allow list that does not match denies"),
    (any_deny_refuses, "a call no rule refuses is allowed"),
    (first_deny_only, "a matching deny denies"),
    (subagent_skipped, "a matching deny denies"),
    (allow_overrides_deny, "a matching deny denies"),
    (cached_by_tool, "interleaved requests keep their own decisions"),
    (exact_protected_path, "a protected path is denied"),
    (silent_validate, "validate refuses the malformed policy"),
    (loose_decision, "decide returns a PolicyDecision"),
    (echoing_policy_failure, "the message carries no input"),
    (subagent_protection_skipped, "a protected path is denied"),
    (shell_only_rules, "a matching deny denies"),
    (hardcoded_tests, "a protected path is denied"),
    (non_bool_allow, "allow is an exact bool"),
    (contradicting_reason, "the reason agrees with the decision"),
    (layers_skipped_under_protection, "a matching deny denies"),
    (cached_by_edit_tool, "a rule id cites an existing pattern"),
    (first_protected_only, "a protected path is denied"),
    (wrong_rule_id, "a deny cites the matching rule"),
    (formless_reason, "the reason is a rule id or a policy reason"),
    (drifting_reason, "identical requests give identical reasons"),
    (tuple_validate, "validate returns ContractViolations"),
    (pathless_validate, "each violation has a path"),
    (echoing_validate, "no violation carries the policy's text"),
    (unmatrixed_harness, "every named harness is in the coverage matrix"),
    (mislabelled_builder, "each edit builder makes a call of its own tool"),
    (wrong_harness_label, "every file-edit tool of each named harness is covered"),
    (missing_edit_tool, "every file-edit tool of each named harness is covered"),
    (last_deny_only, "a matching deny denies"),
    (glob_only_protection, "a protected path is denied"),
    (protection_blind_cache, "interleaved requests keep their own decisions"),
    (misattributed_protection, "a protected path is not attributed to a rule"),
    (no_edit_calls, "the adopter supplies at least one file-edit call"),
    (specific_allow_wins, "a matching deny denies"),
    (argument_allow_skips_protection, "a protected path is denied"),
    (node_denies_replace_namespace, "a matching deny denies"),
    (first_two_denies, "a matching deny denies"),
    (first_two_protected, "a protected path is denied"),
    (single_entry_allow_only, "an allow list that does not match denies"),
    (cached_by_identity, "identical requests give identical reasons"),
    (mapping_validate, "validate returns ContractViolations"),
    (nested_node_skipped, "a protected path is denied"),
    (unmatched_narrow_pattern, "a call no rule refuses is allowed"),
    (fixed_path_naming, "a call no rule refuses is allowed"),
    *VCS_BROKEN_CASES,
    *HARNESS_BROKEN_CASES,
)


@pytest.mark.parametrize(
    ("case", "message"), BROKEN_CASES, ids=[case.__name__ for case, _ in BROKEN_CASES]
)
def test_cases_fail_for_a_broken_provider(
    case: Callable[[Capture], None], message: str
) -> None:
    failing = pytest.raises(pytest.fail.Exception)

    with failing as raised:
        run_case(case)

    assert str(raised.value) == message
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


def policy_validate(capture: Capture) -> None:
    policy_cases.test_policy_validate_refuses_a_malformed_policy(
        ReferencePolicy(), reference_calls()
    )


def policy_deny(capture: Capture) -> None:
    for layer in policy_cases.LAYERS:
        policy_cases.test_policy_deny_in_either_layer_denies(
            ReferencePolicy(), reference_calls(), capture, layer
        )


def policy_protected(capture: Capture) -> None:
    policy_cases.test_policy_protected_path_is_denied(
        ReferencePolicy(), reference_calls(), capture
    )


def policy_failure(capture: Capture) -> None:
    common_cases.test_failure_is_a_bounded_protocol_error(policy_exercise, capture)


def policy_allow_list(capture: Capture) -> None:
    for layer in policy_cases.LAYERS:
        policy_cases.test_policy_allow_list_restricts_in_either_layer(
            ReferencePolicy(), reference_calls(), capture, layer
        )


def policy_positive_control(capture: Capture) -> None:
    policy_cases.test_policy_allows_what_no_rule_refuses(
        ReferencePolicy(), reference_calls(), capture
    )


def policy_determinism(capture: Capture) -> None:
    policy_cases.test_policy_decisions_are_deterministic(
        ReferencePolicy(), reference_calls(), capture
    )


CYCLE_CASES: tuple[Callable[[Capture], None], ...] = (
    resolution,
    bounded_failure,
    policy_validate,
    policy_deny,
    policy_allow_list,
    policy_positive_control,
    policy_protected,
    policy_determinism,
    policy_failure,
)


def run_cycle() -> None:
    for case in CYCLE_CASES:
        run_case(case)


def test_cases_release_every_resource_after_repeated_runs() -> None:
    tracemalloc.start()
    try:
        for _ in range(WARM_UP):
            run_cycle()
        threads = threading.active_count()
        descriptors = descriptor_count()
        before = snapshot()

        for _ in range(CYCLES // 2):
            run_cycle()
        middle = snapshot()
        for _ in range(CYCLES - CYCLES // 2):
            run_cycle()
        retained = recurring_growth(before, middle, snapshot(), CYCLES)
    finally:
        tracemalloc.stop()

    assert threading.active_count() == threads
    assert descriptor_count() == descriptors
    assert current_diagnostics() is None
    assert retained < MAX_RETAINED_BYTES


def test_a_stuck_capture_is_bounded_and_reported() -> None:
    result = run_python(["-I", str(PROBE)], {})

    assert result.code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "timed_out": True,
        "refused": True,
        "held": 1,
    }
