from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from kora.ir.cost import (
    MAX_MONEY,
    PriceRates,
    PriceRevision,
    UsageRow,
    quantize_money,
)
from kora.ir.cost import (
    MoneyOverflow as PricingOverflow,
)


def charge(tokens: int, rate: str | None) -> Decimal | None:
    if tokens == 0:
        return Decimal(0)
    return None if rate is None else Decimal(tokens) * Decimal(rate)


def cache_charge(row: UsageRow, rates: PriceRates) -> Decimal | None:
    if "cache_creation_tokens" not in row.known_token_fields:
        return None
    if row.cache_creation_tokens == 0:
        return Decimal(0)
    if row.cache_creation_by_ttl is None:
        if rates.cache_creation_5m != rates.cache_creation_1h:
            return None
        return charge(row.cache_creation_tokens, rates.cache_creation_5m)
    short = charge(row.cache_creation_by_ttl["5m"], rates.cache_creation_5m)
    long = charge(row.cache_creation_by_ttl["1h"], rates.cache_creation_1h)
    return None if short is None or long is None else short + long


def output_charge(row: UsageRow, rates: PriceRates) -> Decimal | None:
    if "output_tokens" not in row.known_token_fields:
        return None
    if row.output_tokens == 0:
        return Decimal(0)
    if "reasoning_tokens" not in row.known_token_fields:
        if rates.output != rates.reasoning:
            return None
        return charge(row.output_tokens, rates.output)
    output = charge(row.output_tokens - row.reasoning_tokens, rates.output)
    reasoning = charge(row.reasoning_tokens, rates.reasoning)
    return None if output is None or reasoning is None else output + reasoning


def estimate_usage_usd(row: UsageRow, revision: PriceRevision) -> str | None:
    if row.billing_class == "subscription":
        return None
    if row.basis in {"harness_list", "harness_managed"}:
        return row.reported_usd
    rates = revision.prices.get(row.model)
    if (
        rates is None
        or not {"input_tokens", "cached_input_tokens"} <= row.known_token_fields
    ):
        return None
    with localcontext(Context(prec=64, rounding=ROUND_HALF_EVEN)):
        components = (
            charge(row.input_tokens, rates.input),
            charge(row.cached_input_tokens, rates.cached_input),
            cache_charge(row, rates),
            output_charge(row, rates),
        )
        total = Decimal(0)
        for component in components:
            if component is None:
                return None
            total += component
        amount = total / Decimal(1_000_000)
        if amount > MAX_MONEY:
            raise PricingOverflow()
        return quantize_money(amount)
