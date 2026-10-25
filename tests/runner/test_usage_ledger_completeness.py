from kora.protocols.harness import UsageData
from kora.runner.usage import UsageLedger
from tests.plugins.harness.fake.fake_support import observation


def with_fields(data: UsageData, **fields: object) -> UsageData:
    return UsageData.model_validate({**data.model_dump(mode="json"), **fields})


def test_usage_ledger_completeness_no_report_is_unavailable() -> None:
    ledger = UsageLedger()

    coverage = ledger.coverage(result_seen=True)

    assert ledger.rows() == ()
    assert coverage.completeness == "unavailable"
    assert coverage.observation_id is None


def test_usage_ledger_completeness_reported_zero_is_complete() -> None:
    ledger = UsageLedger()

    ledger.observe(observation("m1", 1, output_tokens=0))
    rows = ledger.rows()

    assert rows[0].output_tokens == 0
    assert "output_tokens" in rows[0].known_token_fields
    assert ledger.coverage(result_seen=True).completeness == "complete"


def test_usage_ledger_completeness_needs_a_result() -> None:
    ledger = UsageLedger()

    ledger.observe(observation("m1", 1))

    assert ledger.coverage(result_seen=False).completeness == "partial"


def test_usage_ledger_completeness_unknown_model_is_an_attribution_gap() -> None:
    ledger = UsageLedger()

    ledger.observe(observation("m1", 1, model=None))
    ledger.observe(observation("m2", 2))
    coverage = ledger.coverage(result_seen=True)

    assert [row.model for row in ledger.rows()] == ["fake-model"]
    assert coverage.attribution_gap
    assert coverage.completeness == "partial"


def test_usage_ledger_completeness_replay_changes_nothing() -> None:
    ledger = UsageLedger()
    first = observation("m1", 1)

    ledger.observe(first)
    ledger.observe(first)
    ledger.observe(observation("m1", 0))
    coverage = ledger.coverage(result_seen=True)

    assert ledger.rows()[0].input_tokens == 11
    assert not coverage.observation_gap
    assert coverage.completeness == "complete"


def test_usage_ledger_completeness_conflicting_replay_is_a_gap() -> None:
    ledger = UsageLedger()

    ledger.observe(observation("m1", 1))
    ledger.observe(observation("m1", 1, output_tokens=9))

    assert ledger.coverage(result_seen=True).observation_gap


def test_usage_ledger_completeness_keeps_source_gaps() -> None:
    ledger = UsageLedger()
    data = observation("m1", 1, completeness="partial")
    gapped = with_fields(
        data,
        coverage={
            **data.coverage.model_dump(mode="json"),
            "attribution_gap": True,
            "observation_gap": True,
        },
    )

    ledger.observe(gapped)
    coverage = ledger.coverage(result_seen=True)

    assert coverage.attribution_gap
    assert coverage.observation_gap


def test_usage_ledger_completeness_ttl_split_sums_only_when_known() -> None:
    known = UsageLedger()
    missing = UsageLedger()
    split = {"cache_creation_tokens": 5, "cache_creation_by_ttl": {"5m": 2, "1h": 3}}
    first = with_fields(observation("m1", 1), **split)
    second = with_fields(observation("m2", 2), **split)

    known.observe(first)
    known.observe(second)
    missing.observe(first)
    missing.observe(with_fields(observation("m2", 2), cache_creation_tokens=5))
    known_row = known.rows()[0]
    missing_row = missing.rows()[0]

    assert known_row.cache_creation_by_ttl == {"5m": 4, "1h": 6}
    assert missing_row.cache_creation_by_ttl is None
    assert missing_row.cache_creation_tokens == 10


def test_usage_ledger_completeness_keeps_the_latest_observation_time() -> None:
    ledger = UsageLedger()

    ledger.observe(observation("m1", 1))
    ledger.observe(
        with_fields(observation("m2", 2), observed_at="2026-01-02T00:00:00Z")
    )
    observed_at = ledger.coverage(result_seen=True).observed_at

    assert observed_at is not None
    assert observed_at.isoformat() == "2026-01-02T00:00:00+00:00"
