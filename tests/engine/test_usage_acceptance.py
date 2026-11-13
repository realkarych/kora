from datetime import timedelta

import pytest

from kora.engine.usage_acceptance import (
    PricedUsage,
    UsageMapping,
    UsageRefusal,
    UsageRejected,
    accept_usage,
)
from kora.ir.cost import PriceRevision, UsageCoverage, UsageRow
from tests.ir.test_cost_models import (
    NOW,
    TOKENS,
    rates_payload,
    revision_payload,
    usage_payload,
)

MAPPING = UsageMapping(
    allowed=frozenset(
        {
            ("price_table", "api_key"),
            ("price_table", "subscription"),
            ("harness_list", "api_key"),
            ("harness_list", "subscription"),
        }
    ),
    corrections=frozenset({("price_table", "harness_list")}),
    delta_of_session_totals=False,
)


def priced(
    row: UsageRow, estimate: str | None, dispatcher: str = "grant"
) -> PricedUsage:
    return PricedUsage(row, estimate, dispatcher, estimate)


def price() -> PriceRevision:
    return PriceRevision.model_validate(revision_payload({"model": rates_payload()}))


def usage(**changes: object) -> UsageRow:
    return UsageRow.model_validate(usage_payload(**changes))


def coverage(sequence: int | None = 0, **changes: object) -> UsageCoverage:
    values: dict[str, object] = {
        "version": 1,
        "observation_id": None if sequence is None else "coverage",
        "observation_sequence": sequence,
        "completeness": "unavailable" if sequence is None else "partial",
        "observed_at": None if sequence is None else NOW,
        "attribution_gap": False,
        "observation_gap": False,
    }
    values.update(changes)
    return UsageCoverage.model_validate(values)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"input_tokens": 1_000_000}, "1"),
        ({"input_tokens": 1_000_000, "model": "missing"}, None),
        ({"input_tokens": 1_000_000, "known_token_fields": TOKENS[1:]}, None),
        (
            {
                "cached_input_tokens": 1_000_000,
                "known_token_fields": [
                    name for name in TOKENS if name != "cached_input_tokens"
                ],
            },
            None,
        ),
        ({"billing_class": "subscription", "reported_usd": "7"}, None),
        ({"basis": "harness_list", "reported_usd": "0.37"}, "0.37"),
    ],
)
def test_usage_usd_is_derived_by_engine(
    changes: dict[str, object], expected: str | None
) -> None:
    report = usage(**changes)
    revision = price()
    reported_coverage = coverage()

    result = accept_usage(
        (), None, (report,), reported_coverage, revision, MAPPING, "grant"
    )

    assert result.accepted_rows[0].usd_estimate == expected
    assert result.changed_rows == result.accepted_rows
    assert result.coverage_changed


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": 9},
        {"cached_input_tokens": 9},
        {"cache_creation_tokens": 9},
        {"output_tokens": 9},
        {"reasoning_tokens": 4},
        {"completeness": "unavailable"},
        {"billing_class": "subscription"},
        {"observation_sequence": 1},
    ],
)
def test_usage_replacement_preserves_observed_usage(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "input_tokens": 10,
        "cached_input_tokens": 10,
        "cache_creation_tokens": 10,
        "output_tokens": 10,
        "reasoning_tokens": 5,
        "completeness": "partial",
        "observation_sequence": 2,
    }
    before = priced(usage(**values), "0.00004", "previous-grant")
    values.update(observation_sequence=3)
    values.update(changes)
    after = usage(**values)
    revision = price()
    reported_coverage = coverage(3)

    with pytest.raises(UsageRejected) as refused:
        accept_usage(
            (before,),
            coverage(2),
            (after,),
            reported_coverage,
            revision,
            MAPPING,
            "grant",
        )

    assert refused.value.reason is UsageRefusal.REGRESSION
    assert before.row.input_tokens == 10
    assert before.row.observation_sequence == 2


def test_usage_final_basis_can_correct_estimate_once() -> None:
    before = priced(usage(input_tokens=1_000_000, completeness="partial"), "1")
    final = usage(
        input_tokens=1_000_000,
        completeness="partial",
        observation_sequence=1,
        basis="harness_list",
        reported_usd="0.5",
    )
    reverse = usage(input_tokens=1_000_000, observation_sequence=2)
    lower = usage(
        input_tokens=1_000_000,
        observation_sequence=2,
        basis="harness_list",
        reported_usd="0.4",
    )
    revision = price()

    accepted = accept_usage(
        (before,), coverage(), (final,), coverage(1), revision, MAPPING, "grant"
    )
    with pytest.raises(UsageRejected) as reversal:
        accept_usage(
            accepted.accepted_rows,
            coverage(1),
            (reverse,),
            coverage(2),
            revision,
            MAPPING,
            "grant",
        )
    with pytest.raises(UsageRejected) as regression:
        accept_usage(
            accepted.accepted_rows,
            coverage(1),
            (lower,),
            coverage(2),
            revision,
            MAPPING,
            "grant",
        )

    assert accepted.accepted_rows[0].usd_estimate == "0.5"
    assert accepted.accepted_rows[0].same_basis_usd_floor == "0.5"
    assert reversal.value.reason is regression.value.reason is UsageRefusal.REGRESSION
    assert before.usd_estimate == "1"


def test_usage_replacement_keeps_coherent_unknown_dimensions() -> None:
    before = priced(
        usage(
            input_tokens=10,
            cache_creation_tokens=5,
            cache_creation_by_ttl={"5m": 5, "1h": 0},
        ),
        "0.000015",
    )
    after = usage(
        input_tokens=20,
        cache_creation_tokens=6,
        known_token_fields=TOKENS[1:],
        observation_sequence=1,
        observed_at=NOW - timedelta(seconds=1),
    )
    revision = price()

    result = accept_usage(
        (before,), coverage(), (after,), coverage(1), revision, MAPPING, "grant"
    )

    assert result.accepted_rows[0].row == after
    assert result.accepted_rows[0].usd_estimate is None
    assert result.accepted_rows[0].row.cache_creation_by_ttl is None
    assert result.accepted_rows[0].row.observation_id == before.row.observation_id


def test_usage_replay_includes_source_without_key_order() -> None:
    before = priced(usage(source={"nul\0": -0.0, "other": [1, True]}), "0")
    replay = usage(source={"other": [1, True], "nul\0": -0.0})
    unrelated = priced(usage(model="retained", observation_sequence=8), None)
    revision = price()
    reported_coverage = coverage(12)

    result = accept_usage(
        (before, unrelated),
        reported_coverage,
        (replay,),
        reported_coverage,
        revision,
        MAPPING,
        "grant",
    )

    assert result.accepted_rows == (before,)
    assert result.changed_rows == ()
    assert not result.coverage_changed
    assert unrelated.row.model == "retained"


@pytest.mark.parametrize(
    "changed_source", [{"value": 1.0}, {"value": True}, {"value": "1"}]
)
def test_usage_replay_preserves_source_numeric_kind(
    changed_source: dict[str, object],
) -> None:
    before = priced(usage(source={"value": 1}), "0")
    after = usage(source=changed_source)
    revision = price()
    reported_coverage = coverage()

    with pytest.raises(UsageRejected) as refused:
        accept_usage(
            (before,),
            reported_coverage,
            (after,),
            reported_coverage,
            revision,
            MAPPING,
            "grant",
        )

    assert refused.value.reason is UsageRefusal.CONFLICT


def test_usage_replay_preserves_source_signed_zero() -> None:
    before = priced(usage(source={"value": -0.0}), "0")
    after = usage(source={"value": 0.0})
    revision = price()
    reported_coverage = coverage()

    with pytest.raises(UsageRejected) as refused:
        accept_usage(
            (before,),
            reported_coverage,
            (after,),
            reported_coverage,
            revision,
            MAPPING,
            "grant",
        )

    assert refused.value.reason is UsageRefusal.CONFLICT


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_id": "different"},
        {"observation_gap": True},
        {"completeness": "complete"},
    ],
)
def test_coverage_only_conflict_preserves_prior_evidence(
    changes: dict[str, object],
) -> None:
    before = coverage(3)
    after = coverage(3, **changes)
    revision = price()

    with pytest.raises(UsageRejected) as refused:
        accept_usage((), before, (), after, revision, MAPPING, "grant")

    assert refused.value.reason is UsageRefusal.CONFLICT
    assert before.observation_id == "coverage"
    assert not before.observation_gap


@pytest.mark.parametrize("sequence", [None, 2])
def test_coverage_only_refuses_old_revision_and_initial_reset(
    sequence: int | None,
) -> None:
    before = coverage(3)
    after = coverage(sequence)
    revision = price()

    with pytest.raises(UsageRejected) as refused:
        accept_usage((), before, (), after, revision, MAPPING, "grant")

    assert refused.value.reason is UsageRefusal.REGRESSION


def test_coverage_advances_independently_of_model_observation() -> None:
    row = priced(usage(observation_sequence=2), "0")
    after = coverage(9, observation_gap=True)
    revision = price()

    result = accept_usage(
        (row,), coverage(8), (row.row,), after, revision, MAPPING, "grant"
    )
    replay = accept_usage((row,), after, (), after, revision, MAPPING, "grant")

    assert result.changed_rows == ()
    assert result.coverage_changed
    assert result.coverage.observation_gap
    assert replay.changed_rows == ()
    assert not replay.coverage_changed


@pytest.mark.parametrize("case", ["batch", "union", "duplicate"])
def test_usage_model_cap_refuses_the_complete_batch(case: str) -> None:
    prior = (
        tuple(priced(usage(model=f"prior-{index}"), None) for index in range(64))
        if case == "union"
        else ()
    )
    rows = (
        tuple(usage(model=f"model-{index}") for index in range(65))
        if case == "batch"
        else (usage(), usage())
        if case == "duplicate"
        else (usage(),)
    )
    revision = price()
    reported_coverage = coverage()

    with pytest.raises(UsageRejected) as refused:
        accept_usage(prior, None, rows, reported_coverage, revision, MAPPING, "grant")

    assert refused.value.reason is UsageRefusal.MODELS
    assert len(prior) == (64 if case == "union" else 0)


def test_usage_admits_all_64_models() -> None:
    rows = tuple(usage(model=f"model-{index}") for index in range(64))
    revision = price()
    reported_coverage = coverage()

    result = accept_usage((), None, rows, reported_coverage, revision, MAPPING, "grant")

    assert len(result.changed_rows) == 64
    assert tuple(item.row for item in result.accepted_rows) == rows
    assert all(item.usd_estimate is None for item in result.accepted_rows)


@pytest.mark.parametrize(
    "changes",
    [
        {"basis": "harness_managed"},
        {"basis": "gateway", "billing_class": "gateway"},
        {"delta_of_session_totals": True},
    ],
)
def test_usage_mapping_cannot_be_selected_by_the_report(
    changes: dict[str, object],
) -> None:
    report = usage(**changes)
    revision = price()
    reported_coverage = coverage()

    with pytest.raises(UsageRejected) as refused:
        accept_usage((), None, (report,), reported_coverage, revision, MAPPING, "grant")

    assert refused.value.reason is UsageRefusal.MAPPING


def test_same_grant_post_result_tail_preserves_partial_evidence() -> None:
    before = priced(usage(basis="harness_list", reported_usd="1"), "1")
    tail = usage(
        basis="harness_list",
        reported_usd="1",
        completeness="partial",
        input_tokens=1,
        observation_sequence=1,
    )
    revision = price()

    result = accept_usage(
        (before,), coverage(), (tail,), coverage(1), revision, MAPPING, "grant"
    )
    with pytest.raises(UsageRejected) as refused:
        accept_usage(
            (before,),
            coverage(),
            (tail,),
            coverage(1),
            revision,
            MAPPING,
            "replacement",
        )

    assert result.accepted_rows[0].row.completeness == "partial"
    assert result.accepted_rows[0].usd_estimate == "1"
    assert refused.value.reason is UsageRefusal.REGRESSION


@pytest.mark.parametrize("subscription", [False, True])
@pytest.mark.parametrize("amount", [None, "0.5"])
def test_usage_cannot_erase_known_harness_amount(
    subscription: bool, amount: str | None
) -> None:
    values: dict[str, object] = {
        "basis": "harness_list",
        "reported_usd": "1",
        "billing_class": "subscription" if subscription else "api_key",
    }
    before = priced(usage(**values), None if subscription else "1")
    values.update(reported_usd=amount, observation_sequence=1)
    after = usage(**values)
    revision = price()

    with pytest.raises(UsageRejected) as refused:
        accept_usage(
            (before,), coverage(), (after,), coverage(1), revision, MAPPING, "grant"
        )

    assert refused.value.reason is UsageRefusal.REGRESSION
    assert before.row.reported_usd == "1"


def test_final_basis_correction_requires_observed_harness_amount() -> None:
    before = priced(usage(input_tokens=1_000_000), "1")
    after = usage(input_tokens=1_000_000, basis="harness_list", observation_sequence=1)
    revision = price()

    with pytest.raises(UsageRejected) as refused:
        accept_usage(
            (before,), coverage(), (after,), coverage(1), revision, MAPPING, "grant"
        )

    assert refused.value.reason is UsageRefusal.REGRESSION


def test_usage_cross_grant_replay_preserves_original_provenance() -> None:
    before = priced(usage(input_tokens=10), "0.00001", "original")
    revision = price()
    report_coverage = coverage()

    result = accept_usage(
        (before,),
        report_coverage,
        (before.row,),
        report_coverage,
        revision,
        MAPPING,
        "recovery",
    )

    assert result.changed_rows == ()
    assert result.accepted_rows == (before,)
    assert result.accepted_rows[0].dispatcher_id == "original"


@pytest.mark.parametrize("final_count", [1_000_000, 3_000_000])
def test_usage_price_floor_survives_unknown_estimate_and_new_grant(
    final_count: int,
) -> None:
    rates = rates_payload("0")
    rates.update(cache_creation_5m="1", cache_creation_1h="2")
    revision = PriceRevision.model_validate(revision_payload({"model": rates}))
    before = priced(
        usage(
            cache_creation_tokens=1_000_000,
            cache_creation_by_ttl={"5m": 0, "1h": 1_000_000},
            completeness="partial",
        ),
        "2",
    )
    unknown = usage(
        cache_creation_tokens=1_000_000, observation_sequence=1, completeness="partial"
    )
    final = usage(
        cache_creation_tokens=final_count,
        cache_creation_by_ttl={"5m": final_count, "1h": 0},
        observation_sequence=2,
        completeness="partial",
    )

    intermediate = accept_usage(
        (before,), coverage(), (unknown,), coverage(1), revision, MAPPING, "grant"
    )
    replay = accept_usage(
        intermediate.accepted_rows,
        coverage(1),
        (unknown,),
        coverage(1),
        revision,
        MAPPING,
        "recovery",
    )
    if final_count == 1_000_000:
        with pytest.raises(UsageRejected) as refused:
            accept_usage(
                intermediate.accepted_rows,
                coverage(1),
                (final,),
                coverage(2),
                revision,
                MAPPING,
                "recovery",
            )
        assert refused.value.reason is UsageRefusal.REGRESSION
    else:
        accepted = accept_usage(
            intermediate.accepted_rows,
            coverage(1),
            (final,),
            coverage(2),
            revision,
            MAPPING,
            "recovery",
        )
        assert accepted.accepted_rows[0].usd_estimate == "3"
        assert accepted.accepted_rows[0].same_basis_usd_floor == "3"

    assert intermediate.accepted_rows[0].usd_estimate is None
    assert intermediate.accepted_rows[0].same_basis_usd_floor == "2"
    assert replay.changed_rows == ()
    assert replay.accepted_rows == intermediate.accepted_rows
