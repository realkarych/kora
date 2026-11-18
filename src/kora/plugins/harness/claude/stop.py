from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from kora.ir import ErrorClass
from kora.ir.cost import Completeness, UsageRow
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import DenialSample, RunnerCause
from kora.runner.outcome import Bypass, final_class

CONFIG_ERRORS = frozenset(
    {"authentication_failed", "billing_error", "account_on_hold", "credits_required"}
)


def refused() -> NoReturn:
    raise KoraProtocolError("harness_error", "the Claude session failed", False, {})


@dataclass(frozen=True)
class RecordedResult:
    subtype: str
    is_error: bool
    terminal_reason: str | None


def result_class(
    result: RecordedResult, assistant_errors: Set[str]
) -> ErrorClass | None:
    if result.subtype == "success" and not result.is_error:
        return None
    if result.terminal_reason == "max_turns" or result.subtype == "error_max_turns":
        return ErrorClass.EVENTS_EXHAUSTED
    if result.subtype == "success" and assistant_errors & CONFIG_ERRORS:
        return ErrorClass.CONFIG_ERROR
    if result.subtype == "error_max_budget_usd":
        return ErrorClass.BUDGET_EXHAUSTED
    if result.subtype == "error_max_structured_output_retries":
        return ErrorClass.ARTIFACT_INVALID
    return ErrorClass.HARNESS_ERROR


@dataclass(frozen=True)
class UsageSummary:
    rows: tuple[UsageRow, ...]
    completeness: Completeness
    attribution_gap: bool
    observation_gap: bool
    observed_at: datetime | None


@dataclass(frozen=True)
class ClaudeObservation:
    result_seen: bool
    result_class: ErrorClass | None
    provider_limit_seen: bool
    terminal_reason: str | None
    api_error_status: int | None
    denials: DenialSample
    denials_complete: bool
    unmatched_denials: int
    bypass: Bypass
    usage: UsageSummary

    def error_class(self, cause: RunnerCause) -> ErrorClass | None:
        return final_class(
            cause, self.result_class, self.bypass, self.provider_limit_seen
        )
