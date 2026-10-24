from decimal import ROUND_DOWN, Context, Decimal, Inexact, localcontext

import pytest

from kora.ir.cost import PriceRevision, UsageRow
from kora.ir.pricing import PricingOverflow, estimate_usage_usd
from tests.ir.test_cost_models import (
    TOKENS,
    rates_payload,
    revision_payload,
    usage_payload,
)


@pytest.mark.parametrize(
    ("changes", "rates", "expected"),
    [
        (
            dict(
                input_tokens=1000000,
                cached_input_tokens=1000000,
                cache_creation_tokens=1000000,
                cache_creation_by_ttl={"5m": 250000, "1h": 750000},
                output_tokens=1000000,
                reasoning_tokens=250000,
            ),
            dict(
                input="1",
                cached_input="2",
                cache_creation_5m="3",
                cache_creation_1h="4",
                output="5",
                reasoning="6",
            ),
            "12",
        ),
        (
            dict(cache_creation_tokens=1000000),
            dict(cache_creation_5m="3", cache_creation_1h="3"),
            "3",
        ),
        (
            dict(cache_creation_tokens=1000000),
            dict(cache_creation_5m="3", cache_creation_1h="4"),
            None,
        ),
        (
            dict(output_tokens=1000000, known_token_fields=TOKENS[:-1]),
            dict(output="5", reasoning="5"),
            "5",
        ),
        (
            dict(output_tokens=1000000, known_token_fields=TOKENS[:-1]),
            dict(output="5", reasoning="6"),
            None,
        ),
        (dict(known_token_fields=TOKENS[:-1]), dict(output=None, reasoning=None), "0"),
        ({}, dict.fromkeys(rates_payload(), None), "0"),
        (dict(known_token_fields=[]), {}, None),
        (dict(input_tokens=1000000, basis="harness_list", reported_usd="7"), {}, "7"),
        (
            dict(input_tokens=1000000, basis="harness_managed", reported_usd="8"),
            {},
            "8",
        ),
        (dict(input_tokens=1000000, basis="harness_list"), {}, None),
        (dict(input_tokens=1000000, basis="gateway", reported_usd="7"), {}, "1"),
        (
            dict(input_tokens=1000000, billing_class="subscription", reported_usd="7"),
            {},
            None,
        ),
        (dict(input_tokens=1), dict(input="0.00005"), "0"),
        (dict(input_tokens=3), dict(input="0.00005"), "0.0000000002"),
        (
            dict(input_tokens=3, cached_input_tokens=3),
            dict(input="0.00005", cached_input="0.00005"),
            "0.0000000003",
        ),
        (
            dict(input_tokens=1000000),
            dict(input="99999999999999.9999999999"),
            "99999999999999.9999999999",
        ),
        (dict(input_tokens=2147483647), dict(input="0.0000000001"), "0.0000002147"),
        (
            dict(output_tokens=1000000, reasoning_tokens=1000000),
            dict(output=None, reasoning="2"),
            "2",
        ),
        (
            dict(
                cache_creation_tokens=1000000,
                cache_creation_by_ttl={"5m": 1000000, "1h": 0},
            ),
            dict(cache_creation_5m="2", cache_creation_1h=None),
            "2",
        ),
    ],
)
def test_usage_pricing_avoids_double_counting(
    changes: dict[str, object], rates: dict[str, object], expected: str | None
) -> None:
    row = UsageRow.model_validate(usage_payload(**changes))
    rate_values = rates_payload()
    rate_values.update(rates)
    revision = PriceRevision.model_validate(revision_payload({"model": rate_values}))

    amount = estimate_usage_usd(row, revision)

    assert amount == expected


def test_pricing_uses_only_exact_model_and_supplied_revision() -> None:
    row = UsageRow.model_validate(
        usage_payload(input_tokens=1000000, source={"rates": "SECRET", "usd": 100})
    )
    revision = PriceRevision.model_validate(
        revision_payload({"other": rates_payload()})
    )

    amount = estimate_usage_usd(row, revision)

    assert amount is None


def test_pricing_is_independent_of_ambient_decimal_context() -> None:
    row = UsageRow.model_validate(usage_payload(input_tokens=3, cached_input_tokens=3))
    revision = PriceRevision.model_validate(
        revision_payload({"model": rates_payload("0.00005")})
    )
    hostile = Context(prec=1, rounding=ROUND_DOWN, Emin=-1, Emax=1, traps=[Inexact])

    with localcontext(hostile):
        amount = estimate_usage_usd(row, revision)

    assert amount == "0.0000000003"


def test_pricing_refuses_overflow_without_clamping() -> None:
    row = UsageRow.model_validate(usage_payload(input_tokens=1000001))
    revision = PriceRevision.model_validate(
        revision_payload({"model": rates_payload("99999999999999.9999999999")})
    )

    with pytest.raises(PricingOverflow) as refused:
        estimate_usage_usd(row, revision)

    assert str(refused.value) == "accounting amount exceeds its bound"
    assert Decimal(revision.prices["model"].input or "0") > 0
