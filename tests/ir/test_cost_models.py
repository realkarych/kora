import json
from abc import ABCMeta
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from traceback import format_exception
from types import MappingProxyType
from typing import cast

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from kora.ir.cost import (
    ModelCost,
    PriceRates,
    PriceRevision,
    RunCost,
    UsageCoverage,
    UsageRow,
    accumulate_attempt_models,
    preflight,
)
from kora.ir.errors import safe_field_errors
from kora.ir.json import CanonicalBudget, JsonShapeError

NOW = datetime(2026, 10, 23, tzinfo=UTC)
TOKENS = [
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "reasoning_tokens",
]


def usage_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = dict.fromkeys(TOKENS, 0)
    value.update(
        model="model",
        known_token_fields=TOKENS.copy(),
        cache_creation_by_ttl=None,
        reported_usd=None,
        observation_id="observation",
        observation_sequence=0,
        observed_at=NOW,
        completeness="complete",
        basis="price_table",
        billing_class="api_key",
        delta_of_session_totals=False,
        source={},
    )
    value.update(changes)
    return value


def rates_payload(value: object = "1") -> dict[str, object]:
    return dict.fromkeys(
        [
            "input",
            "cached_input",
            "cache_creation_5m",
            "cache_creation_1h",
            "output",
            "reasoning",
        ],
        value,
    )


def revision_payload(prices: object) -> dict[str, object]:
    return dict(
        id="revision",
        namespace_id=None,
        effective_from=NOW,
        version=1,
        normalization_version=1,
        prices=prices,
    )


def model_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = dict.fromkeys(TOKENS, 0)
    value.update(
        model="model",
        unknown_token_fields=[],
        completeness="complete",
        usd_estimate="0",
        known_usd_estimate="0",
        pricing_status="estimated",
        billing_classes=["api_key"],
        bases=["price_table"],
    )
    value.update(changes)
    return value


def run_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = dict(
        version=1,
        as_of=NOW,
        observed_at=None,
        completeness="complete",
        usd_estimate="0",
        known_usd_estimate="0",
        pricing_status="estimated",
        missing_report_attempts=0,
        attribution_gap_attempts=0,
        observation_gap_attempts=0,
        models=[],
    )
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "value",
    [
        True,
        1,
        1.0,
        "-1",
        "+1",
        "01",
        "0.0",
        "1.0",
        "1e2",
        ".1",
        "1.",
        "NaN",
        "999999999999999",
        "0.00000000001",
        "9" * 100000,
    ],
)
def test_price_and_approval_bounds_are_explicit(value: object) -> None:
    payload = rates_payload(value)

    with pytest.raises(ValidationError):
        PriceRates.model_validate(payload)
    with pytest.raises(ValidationError):
        PriceRates.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "changes",
    [
        dict(input_tokens=True),
        dict(input_tokens=1.0),
        dict(input_tokens="1"),
        dict(input_tokens=2147483648),
        dict(reasoning_tokens=1),
        dict(known_token_fields=["input_tokens", "input_tokens"]),
        dict(known_token_fields=["unknown"]),
        dict(cache_creation_by_ttl={"5m": 0}),
        dict(cache_creation_by_ttl={"5m": 1, "1h": 0}),
        dict(cache_creation_by_ttl={"5m": True, "1h": 0}),
        dict(observation_sequence=True),
        dict(observed_at=1),
        dict(observed_at=datetime(2026, 10, 23)),
        dict(usd_estimate="0"),
        dict(version=True),
    ],
)
def test_usage_refuses_incoherent_evidence(changes: dict[str, object]) -> None:
    payload = usage_payload(**changes)

    with pytest.raises(ValidationError):
        UsageRow.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        dict(observation_id="x"),
        dict(observation_sequence=0),
        dict(observed_at=NOW),
        dict(attribution_gap=True),
        dict(completeness="complete"),
        dict(version=True),
    ],
)
def test_initial_coverage_cannot_assert_evidence(changes: dict[str, object]) -> None:
    payload: dict[str, object] = dict(
        version=1,
        observation_id=None,
        observation_sequence=None,
        completeness="unavailable",
        observed_at=None,
        attribution_gap=False,
        observation_gap=False,
    )
    payload.update(changes)

    with pytest.raises(ValidationError):
        UsageCoverage.model_validate(payload)


def test_gap_coverage_can_preserve_missing_source_time() -> None:
    payload = dict(
        version=1,
        observation_id="gap",
        observation_sequence=0,
        completeness="partial",
        observed_at=None,
        attribution_gap=True,
        observation_gap=False,
    )

    coverage = UsageCoverage.model_validate(payload)

    assert coverage.observed_at is None


def test_cost_values_own_their_nested_state() -> None:
    raw_rates = rates_payload()
    raw_prices = {"model": raw_rates}
    source: dict[str, object] = {"evidence": [{"native": 1}]}
    revision = PriceRevision.model_validate(revision_payload(raw_prices))
    row = UsageRow.model_validate(
        usage_payload(source=source, cache_creation_by_ttl={"5m": 0, "1h": 0})
    )

    raw_rates["input"] = "2"
    raw_prices.clear()
    source.clear()
    output = revision.model_dump(mode="json")
    cast("dict[str, object]", output["prices"]).clear()
    with pytest.raises(TypeError):
        cast("dict[str, PriceRates]", revision.prices)["other"] = (
            PriceRates.model_validate(rates_payload())
        )
    with pytest.raises(TypeError):
        cast("dict[str, object]", row.source)["other"] = 1
    with pytest.raises(TypeError):
        cast("dict[str, int]", row.cache_creation_by_ttl)["5m"] = 1

    assert revision.prices["model"].input == "1"
    assert row.model_dump(mode="json")["source"] == {"evidence": [{"native": 1}]}


def test_price_snapshot_decimal_round_trip() -> None:
    revision = PriceRevision.model_validate(
        revision_payload({"model": rates_payload("99999999999999.9999999999")})
    )
    snapshot = RunCost.model_validate(
        run_payload(models=[model_payload()], observed_at=NOW)
    )

    restored = PriceRevision.model_validate_json(revision.model_dump_json())
    restored_snapshot = RunCost.model_validate_json(snapshot.model_dump_json())

    assert restored == revision
    assert restored_snapshot == snapshot
    assert (
        json.loads(restored.model_dump_json())["effective_from"]
        == "2026-10-23T00:00:00Z"
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(input_tokens=9007199254740992),
        dict(bases=["price_table", "price_table"]),
        dict(billing_classes=["managed", "api_key"]),
        dict(usd_estimate="1", known_usd_estimate="0"),
        dict(pricing_status="not_billable"),
        dict(unknown_token_fields=["output_tokens", "input_tokens"]),
    ],
)
def test_model_snapshot_requires_consistency(changes: dict[str, object]) -> None:
    payload = model_payload(**changes)

    with pytest.raises(ValidationError):
        ModelCost.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        dict(models=[model_payload(), model_payload()]),
        dict(missing_report_attempts=True),
        dict(missing_report_attempts=1),
        dict(usd_estimate="1"),
        dict(models=[model_payload(model=str(i)) for i in range(1025)]),
    ],
)
def test_run_snapshot_requires_consistency(changes: dict[str, object]) -> None:
    payload = run_payload(**changes)

    with pytest.raises(ValidationError):
        RunCost.model_validate(payload)


def test_attempt_model_union_is_cumulative_and_preserves_input() -> None:
    models = frozenset(str(i) for i in range(64))

    same = accumulate_attempt_models(models, "0")
    with pytest.raises(PydanticCustomError) as saturated:
        accumulate_attempt_models(models, "other")
    with pytest.raises(PydanticCustomError) as overlong:
        accumulate_attempt_models(frozenset({"x" * 257}), "other")

    assert same == models
    assert saturated.value.type == overlong.value.type == "cost_invalid"


class ModelIdentifierSubclass(str):
    pass


class ModelUnionSubclass(frozenset[str]):
    pass


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        False,
        1,
        "",
        "private\nmodel",
        "private-model-" * 24,
        ModelIdentifierSubclass("model"),
    ],
)
@pytest.mark.parametrize("role", ["prior", "incoming"])
def test_attempt_model_union_refuses_invalid_identifiers_without_mutation(
    invalid: object, role: str
) -> None:
    prior = frozenset({invalid}) if role == "prior" else frozenset({"existing"})
    incoming = "added" if role == "prior" else invalid

    with pytest.raises(PydanticCustomError) as refusal:
        accumulate_attempt_models(cast("frozenset[str]", prior), cast("str", incoming))

    assert refusal.value.type == "cost_invalid"
    assert "private" not in str(refusal.value)


@pytest.mark.parametrize(
    "prior",
    [None, False, [], {"existing"}, ("existing",), ModelUnionSubclass({"existing"})],
)
def test_attempt_model_union_requires_exact_frozenset(prior: object) -> None:
    original = repr(prior)

    with pytest.raises(PydanticCustomError) as refusal:
        accumulate_attempt_models(cast("frozenset[str]", prior), "added")

    assert refusal.value.type == "cost_invalid"
    assert repr(prior) == original


@pytest.mark.parametrize("identifier", ["model", "Ж" * 256, "😀" * 256])
@pytest.mark.parametrize("role", ["prior", "incoming"])
def test_attempt_model_union_accepts_printable_character_boundary(
    identifier: str, role: str
) -> None:
    prior = frozenset({identifier}) if role == "prior" else frozenset({"existing"})
    incoming = "added" if role == "prior" else identifier
    expected = (
        frozenset({identifier, "added"})
        if role == "prior"
        else frozenset({"existing", identifier})
    )

    result = accumulate_attempt_models(prior, incoming)

    assert result == expected


@pytest.mark.parametrize("count", [0, 63])
def test_attempt_model_union_adds_within_capacity(count: int) -> None:
    prior = frozenset(str(index) for index in range(count))
    expected = frozenset((*prior, "added"))

    result = accumulate_attempt_models(prior, "added")

    assert result == expected
    assert len(result) == count + 1


@pytest.mark.parametrize("incoming", ["0", "added"])
def test_attempt_model_union_refuses_oversized_prior(incoming: str) -> None:
    prior = frozenset(str(index) for index in range(65))

    with pytest.raises(PydanticCustomError) as refusal:
        accumulate_attempt_models(prior, incoming)

    assert refusal.value.type == "cost_invalid"


@pytest.mark.parametrize(
    "source",
    [
        {"marker": "x" * 8192},
        {"marker": [0] * 8192},
        {"marker": "\u0430" * 4096},
        {"marker": "\\" * 4096},
    ],
)
def test_source_is_bounded_before_copying(source: dict[str, object]) -> None:
    payload = usage_payload(source=source)

    with pytest.raises(ValidationError):
        UsageRow.model_validate(payload)


def test_source_rejects_cycles_and_excess_depth() -> None:
    cycle: dict[str, object] = {}
    cycle["cycle"] = cycle
    deep: dict[str, object] = {}
    for _ in range(17):
        deep = {"nested": deep}

    with pytest.raises(ValidationError):
        UsageRow.model_validate(usage_payload(source=cycle))
    with pytest.raises(ValidationError):
        UsageRow.model_validate(usage_payload(source=deep))


@pytest.mark.parametrize(
    "payload",
    [
        revision_payload({"MODEL_SECRET": rates_payload("RATE_SECRET")}),
        {**revision_payload({}), "FIELD_SECRET": "VALUE_SECRET"},
        revision_payload({"MODEL_SECRET": {"FIELD_SECRET": "VALUE_SECRET"}}),
    ],
)
def test_cost_validation_errors_are_content_free(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as refused:
        PriceRevision.model_validate(payload)

    safe = safe_field_errors(
        refused.value, frozenset(PriceRevision.model_fields), "Invalid cost value."
    )
    rendered = str(refused.value) + repr(safe)

    assert "SECRET" not in rendered
    assert safe


@pytest.mark.parametrize("input_form", ["direct", "mapping", "json"])
@pytest.mark.parametrize(
    ("model_type", "field", "payload"),
    [
        (PriceRevision, "id", revision_payload({})),
        (PriceRevision, "namespace_id", revision_payload({})),
        (UsageRow, "model", usage_payload()),
        (UsageRow, "observation_id", usage_payload()),
    ],
)
def test_identifier_exact_length_remains_valid(
    model_type: type[PriceRevision] | type[UsageRow],
    field: str,
    payload: dict[str, object],
    input_form: str,
) -> None:
    identifier = "x" * 256
    original = model_type.model_validate(payload).model_dump(mode="json")
    original[field] = identifier
    encoded = json.dumps(original)

    if input_form == "json":
        value = model_type.model_validate_json(encoded)
    else:
        value = model_type.model_validate(
            MappingProxyType(original) if input_form == "mapping" else original
        )

    assert value.model_dump(mode="json")[field] == identifier
    assert json.dumps(original) == encoded


@pytest.mark.parametrize("input_form", ["direct", "mapping", "json"])
@pytest.mark.parametrize(
    ("model_type", "field", "payload"),
    [
        (PriceRevision, "id", revision_payload({})),
        (PriceRevision, "namespace_id", revision_payload({})),
        (UsageRow, "model", usage_payload()),
        (UsageRow, "observation_id", usage_payload()),
    ],
)
def test_identifier_overlength_refusal_is_content_free_without_mutation(
    model_type: type[PriceRevision] | type[UsageRow],
    field: str,
    payload: dict[str, object],
    input_form: str,
) -> None:
    original = model_type.model_validate(payload).model_dump(mode="json")
    original[field] = "IDENTIFIER_SECRET".ljust(257, "x")
    encoded = json.dumps(original)

    if input_form == "json":
        with pytest.raises(ValidationError) as refused:
            model_type.model_validate_json(encoded)
    else:
        with pytest.raises(ValidationError) as refused:
            model_type.model_validate(
                MappingProxyType(original) if input_form == "mapping" else original
            )
    safe = safe_field_errors(
        refused.value, frozenset(model_type.model_fields), "Invalid cost value."
    )

    assert safe[0].type == "cost_invalid"
    assert "SECRET" not in "".join(format_exception(refused.value)) + repr(safe)
    assert json.dumps(original) == encoded


@pytest.mark.parametrize("input_form", ["direct", "mapping", "json"])
def test_opaque_source_does_not_apply_typed_identifier_limits(input_form: str) -> None:
    identifiers = dict.fromkeys(
        ("id", "namespace_id", "model", "observation_id"), "x" * 257
    )
    source = {**identifiers, "nested": [{"values": dict(identifiers)}]}
    payload = usage_payload(source=source, observed_at="2026-10-23T00:00:00Z")
    encoded = json.dumps(payload)

    if input_form == "json":
        value = UsageRow.model_validate_json(encoded)
    else:
        value = UsageRow.model_validate(
            MappingProxyType(payload) if input_form == "mapping" else payload
        )

    assert value.model_dump(mode="json")["source"] == source
    assert json.dumps(payload) == encoded


@pytest.mark.parametrize(
    ("name", "value"),
    [("input_tokens", 2147483647), ("observation_sequence", 9007199254740991)],
)
def test_usage_accepts_exact_integer_limits(name: str, value: int) -> None:
    payload = usage_payload(**{name: value})

    row = UsageRow.model_validate(payload)

    assert getattr(row, name) == value


@pytest.mark.parametrize("value", ["0", "0.0000000001", "99999999999999.9999999999"])
def test_money_accepts_exact_domain(value: str) -> None:
    payload = rates_payload(value)

    rates = PriceRates.model_validate(payload)
    restored = PriceRates.model_validate_json(rates.model_dump_json())

    assert restored.input == value


def test_source_exact_canonical_byte_bound() -> None:
    exact = {"x": "x" * 8184}
    excessive = {"x": "x" * 8185}

    row = UsageRow.model_validate(usage_payload(source=exact))
    with pytest.raises(ValidationError):
        UsageRow.model_validate(usage_payload(source=excessive))

    assert (
        len(
            json.dumps(
                row.model_dump(mode="json")["source"], separators=(",", ":")
            ).encode()
        )
        == 8192
    )


def test_revision_exact_canonical_byte_bound() -> None:
    payload = revision_with_encoded_size("2026-10-23T00:00:00Z", 65536)
    prices = cast("dict[str, dict[str, object]]", payload["prices"])
    exact = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    first = next(key for key in prices if len(key) < 256)

    revision = PriceRevision.model_validate_json(exact)
    python_revision = PriceRevision.model_validate(payload)
    prices[first + "x"] = prices.pop(first)
    with pytest.raises(ValidationError):
        PriceRevision.model_validate(payload)
    with pytest.raises(ValidationError):
        PriceRevision.model_validate_json(json.dumps(payload, separators=(",", ":")))

    assert len(exact.encode()) == 65536
    assert len(revision.prices) == 200
    assert python_revision == revision


def test_nested_source_and_snapshot_containers_are_immutable() -> None:
    row = UsageRow.model_validate(usage_payload(source={"native": [{"count": 1}]}))
    models = [model_payload()]
    snapshot = RunCost.model_validate(run_payload(models=models, observed_at=NOW))
    native = cast("tuple[dict[str, object], ...]", row.source["native"])

    models.clear()
    with pytest.raises(TypeError):
        native[0]["count"] = 2
    with pytest.raises(ValidationError):
        snapshot.models[0].input_tokens = 1
    output = snapshot.model_dump(mode="json")
    cast("list[object]", output["models"]).clear()

    assert len(snapshot.models) == 1
    assert row.model_dump(mode="json")["source"] == {"native": [{"count": 1}]}


@pytest.mark.parametrize(
    "payload",
    [
        run_payload(
            models=[
                model_payload(
                    pricing_status="not_billable",
                    usd_estimate=None,
                    known_usd_estimate=None,
                    billing_classes=["subscription"],
                )
            ],
            pricing_status="unpriced",
            observed_at=NOW,
            usd_estimate=None,
            known_usd_estimate=None,
        ),
        run_payload(
            models=[model_payload()],
            pricing_status="partially_priced",
            observed_at=NOW,
            usd_estimate=None,
            known_usd_estimate="0",
        ),
    ],
)
def test_run_status_matches_complete_model_evidence(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunCost.model_validate(payload)


def test_raw_json_refusal_is_content_free_and_bounded() -> None:
    malformed = '{"SECRET": "\\uZZZZ"}'
    oversized = b" " * 65537

    with pytest.raises(ValidationError) as invalid:
        PriceRevision.model_validate_json(malformed)
    with pytest.raises(ValidationError):
        PriceRevision.model_validate_json(oversized)

    assert "SECRET" not in str(invalid.value)


def test_snapshot_money_sum_ignores_ambient_decimal_context() -> None:
    from decimal import ROUND_DOWN, Context, Inexact, localcontext

    models = [
        model_payload(
            model="a",
            usd_estimate="123.4567890123",
            known_usd_estimate="123.4567890123",
        ),
        model_payload(
            model="b", usd_estimate="0.0000000001", known_usd_estimate="0.0000000001"
        ),
    ]
    payload = run_payload(
        models=models,
        observed_at=NOW,
        usd_estimate="123.4567890124",
        known_usd_estimate="123.4567890124",
    )

    with localcontext(Context(prec=1, rounding=ROUND_DOWN, traps=[Inexact])):
        snapshot = RunCost.model_validate(payload)

    assert snapshot.usd_estimate == "123.4567890124"


@pytest.mark.parametrize("value", ["1.0", 1.0, True, "0.00000000001"])
def test_direct_money_parser_refuses_noncanonical_input(value: object) -> None:
    from kora.ir.cost import parse_money

    with pytest.raises(PydanticCustomError) as refusal:
        parse_money(value)

    assert refusal.value.type == "cost_invalid"


def test_money_storage_formatting_is_exact_and_context_independent() -> None:
    from decimal import ROUND_DOWN, Context, Decimal, Inexact, localcontext

    from kora.ir.cost import format_money, parse_money, quantize_money

    stored = Decimal("99999999999999.9999999999")

    with localcontext(Context(prec=1, rounding=ROUND_DOWN, traps=[Inexact])):
        wire = format_money(stored)
        parsed = parse_money(wire)
        rounded = quantize_money(Decimal("0.00000000015"))
        with pytest.raises(PydanticCustomError) as refusal:
            format_money(Decimal("0.00000000001"))

    assert wire == "99999999999999.9999999999"
    assert refusal.value.type == "cost_invalid"
    assert parsed == stored
    assert rounded == "0.0000000002"


def test_absent_priced_observations_cannot_claim_zero_subtotal() -> None:
    payload = run_payload(
        completeness="unavailable",
        missing_report_attempts=1,
        pricing_status="partially_priced",
        usd_estimate=None,
        known_usd_estimate="0",
    )

    with pytest.raises(ValidationError):
        RunCost.model_validate(payload)


@pytest.mark.parametrize("model_count", [1024, 1025])
def test_snapshot_model_cardinality_bound(model_count: int) -> None:
    models = [model_payload(model=f"{i:04d}") for i in range(model_count)]
    payload = run_payload(models=models, observed_at=NOW)

    if model_count == 1025:
        with pytest.raises(ValidationError):
            RunCost.model_validate(payload)
    else:
        snapshot = RunCost.model_validate(payload)

        assert len(snapshot.models) == 1024


def test_shared_usage_remains_a_schema_described_json_boundary() -> None:
    schema = UsageRow.model_json_schema()

    assert schema["properties"]["source"]["type"] == "object"


@pytest.mark.parametrize("value", ["0", "1234567890", "-1", "1.2", "1e3"])
def test_timestamps_refuse_numeric_epoch_strings(value: str) -> None:
    payload = usage_payload(observed_at=value)

    with pytest.raises(ValidationError):
        UsageRow.model_validate(payload)
    with pytest.raises(ValidationError):
        UsageRow.model_validate_json(json.dumps(payload))


def revision_with_encoded_size(
    effective_from: str, byte_count: int
) -> dict[str, object]:
    prices = {f"{i:03d}": rates_payload() for i in range(200)}
    payload = revision_payload(prices)
    payload["effective_from"] = effective_from
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    padding = byte_count - len(encoded)
    for i in range(200):
        growth = min(padding, 253)
        prices[f"{i:03d}" + "x" * growth] = prices.pop(f"{i:03d}")
        padding -= growth
    return payload


def test_revision_refuses_canonical_growth_from_basic_iso() -> None:
    payload = revision_with_encoded_size("20261023T000000Z", 65536)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )

    with pytest.raises(ValidationError):
        PriceRevision.model_validate(payload)
    with pytest.raises(ValidationError):
        PriceRevision.model_validate_json(encoded)

    assert len(encoded.encode()) == 65536


def test_revision_normalization_at_exact_bound_round_trips() -> None:
    payload = revision_with_encoded_size("20261023T000000Z", 65532)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    offset_payload = dict(payload)
    offset_payload["effective_from"] = datetime.fromisoformat(
        "2026-10-23T01:00:00+01:00"
    )

    revision = PriceRevision.model_validate_json(encoded)
    offset_revision = PriceRevision.model_validate(offset_payload)
    restored = PriceRevision.model_validate_json(revision.model_dump_json())

    assert restored == revision == offset_revision
    assert (
        len(
            json.dumps(
                revision.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )
        == 65536
    )


@pytest.mark.parametrize("priced_observations", [False, True])
def test_missing_reports_refuse_partial_snapshot_completeness(
    priced_observations: bool,
) -> None:
    models = (
        [model_payload(usd_estimate="1", known_usd_estimate="1")]
        if priced_observations
        else []
    )
    payload = run_payload(
        models=models,
        completeness="partial",
        missing_report_attempts=1,
        pricing_status="partially_priced" if priced_observations else "unpriced",
        usd_estimate=None,
        known_usd_estimate="1" if priced_observations else None,
    )

    with pytest.raises(ValidationError):
        RunCost.model_validate(payload)


@pytest.mark.parametrize("priced_observations", [False, True])
def test_missing_reports_preserve_unavailable_and_known_subtotals(
    priced_observations: bool,
) -> None:
    models = (
        [model_payload(usd_estimate="1", known_usd_estimate="1")]
        if priced_observations
        else []
    )
    payload = run_payload(
        models=models,
        completeness="unavailable",
        missing_report_attempts=1,
        pricing_status="partially_priced" if priced_observations else "unpriced",
        usd_estimate=None,
        known_usd_estimate="1" if priced_observations else None,
    )

    snapshot = RunCost.model_validate(payload)

    assert snapshot.completeness == "unavailable"
    assert snapshot.known_usd_estimate == ("1" if priced_observations else None)


@pytest.mark.parametrize(
    "gap", ["attribution_gap_attempts", "observation_gap_attempts"]
)
def test_observed_gaps_preserve_partial_snapshot_completeness(gap: str) -> None:
    payload = run_payload(
        models=[model_payload(usd_estimate="1", known_usd_estimate="1")],
        observed_at=NOW,
        completeness="partial",
        pricing_status="partially_priced",
        usd_estimate=None,
        known_usd_estimate="1",
        **{gap: 1},
    )

    snapshot = RunCost.model_validate(payload)

    assert snapshot.completeness == "partial"
    assert snapshot.known_usd_estimate == "1"


@pytest.mark.parametrize(
    "value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
def test_utc_range_overflow_is_a_content_free_validation_refusal(value: str) -> None:
    python_payload = usage_payload(observed_at=datetime.fromisoformat(value))
    json_payload = json.dumps(usage_payload(observed_at=value))

    with pytest.raises(ValidationError) as python_error:
        UsageRow.model_validate(python_payload)
    with pytest.raises(ValidationError) as json_error:
        UsageRow.model_validate_json(json_payload)
    python_safe = safe_field_errors(
        python_error.value, frozenset(UsageRow.model_fields), "Invalid cost value."
    )
    json_safe = safe_field_errors(
        json_error.value, frozenset(UsageRow.model_fields), "Invalid cost value."
    )

    assert python_safe == json_safe
    assert python_safe[0].type == "cost_invalid"
    assert value not in str(python_error.value) + str(json_error.value) + repr(
        python_safe
    )


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"])
def test_utc_year_boundaries_remain_valid(value: str) -> None:
    payload = usage_payload(observed_at=value)

    row = UsageRow.model_validate(payload)
    restored = UsageRow.model_validate_json(row.model_dump_json())

    assert restored == row
    assert restored.model_dump(mode="json")["observed_at"] == value


@pytest.mark.parametrize(
    "source",
    [
        {"SOURCE_KEY_SECRET": "SOURCE_PAYLOAD_SECRET" * 1000},
        {"SOURCE_KEY_SECRET": {"SOURCE_PAYLOAD_SECRET": float("inf")}},
    ],
)
def test_source_marker_validation_errors_are_content_free(
    source: dict[str, object],
) -> None:
    payload = usage_payload(source=source)
    payload["observed_at"] = "2026-10-23T00:00:00Z"

    with pytest.raises(ValidationError) as python_error:
        UsageRow.model_validate(payload)
    with pytest.raises(ValidationError) as json_error:
        UsageRow.model_validate_json(json.dumps(payload))
    python_safe = safe_field_errors(
        python_error.value, frozenset(UsageRow.model_fields), "Invalid cost value."
    )
    json_safe = safe_field_errors(
        json_error.value, frozenset(UsageRow.model_fields), "Invalid cost value."
    )

    assert python_safe == json_safe
    assert "SECRET" not in str(python_error.value) + str(json_error.value) + repr(
        python_safe
    )


@pytest.mark.parametrize("raw_json", [False, True])
def test_complete_nonempty_snapshot_requires_source_time(raw_json: bool) -> None:
    payload = run_payload(models=[model_payload()])
    payload["as_of"] = "2026-10-23T00:00:00Z"

    if raw_json:
        with pytest.raises(ValidationError):
            RunCost.model_validate_json(json.dumps(payload))
    else:
        with pytest.raises(ValidationError):
            RunCost.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"models": [model_payload()], "completeness": "partial"},
        {
            "models": [model_payload()],
            "completeness": "partial",
            "attribution_gap_attempts": 1,
            "pricing_status": "partially_priced",
            "usd_estimate": None,
        },
        {
            "models": [model_payload()],
            "completeness": "partial",
            "observation_gap_attempts": 1,
            "pricing_status": "partially_priced",
            "usd_estimate": None,
        },
        {
            "completeness": "unavailable",
            "missing_report_attempts": 1,
            "pricing_status": "unpriced",
            "usd_estimate": None,
            "known_usd_estimate": None,
        },
    ],
)
def test_snapshot_preserves_legitimate_null_source_times(
    changes: dict[str, object],
) -> None:
    payload = run_payload(**changes)

    snapshot = RunCost.model_validate(payload)
    restored = RunCost.model_validate_json(snapshot.model_dump_json())

    assert snapshot.observed_at is None
    assert restored == snapshot


@pytest.mark.parametrize("completeness", ["complete", "partial", "unavailable"])
def test_real_coverage_retains_nullable_source_time(completeness: str) -> None:
    payload = dict(
        version=1,
        observation_id="source",
        observation_sequence=0,
        completeness=completeness,
        observed_at=None,
        attribution_gap=False,
        observation_gap=False,
    )

    coverage = UsageCoverage.model_validate(payload)
    restored = UsageCoverage.model_validate_json(coverage.model_dump_json())

    assert coverage.observed_at is None
    assert restored == coverage


SCALAR_PREFLIGHT_CASES: tuple[tuple[object, int], ...] = (
    (None, 4),
    (True, 4),
    (False, 5),
    (0, 1),
    (12, 2),
    (-12, 3),
    (1.25, 4),
    (-0.0, 4),
    ("", 2),
    ("text", 6),
    ('"\\\n', 8),
    ("Ж😀", 8),
)


@pytest.mark.parametrize(("value", "encoded_size"), SCALAR_PREFLIGHT_CASES)
@pytest.mark.parametrize("depth", [0, 16])
def test_cost_preflight_scalars_preserve_canonical_bounds(
    value: object, encoded_size: int, depth: int
) -> None:
    exact = CanonicalBudget(encoded_size)
    insufficient = CanonicalBudget(encoded_size - 1)

    preflight(value, exact, depth)
    with pytest.raises(JsonShapeError) as refused:
        preflight(value, insufficient, depth)

    assert exact.remaining == 0
    assert refused.value.rule == "json_size"


@pytest.mark.parametrize(("value", "encoded_size"), SCALAR_PREFLIGHT_CASES)
def test_cost_preflight_rejects_deep_scalars_before_spending(
    value: object, encoded_size: int
) -> None:
    budget = CanonicalBudget(encoded_size)

    with pytest.raises(JsonShapeError) as refused:
        preflight(value, budget, 17)

    assert refused.value.rule == "json_depth"
    assert budget.remaining == encoded_size


@pytest.mark.parametrize(("sign", "encoded_size"), [(1, 4215), (-1, 4216)])
def test_cost_preflight_preserves_integer_bit_boundary(
    sign: int, encoded_size: int
) -> None:
    exact = CanonicalBudget(encoded_size)
    excessive = CanonicalBudget(1_048_576)
    largest_width = sign * (1 << 13999)
    excessive_width = sign * (1 << 14000)

    preflight(largest_width, exact)
    with pytest.raises(JsonShapeError) as refused:
        preflight(excessive_width, excessive)

    assert exact.remaining == 0
    assert refused.value.rule == "json_size"
    assert excessive.remaining == 1_048_576


class EqualityTrap(ABCMeta):
    def __eq__(cls, other: object) -> bool:
        if other is str or other is bool or other is int or other is float:
            raise AssertionError("type equality must not select scalar admission")
        return cls is other

    def __hash__(cls) -> int:
        return type.__hash__(cls)


class EqualitySensitiveString(str, metaclass=EqualityTrap):
    pass


class ScalarMapping(int, Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        if key != "x":
            raise KeyError(key)
        return False

    def __iter__(self) -> Iterator[str]:
        return iter(("x",))

    def __len__(self) -> int:
        return 1


class TimestampMapping(datetime, Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        if key != "x":
            raise KeyError(key)
        return False

    def __iter__(self) -> Iterator[str]:
        return iter(("x",))

    def __len__(self) -> int:
        return 1


class ConversionOnly:
    def __int__(self) -> int:
        return 1

    def __float__(self) -> float:
        return 1.0

    def __str__(self) -> str:
        return "1"


@pytest.mark.parametrize(
    ("value", "encoded_size"),
    [
        (ModelIdentifierSubclass("Ж😀"), 8),
        (EqualitySensitiveString("text"), 6),
        (ScalarMapping(123), 11),
        (TimestampMapping(2026, 10, 31, tzinfo=UTC), 11),
        (NOW, 22),
    ],
)
def test_cost_preflight_preserves_scalar_subclass_and_mapping_precedence(
    value: object, encoded_size: int
) -> None:
    exact = CanonicalBudget(encoded_size)
    insufficient = CanonicalBudget(encoded_size - 1)

    preflight(value, exact)
    with pytest.raises(JsonShapeError) as refused:
        preflight(value, insufficient)

    assert exact.remaining == 0
    assert refused.value.rule == "json_size"


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        pytest.param(float("nan"), "json_float", id="nan"),
        pytest.param(float("inf"), "json_float", id="positive-infinity"),
        pytest.param(float("-inf"), "json_float", id="negative-infinity"),
        pytest.param("\ud800", "json_scalar", id="surrogate"),
        pytest.param(1 << 14000, "json_size", id="positive-integer-overflow"),
        pytest.param(-(1 << 14000), "json_size", id="negative-integer-overflow"),
        pytest.param(ConversionOnly(), "json_scalar", id="conversion-only"),
    ],
)
def test_cost_preflight_scalar_refusals_preserve_safe_public_errors(
    value: object, rule: str
) -> None:
    budget = CanonicalBudget(1_048_576)
    payload = usage_payload(source={"SCALAR_VALUE_SECRET": value})

    with pytest.raises(JsonShapeError) as private_error:
        preflight(value, budget)
    with pytest.raises(ValidationError) as public_error:
        UsageRow.model_validate(payload)

    assert private_error.value.rule == rule
    assert "SCALAR_VALUE_SECRET" not in str(public_error.value)
    assert all(error["type"] == "cost_invalid" for error in public_error.value.errors())
