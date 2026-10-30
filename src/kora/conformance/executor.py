from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from kora.conformance.bridge import PreparedInvocation
from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    INSTANCE_ID,
    MARKER,
    check_capabilities_are_frozen_and_consistent,
    check_config_schema_is_an_object_schema,
    check_failure_is_a_bounded_protocol_error,
    check_provider_logs_through_its_context,
    context_for,
    fail,
    utc_now,
)
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings
from kora.protocols.common import ProtocolContext, ProviderProtocol
from kora.protocols.metadata import ProtocolName

Check = Callable[[PreparedInvocation, Capture, ProtocolContext], Awaitable[None] | None]
Failure = Callable[[object, JsonObject, ProtocolContext, str], Awaitable[object]]
ConfigurationEffect = Callable[[object, JsonObject, ProtocolContext], Awaitable[None]]


@dataclass(frozen=True)
class Case:
    name: str
    check: Check


async def failure_case(
    invocation: PreparedInvocation, capture: Capture, context: ProtocolContext
) -> None:
    failure = cast(Failure, invocation.exercises.failure)
    await check_failure_is_a_bounded_protocol_error(
        lambda ctx, marker: failure(
            invocation.instance, invocation.configuration, ctx, marker
        ),
        context,
        MARKER,
    )


async def logging_case(
    invocation: PreparedInvocation, capture: Capture, context: ProtocolContext
) -> None:
    failure = cast(Failure, invocation.exercises.failure)
    await check_provider_logs_through_its_context(
        lambda ctx, marker: failure(
            invocation.instance, invocation.configuration, ctx, marker
        ),
        capture,
        context,
        MARKER,
    )


def common_cases() -> tuple[Case, ...]:
    return (
        Case(
            "capabilities",
            lambda i, c, x: check_capabilities_are_frozen_and_consistent(
                cast(ProviderProtocol, i.instance)
            ),
        ),
        Case(
            "schema",
            lambda i, c, x: check_config_schema_is_an_object_schema(
                cast(ProviderProtocol, i.instance)
            ),
        ),
        Case("failure", failure_case),
        Case("logging", logging_case),
        Case(
            "configuration",
            lambda i, c, x: cast(ConfigurationEffect, i.exercises.configuration_effect)(
                i.instance, i.configuration, x
            ),
        ),
    )


def secrets_cases() -> tuple[Case, ...]:
    from kora.conformance.secrets import (
        SecretsConformanceInputs,
        check_secrets_forbidden_reference_is_refused_like_a_missing_one,
        check_secrets_list_names_returns_names_only,
        check_secrets_missing_reference_is_a_bounded_error,
        check_secrets_resolution_returns_a_masked_value,
        check_secrets_value_is_masked_in_a_captured_stream,
    )
    from kora.protocols.secrets import SecretsProvider

    def inputs(i: PreparedInvocation) -> SecretsConformanceInputs:
        return cast(SecretsConformanceInputs, i.exercises)

    def provider(i: PreparedInvocation) -> SecretsProvider:
        return cast(SecretsProvider, i.instance)

    return (
        Case(
            "resolution",
            lambda i, c, x: check_secrets_resolution_returns_a_masked_value(
                provider(i), inputs(i).resolvable, c, x
            ),
        ),
        Case(
            "missing",
            lambda i, c, x: check_secrets_missing_reference_is_a_bounded_error(
                provider(i), inputs(i).missing_reference, x
            ),
        ),
        Case(
            "forbidden",
            lambda i, c, x: (
                check_secrets_forbidden_reference_is_refused_like_a_missing_one(
                    provider(i), inputs(i).forbidden_reference, x
                )
            ),
        ),
        Case(
            "names",
            lambda i, c, x: check_secrets_list_names_returns_names_only(
                provider(i), inputs(i).listed_prefix, inputs(i).resolvable
            ),
        ),
        Case(
            "stream",
            lambda i, c, x: check_secrets_value_is_masked_in_a_captured_stream(
                provider(i), inputs(i).resolvable, c, x
            ),
        ),
    )


def policy_cases() -> tuple[Case, ...]:
    from kora.conformance.policy import (
        LAYERS,
        PolicyConformanceInputs,
        check_policy_allow_list_restricts_in_either_layer,
        check_policy_allows_what_no_rule_refuses,
        check_policy_decisions_are_deterministic,
        check_policy_deny_in_either_layer_denies,
        check_policy_protected_path_is_denied,
        check_policy_validate_refuses_a_malformed_policy,
    )
    from kora.protocols.policy import Policy

    def inputs(i: PreparedInvocation) -> PolicyConformanceInputs:
        return cast(PolicyConformanceInputs, i.exercises)

    def provider(i: PreparedInvocation) -> Policy:
        return cast(Policy, i.instance)

    def layer_cases(layer: str) -> tuple[Case, ...]:
        return (
            Case(
                f"deny_{layer}",
                lambda i, c, x: check_policy_deny_in_either_layer_denies(
                    provider(i), inputs(i).calls, x, layer
                ),
            ),
            Case(
                f"allow_{layer}",
                lambda i, c, x: check_policy_allow_list_restricts_in_either_layer(
                    provider(i), inputs(i).calls, x, layer
                ),
            ),
        )

    return (
        Case(
            "validation",
            lambda i, c, x: check_policy_validate_refuses_a_malformed_policy(
                provider(i), inputs(i).calls
            ),
        ),
        *(case for layer in LAYERS for case in layer_cases(layer)),
        Case(
            "allowed",
            lambda i, c, x: check_policy_allows_what_no_rule_refuses(
                provider(i), inputs(i).calls, x
            ),
        ),
        Case(
            "protected",
            lambda i, c, x: check_policy_protected_path_is_denied(
                provider(i), inputs(i).calls, x
            ),
        ),
        Case(
            "deterministic",
            lambda i, c, x: check_policy_decisions_are_deterministic(
                provider(i), inputs(i).calls, x
            ),
        ),
    )


def harness_cases() -> tuple[Case, ...]:
    from kora.conformance.harness import (
        MAX_EVENTS,
        HarnessCase,
        HarnessConformanceInputs,
        HarnessHost,
        HostedRun,
        check_harness_events_are_normalised,
        check_harness_max_events_denies_at_exact_count,
        check_harness_policy_precedes_every_tool_call,
        check_harness_structured_output_and_fallbacks,
        check_harness_usage_completeness,
        run_harness_case,
    )
    from kora.protocols.harness import HarnessAdapter

    async def run(
        i: PreparedInvocation, x: ProtocolContext, scenario: HarnessCase
    ) -> HostedRun:
        return await run_harness_case(
            HarnessHost(),
            cast(HarnessAdapter, i.instance),
            i.configuration,
            x,
            cast(HarnessConformanceInputs, i.exercises),
            scenario,
            MAX_EVENTS if scenario is HarnessCase.MAX_EVENTS else None,
        )

    def single(
        name: str, scenario: HarnessCase, check: Callable[[HostedRun], None]
    ) -> Case:
        async def exercise(
            i: PreparedInvocation, c: Capture, x: ProtocolContext
        ) -> None:
            check(await run(i, x, scenario))

        return Case(name, exercise)

    def paired(
        name: str,
        first: HarnessCase,
        second: HarnessCase,
        check: Callable[[HostedRun, HostedRun], None],
    ) -> Case:
        async def exercise(
            i: PreparedInvocation, c: Capture, x: ProtocolContext
        ) -> None:
            check(await run(i, x, first), await run(i, x, second))

        return Case(name, exercise)

    return (
        single("events", HarnessCase.TOOL_CALLS, check_harness_events_are_normalised),
        single(
            "policy",
            HarnessCase.TOOL_CALLS,
            check_harness_policy_precedes_every_tool_call,
        ),
        single(
            "limit",
            HarnessCase.MAX_EVENTS,
            check_harness_max_events_denies_at_exact_count,
        ),
        paired(
            "usage",
            HarnessCase.USAGE,
            HarnessCase.NO_USAGE,
            check_harness_usage_completeness,
        ),
        paired(
            "output",
            HarnessCase.STRUCTURED_OUTPUT,
            HarnessCase.RESULT_TEXT,
            check_harness_structured_output_and_fallbacks,
        ),
    )


def vcs_cases() -> tuple[Case, ...]:
    from kora.conformance.vcs import (
        VcsBench,
        VcsBinding,
        VcsConformanceInputs,
        VcsRemote,
        check_vcs_api_credential_is_refused,
        check_vcs_checkout_captures_requested_base,
        check_vcs_credential_binding_mismatch_is_refused,
        check_vcs_head_sha,
        check_vcs_local_credential_carries_no_secret,
        check_vcs_private_files_stay_outside_candidate,
        check_vcs_repeated_finalize_returns_one_ref,
        check_vcs_sealed_transfer_fixes_inspected_revision,
    )
    from kora.protocols.vcs import Vcs

    def inputs(i: PreparedInvocation) -> VcsConformanceInputs:
        return cast(VcsConformanceInputs, i.exercises)

    def provider(i: PreparedInvocation) -> Vcs:
        return cast(Vcs, i.instance)

    async def bench_for(i: PreparedInvocation, x: ProtocolContext) -> VcsBench:
        bench = await inputs(i).bench(provider(i), i.configuration, x)
        executor = bench.executor
        if any(
            receiver is not i.instance
            for receiver in (executor.destinations, executor.sources, executor.heads)
        ):
            fail("conformance executor receiver was replaced")
        return bench

    def checkout(
        name: str,
        check: Callable[[Vcs, VcsRemote, VcsBench, ProtocolContext], Awaitable[None]],
    ) -> Case:
        async def exercise(
            i: PreparedInvocation, c: Capture, x: ProtocolContext
        ) -> None:
            bundle = inputs(i)
            bench = await bench_for(i, x)
            await check(provider(i), bundle.remote, bench, x)

        return Case(name, exercise)

    def publish(
        name: str,
        check: Callable[
            [Vcs, VcsRemote, VcsBench, ProtocolContext, VcsBinding], Awaitable[None]
        ],
    ) -> Case:
        async def exercise(
            i: PreparedInvocation, c: Capture, x: ProtocolContext
        ) -> None:
            bundle = inputs(i)
            bench = await bench_for(i, x)
            remote = bundle.head_remote if name == "head" else bundle.remote
            await check(provider(i), remote, bench, x, bundle.binding)

        return Case(name, exercise)

    def credential(
        name: str,
        check: Callable[[Vcs, VcsRemote, ProtocolContext, VcsBinding], Awaitable[None]],
    ) -> Case:
        return Case(
            name,
            lambda i, c, x: check(provider(i), inputs(i).remote, x, inputs(i).binding),
        )

    return (
        checkout("checkout", check_vcs_checkout_captures_requested_base),
        checkout("private_files", check_vcs_private_files_stay_outside_candidate),
        publish("sealed", check_vcs_sealed_transfer_fixes_inspected_revision),
        publish("finalize", check_vcs_repeated_finalize_returns_one_ref),
        publish("head", check_vcs_head_sha),
        credential("local_credential", check_vcs_local_credential_carries_no_secret),
        credential("binding", check_vcs_credential_binding_mismatch_is_refused),
        credential("api_credential", check_vcs_api_credential_is_refused),
    )


def storage_cases() -> tuple[Case, ...]:
    from kora.conformance.artifact_storage import (
        STAGING_SIZES,
        check_artifact_storage_deletion_lifetime,
        check_artifact_storage_inventory_is_bounded,
        check_artifact_storage_promotion_is_create_only,
        check_artifact_storage_promotion_replay_preserves_outcome,
        check_artifact_storage_range_lifetime,
        check_artifact_storage_range_reads_are_exact,
        check_artifact_storage_staging_cleanup_is_confined,
        check_artifact_storage_staging_lifetime,
        check_artifact_storage_staging_round_trip,
    )
    from kora.protocols.storage import ArtifactStorage

    def provider(i: PreparedInvocation) -> ArtifactStorage:
        return cast(ArtifactStorage, i.instance)

    def staging(size: int) -> Case:
        return Case(
            f"round_trip_{size}",
            lambda i, c, x: check_artifact_storage_staging_round_trip(
                provider(i), x, i.reconstruct, size
            ),
        )

    def simple(
        name: str, check: Callable[[ArtifactStorage, ProtocolContext], Awaitable[None]]
    ) -> Case:
        return Case(name, lambda i, c, x: check(provider(i), x))

    return (
        *(staging(size) for size in STAGING_SIZES),
        simple("promotion", check_artifact_storage_promotion_is_create_only),
        Case(
            "replay",
            lambda i, c, x: check_artifact_storage_promotion_replay_preserves_outcome(
                provider(i), x, i.reconstruct
            ),
        ),
        Case(
            "confined_cleanup",
            lambda i, c, x: check_artifact_storage_staging_cleanup_is_confined(
                provider(i), x, i.reconstruct
            ),
        ),
        simple("staging_lifetime", check_artifact_storage_staging_lifetime),
        simple("deletion", check_artifact_storage_deletion_lifetime),
        simple("inventory", check_artifact_storage_inventory_is_bounded),
        simple("range", check_artifact_storage_range_reads_are_exact),
        simple("range_lifetime", check_artifact_storage_range_lifetime),
    )


def artifact_type_cases() -> tuple[Case, ...]:
    from kora.conformance.artifact_type import (
        ArtifactTypeConformanceInputs,
        check_artifact_type_accepts_declared_values,
        check_artifact_type_digest_fields_are_stable,
        check_artifact_type_invalid_extraction_is_refused,
        check_artifact_type_rejects_declared_values,
        check_artifact_type_summaries_are_detached,
    )
    from kora.protocols.artifact_type import ArtifactType

    def simple(
        name: str,
        check: Callable[
            [ArtifactType, ArtifactTypeConformanceInputs, ProtocolContext], None
        ],
    ) -> Case:
        return Case(
            name,
            lambda i, c, x: check(
                cast(ArtifactType, i.instance),
                cast(ArtifactTypeConformanceInputs, i.exercises),
                x,
            ),
        )

    return (
        simple("accepted", check_artifact_type_accepts_declared_values),
        simple("rejected", check_artifact_type_rejects_declared_values),
        simple("digest_fields", check_artifact_type_digest_fields_are_stable),
        simple("summary", check_artifact_type_summaries_are_detached),
        simple("invalid_extraction", check_artifact_type_invalid_extraction_is_refused),
    )


def authentication_cases() -> tuple[Case, ...]:
    from kora.conformance.authentication import (
        AuthenticationConformanceInputs,
        check_authentication_preserves_principals,
        check_authentication_refusals_are_uniform,
        check_authentication_revalidates_current_authority,
        check_authentication_revocation_is_permanent,
        check_authentication_unavailability_grants_no_authority,
        check_authentication_unexpected_failure_grants_no_authority,
    )
    from kora.protocols.authentication import Authenticator

    def simple(
        name: str,
        check: Callable[
            [Authenticator, AuthenticationConformanceInputs, ProtocolContext],
            Awaitable[None],
        ],
    ) -> Case:
        return Case(
            name,
            lambda i, c, x: check(
                cast(Authenticator, i.instance),
                cast(AuthenticationConformanceInputs, i.exercises),
                x,
            ),
        )

    return (
        simple("principals", check_authentication_preserves_principals),
        simple("refusals", check_authentication_refusals_are_uniform),
        simple("current_authority", check_authentication_revalidates_current_authority),
        simple("revocation", check_authentication_revocation_is_permanent),
        simple(
            "unexpected_failure",
            check_authentication_unexpected_failure_grants_no_authority,
        ),
        simple(
            "unavailability", check_authentication_unavailability_grants_no_authority
        ),
    )


def selected_cases(protocol: ProtocolName) -> tuple[Case, ...]:
    builders = {
        ProtocolName.AUTHENTICATION: authentication_cases,
        ProtocolName.RUNTIME: lambda: (),
        ProtocolName.SECRETS: secrets_cases,
        ProtocolName.POLICY: policy_cases,
        ProtocolName.HARNESS: harness_cases,
        ProtocolName.VCS: vcs_cases,
        ProtocolName.ARTIFACT_STORAGE: storage_cases,
        ProtocolName.ARTIFACT_TYPE: artifact_type_cases,
    }
    return (*common_cases(), *builders[protocol]())


class Executor:
    def __init__(self, protocol: ProtocolName) -> None:
        self.cases = selected_cases(protocol)
        self._completed: list[str] = []
        self._capture_settings: LoggingSettings
        if protocol is ProtocolName.AUTHENTICATION:
            from kora.conformance.authentication import authentication_capture_settings

            self._capture_settings = authentication_capture_settings()
        elif protocol is ProtocolName.ARTIFACT_TYPE:
            from kora.conformance.artifact_type import artifact_type_capture_settings

            self._capture_settings = artifact_type_capture_settings()
        else:
            self._capture_settings = LoggingSettings()

    @property
    def complete(self) -> bool:
        return tuple(self._completed) == tuple(case.name for case in self.cases)

    @property
    def completed(self) -> int:
        return len(self._completed)

    async def run(self, case: Case, invocation: PreparedInvocation) -> None:
        if self.complete or case is not self.cases[len(self._completed)]:
            fail("conformance case sequence is invalid")
        with captured(
            CONFORMANCE_COMPONENT, self._capture_settings, INSTANCE_ID, utc_now
        ) as capture:
            result = case.check(invocation, capture, context_for(capture))
            if result is not None:
                await result
        self._completed.append(case.name)
