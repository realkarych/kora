import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from kora.ir.cost import (
    Basis,
    BillingClass,
    PriceRevision,
    UsageCoverage,
    UsageRow,
    parse_money,
)
from kora.ir.pricing import estimate_usage_usd

MAX_USAGE_MODELS: Final[int] = 64
COMPLETENESS_ORDER: Final[dict[str, int]] = {
    "unavailable": 0,
    "partial": 1,
    "complete": 2,
}


class UsageRefusal(StrEnum):
    CONFLICT = "usage_conflict"
    REGRESSION = "usage_regression"
    MODELS = "usage_models"
    MAPPING = "usage_mapping"


class UsageRejected(ValueError):
    def __init__(self, reason: UsageRefusal) -> None:
        super().__init__("the usage report is not admissible")
        self.reason = reason


@dataclass(frozen=True)
class UsageMapping:
    allowed: frozenset[tuple[Basis, BillingClass]]
    corrections: frozenset[tuple[Basis, Basis]]
    delta_of_session_totals: bool


@dataclass(frozen=True, repr=False)
class PricedUsage:
    row: UsageRow
    usd_estimate: str | None
    dispatcher_id: str
    same_basis_usd_floor: str | None


@dataclass(frozen=True, repr=False)
class UsageAcceptance:
    accepted_rows: tuple[PricedUsage, ...]
    changed_rows: tuple[PricedUsage, ...]
    coverage: UsageCoverage
    coverage_changed: bool


def row_identity(row: UsageRow) -> str:
    return json.dumps(
        row.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def changed_coverage(before: UsageCoverage | None, after: UsageCoverage) -> bool:
    if before is None:
        return True
    if before.observation_sequence == after.observation_sequence:
        if before != after:
            raise UsageRejected(UsageRefusal.CONFLICT)
        return False
    if after.observation_sequence is None or (
        before.observation_sequence is not None
        and after.observation_sequence < before.observation_sequence
    ):
        raise UsageRejected(UsageRefusal.REGRESSION)
    return True


def token_counts(row: UsageRow) -> tuple[int, ...]:
    return (
        row.input_tokens,
        row.cached_input_tokens,
        row.cache_creation_tokens,
        row.output_tokens,
        row.reasoning_tokens,
    )


def replacement(
    before: PricedUsage,
    after: UsageRow,
    price: PriceRevision,
    mapping: UsageMapping,
    dispatcher_id: str,
) -> PricedUsage | None:
    previous = before.row
    if previous.observation_sequence == after.observation_sequence:
        if row_identity(previous) != row_identity(after):
            raise UsageRejected(UsageRefusal.CONFLICT)
        return None
    if (
        after.observation_sequence < previous.observation_sequence
        or any(
            current < old
            for old, current in zip(
                token_counts(previous), token_counts(after), strict=True
            )
        )
        or (
            before.dispatcher_id != dispatcher_id
            and COMPLETENESS_ORDER[after.completeness]
            < COMPLETENESS_ORDER[previous.completeness]
        )
        or after.billing_class != previous.billing_class
    ):
        raise UsageRejected(UsageRefusal.REGRESSION)
    correction = previous.basis != after.basis
    if correction and (
        (previous.basis, after.basis) not in mapping.corrections
        or after.reported_usd is None
    ):
        raise UsageRejected(UsageRefusal.REGRESSION)
    if (
        not correction
        and previous.basis in {"harness_list", "harness_managed"}
        and previous.reported_usd is not None
        and (
            after.reported_usd is None
            or parse_money(after.reported_usd) < parse_money(previous.reported_usd)
        )
    ):
        raise UsageRejected(UsageRefusal.REGRESSION)
    estimate = estimate_usage_usd(after, price)
    if (
        not correction
        and before.same_basis_usd_floor is not None
        and estimate is not None
        and parse_money(estimate) < parse_money(before.same_basis_usd_floor)
    ):
        raise UsageRejected(UsageRefusal.REGRESSION)
    floor = (
        estimate if estimate is not None or correction else before.same_basis_usd_floor
    )
    return PricedUsage(after, estimate, dispatcher_id, floor)


def accept_usage(
    prior: tuple[PricedUsage, ...],
    prior_coverage: UsageCoverage | None,
    rows: tuple[UsageRow, ...],
    coverage: UsageCoverage,
    price: PriceRevision,
    mapping: UsageMapping,
    dispatcher_id: str,
) -> UsageAcceptance:
    if len(prior) > MAX_USAGE_MODELS or len({item.row.model for item in prior}) != len(
        prior
    ):
        raise RuntimeError("the stored usage inventory is invalid")
    if len(rows) > MAX_USAGE_MODELS or len({row.model for row in rows}) != len(rows):
        raise UsageRejected(UsageRefusal.MODELS)
    previous = {item.row.model: item for item in prior}
    if len(previous.keys() | {row.model for row in rows}) > MAX_USAGE_MODELS:
        raise UsageRejected(UsageRefusal.MODELS)
    coverage_changed = changed_coverage(prior_coverage, coverage)
    accepted: list[PricedUsage] = []
    changed: list[PricedUsage] = []
    for row in rows:
        if (row.basis, row.billing_class) not in mapping.allowed or (
            row.delta_of_session_totals != mapping.delta_of_session_totals
        ):
            raise UsageRejected(UsageRefusal.MAPPING)
        current = previous.get(row.model)
        candidate = (
            first_usage(row, price, dispatcher_id)
            if current is None
            else replacement(current, row, price, mapping, dispatcher_id)
        )
        if candidate is None:
            if current is None:
                raise RuntimeError("the replayed usage row is missing")
            accepted.append(current)
        else:
            changed.append(candidate)
            accepted.append(candidate)
    return UsageAcceptance(tuple(accepted), tuple(changed), coverage, coverage_changed)


def first_usage(row: UsageRow, price: PriceRevision, dispatcher_id: str) -> PricedUsage:
    estimate = estimate_usage_usd(row, price)
    return PricedUsage(row, estimate, dispatcher_id, estimate)
