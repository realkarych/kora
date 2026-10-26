from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from kora.ir import ErrorClass
from kora.protocols.harness import OutputCandidate, RunnerCause, UnconfirmedSample

CAUSE_CLASSES: Final[MappingProxyType[RunnerCause, ErrorClass]] = MappingProxyType(
    {
        "max_events": ErrorClass.EVENTS_EXHAUSTED,
        "budget": ErrorClass.BUDGET_EXHAUSTED,
        "stall": ErrorClass.STALLED,
        "deadline": ErrorClass.TIMEOUT,
        "cancel": ErrorClass.CANCELLED,
        "provider_limit": ErrorClass.PROVIDER_LIMITED,
        "policy_bypass": ErrorClass.HARNESS_ERROR,
        "version_mismatch": ErrorClass.CONFIG_ERROR,
        "plugin_mismatch": ErrorClass.CONFIG_ERROR,
        "integrity_mismatch": ErrorClass.INTEGRITY_ERROR,
        "config_check_failed": ErrorClass.CONFIG_ERROR,
        "policy_error": ErrorClass.CONFIG_ERROR,
        "policy_timeout": ErrorClass.INFRASTRUCTURE,
        "journal_unavailable": ErrorClass.INFRASTRUCTURE,
        "cli_exit_without_result": ErrorClass.HARNESS_ERROR,
        "oversized_message": ErrorClass.HARNESS_ERROR,
        "launch_rejected": ErrorClass.CONFIG_ERROR,
        "launch_failed": ErrorClass.INFRASTRUCTURE,
        "launcher_died": ErrorClass.INFRASTRUCTURE,
    }
)


@dataclass(frozen=True)
class Bypass:
    confirmed: bool
    unconfirmed: UnconfirmedSample


def final_class(
    cause: RunnerCause,
    result_class: ErrorClass | None,
    bypass: Bypass,
    provider_limit_seen: bool,
) -> ErrorClass | None:
    if bypass.confirmed:
        return ErrorClass.HARNESS_ERROR
    if cause != "completed":
        return CAUSE_CLASSES[cause]
    if provider_limit_seen and result_class is not None:
        return ErrorClass.PROVIDER_LIMITED
    return result_class


def select_output(
    structured: OutputCandidate | None, result_text: OutputCandidate | None
) -> OutputCandidate | None:
    if structured is not None:
        return structured
    return result_text
