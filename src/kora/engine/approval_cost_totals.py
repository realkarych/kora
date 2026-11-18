from dataclasses import dataclass, field
from datetime import datetime
from decimal import Context, Decimal, localcontext

from kora.db.approval_cost_reads import CostAttempt, CostUsage
from kora.db.cost_reads import CostReadRefused
from kora.ir.cost import (
    MAX_MONEY,
    MAX_SUM,
    Basis,
    BillingClass,
    Completeness,
    ModelCost,
    PricingStatus,
    RunCost,
    TokenField,
    format_money,
    preflight,
)
from kora.ir.json import CanonicalBudget, JsonShapeError

TOKEN_FIELDS: tuple[TokenField, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
)
COMPLETENESS: tuple[Completeness, ...] = ("unavailable", "partial", "complete")
MODEL_LIMIT: int = 1024


class ApprovalCostOverflow(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the approval cost exceeds its bound")


def weakest(left: Completeness, right: Completeness) -> Completeness:
    return COMPLETENESS[min(COMPLETENESS.index(left), COMPLETENESS.index(right))]


def add_count(left: int, right: int) -> int:
    if right < 0 or left > MAX_SUM - right:
        raise ApprovalCostOverflow()
    return left + right


def add_money(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if right is None:
        return left
    with localcontext(Context(prec=64)):
        value = (Decimal(0) if left is None else left) + right
    if value > MAX_MONEY:
        raise ApprovalCostOverflow()
    return value


def money_text(value: Decimal | None) -> str | None:
    return None if value is None else format_money(value)


def pricing_state(
    known: Decimal | None, all_priced: bool, only_subscription: bool
) -> PricingStatus:
    if all_priced:
        return "estimated"
    if only_subscription:
        return "not_billable"
    return "unpriced" if known is None else "partially_priced"


@dataclass
class ModelTotal:
    model: str
    tokens: dict[TokenField, int] = field(
        default_factory=lambda: dict.fromkeys(TOKEN_FIELDS, 0)
    )
    unknown: set[TokenField] = field(default_factory=set[TokenField])
    classes: set[BillingClass] = field(default_factory=set[BillingClass])
    bases: set[Basis] = field(default_factory=set[Basis])
    completeness: Completeness = "complete"
    known: Decimal | None = None
    all_priced: bool = True

    def add(self, saved: CostUsage) -> None:
        usage = saved.usage
        counts = (
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.cache_creation_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
        )
        for name, count in zip(TOKEN_FIELDS, counts, strict=True):
            self.tokens[name] = add_count(self.tokens[name], count)
        self.unknown.update(set(TOKEN_FIELDS) - usage.known_token_fields)
        self.classes.add(usage.billing_class)
        self.bases.add(usage.basis)
        self.completeness = weakest(self.completeness, usage.completeness)
        if saved.lifecycle.unsettled:
            self.completeness = weakest(self.completeness, "partial")
        self.known = add_money(self.known, saved.amount)
        self.all_priced = self.all_priced and saved.amount is not None

    def value(self) -> ModelCost:
        state = pricing_state(
            self.known, self.all_priced, self.classes == {"subscription"}
        )
        known = money_text(self.known)
        return ModelCost(
            model=self.model,
            input_tokens=self.tokens["input_tokens"],
            cached_input_tokens=self.tokens["cached_input_tokens"],
            cache_creation_tokens=self.tokens["cache_creation_tokens"],
            output_tokens=self.tokens["output_tokens"],
            reasoning_tokens=self.tokens["reasoning_tokens"],
            unknown_token_fields=tuple(sorted(self.unknown)),
            completeness=self.completeness,
            usd_estimate=known if state == "estimated" else None,
            known_usd_estimate=known,
            pricing_status=state,
            billing_classes=tuple(sorted(self.classes)),
            bases=tuple(sorted(self.bases)),
        )


@dataclass
class ApprovalCostTotals:
    as_of: datetime
    models: dict[str, ModelTotal] = field(default_factory=dict[str, ModelTotal])
    completeness: Completeness = "complete"
    observed_at: datetime | None = None
    covered: int = 0
    missing: int = 0
    attribution_gaps: int = 0
    observation_gaps: int = 0

    def add_attempt(self, saved: CostAttempt) -> None:
        if (
            saved.kind == "agent"
            and saved.started_at is None
            and (
                saved.lease_epoch != 0
                or saved.domain_recorded
                or saved.worker_recorded
                or saved.execution_state in {"live", "unknown"}
            )
        ):
            raise CostReadRefused()
        if not saved.has_usage and (saved.kind != "agent" or saved.started_at is None):
            return
        self.covered = add_count(self.covered, 1)
        coverage = saved.coverage
        missing = (
            not saved.has_usage
            or coverage is None
            or coverage.observation_id is None
            or coverage.observed_at is None
        )
        if missing:
            self.missing = add_count(self.missing, 1)
            self.completeness = "unavailable"
        if coverage is not None:
            self.completeness = weakest(self.completeness, coverage.completeness)
            self.attribution_gaps = add_count(
                self.attribution_gaps, int(coverage.attribution_gap)
            )
            self.observation_gaps = add_count(
                self.observation_gaps, int(coverage.observation_gap)
            )
            if coverage.observed_at is not None:
                if coverage.observed_at > self.as_of:
                    raise CostReadRefused()
                self.observed_at = (
                    coverage.observed_at
                    if self.observed_at is None
                    else min(self.observed_at, coverage.observed_at)
                )
        if saved.unsettled:
            self.completeness = weakest(self.completeness, "partial")

    def add_usage(self, saved: CostUsage) -> None:
        usage = saved.usage
        if usage.observed_at > self.as_of:
            raise CostReadRefused()
        aggregate = self.models.get(usage.model)
        if aggregate is None:
            if len(self.models) >= MODEL_LIMIT:
                raise ApprovalCostOverflow()
            aggregate = ModelTotal(usage.model)
            self.models[usage.model] = aggregate
        aggregate.add(saved)

    def value(self) -> RunCost:
        models = tuple(self.models[name].value() for name in sorted(self.models))
        known: Decimal | None = Decimal(0) if self.covered == 0 else None
        for model in models:
            known = add_money(
                known,
                None
                if model.known_usd_estimate is None
                else Decimal(model.known_usd_estimate),
            )
            self.completeness = weakest(self.completeness, model.completeness)
        gaps = bool(self.missing or self.attribution_gaps or self.observation_gaps)
        state = pricing_state(
            known,
            not gaps and all(model.pricing_status == "estimated" for model in models),
            not gaps
            and bool(models)
            and all(model.pricing_status == "not_billable" for model in models),
        )
        value: dict[str, object] = {
            "version": 1,
            "as_of": self.as_of,
            "observed_at": None if self.missing else self.observed_at,
            "completeness": self.completeness,
            "usd_estimate": money_text(known) if state == "estimated" else None,
            "known_usd_estimate": money_text(known),
            "pricing_status": state,
            "missing_report_attempts": self.missing,
            "attribution_gap_attempts": self.attribution_gaps,
            "observation_gap_attempts": self.observation_gaps,
            "models": models,
        }
        try:
            preflight(value, CanonicalBudget(RunCost.byte_limit))
        except JsonShapeError as error:
            raise ApprovalCostOverflow() from error
        return RunCost.model_validate(value)
