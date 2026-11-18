from kora.protocols.harness import UsageData
from kora.runner.usage import UsageLedger
from tests.plugins.harness.fake.fake_support import observation


def test_finalisation_issues_a_higher_coverage_revision() -> None:
    ledger = UsageLedger()
    ledger.observe(observation("m1", 1))
    partial = ledger.coverage(False)
    rows = ledger.rows()

    finalised = ledger.coverage(True)

    assert partial.completeness == "partial"
    assert finalised.completeness == "complete"
    assert partial.observation_sequence is not None
    assert finalised.observation_sequence is not None
    assert finalised.observation_sequence > partial.observation_sequence
    assert finalised.observation_id != partial.observation_id
    assert finalised.observed_at == partial.observed_at
    assert ledger.rows() == rows


def test_a_new_gap_issues_a_higher_coverage_revision() -> None:
    ledger = UsageLedger()
    ledger.observe(observation("m1", 1))
    complete = ledger.coverage(True)
    rows = ledger.rows()

    ledger.observe(observation("m1", 1, output_tokens=9))
    gapped = ledger.coverage(True)

    assert complete.completeness == "complete"
    assert gapped.observation_gap
    assert gapped.completeness == "partial"
    assert complete.observation_sequence is not None
    assert gapped.observation_sequence is not None
    assert gapped.observation_sequence > complete.observation_sequence
    assert gapped.observation_id != complete.observation_id
    assert gapped.observed_at == complete.observed_at
    assert ledger.rows() == rows


def test_unchanged_coverage_keeps_its_revision() -> None:
    ledger = UsageLedger()
    ledger.observe(observation("m1", 1))
    partial = ledger.coverage(False)

    repeated_partial = ledger.coverage(False)
    finalised = ledger.coverage(True)
    repeated_finalised = ledger.coverage(True)
    without_result = ledger.coverage(False)
    ledger.observe(observation("m2", 2))
    after_observation = ledger.coverage(False)

    assert repeated_partial is partial
    assert finalised.completeness == "complete"
    assert repeated_finalised is finalised
    assert without_result is finalised
    assert after_observation is finalised
    assert ledger.rows()[0].observation_sequence == 2


def test_relay_restart_reads_the_retained_revision() -> None:
    ledger = UsageLedger()
    ledger.observe(observation("m1", 1))
    first_reader = ledger.coverage
    relayed = first_reader(False)

    restarted_reader = ledger.coverage
    recovered = restarted_reader(False)
    finalised = restarted_reader(True)

    assert recovered is relayed
    assert relayed.observation_sequence is not None
    assert finalised.observation_sequence == relayed.observation_sequence + 1
    assert (
        finalised.observation_id == f"usage-coverage-{finalised.observation_sequence}"
    )
    assert finalised.observed_at == relayed.observed_at


def test_sequence_limit_keeps_an_observation_gap() -> None:
    ledger = UsageLedger(sequence_limit=1)
    ledger.observe(observation("m1", 1))
    partial = ledger.coverage(False)
    later = UsageData.model_validate(
        {
            **observation("m2", 2).model_dump(mode="json"),
            "observed_at": "2026-01-02T00:00:00Z",
        }
    )

    exhausted = ledger.coverage(True)
    ledger.observe(observation("m1", 1, output_tokens=9))
    after_gap = ledger.coverage(True)
    ledger.observe(later)
    after_observation = ledger.coverage(True)

    assert partial.observation_sequence == 0
    assert partial.observation_id == "usage-coverage-0"
    assert partial.completeness == "partial"
    assert not partial.observation_gap
    assert exhausted.observation_sequence == 1
    assert exhausted.observation_id == "usage-coverage-1"
    assert exhausted.observation_gap
    assert exhausted.completeness == "partial"
    assert exhausted.observed_at == partial.observed_at
    assert partial.observed_at is not None
    assert later.observed_at > partial.observed_at
    assert after_gap is exhausted
    assert after_observation is exhausted
