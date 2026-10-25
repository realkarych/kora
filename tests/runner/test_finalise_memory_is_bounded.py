import asyncio
from typing import Final

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.conformance.harness import NoLaunches
from kora.ir.json import JsonObject
from kora.protocols.harness import (
    MAX_SAMPLE_ITEMS,
    DenialSample,
)
from kora.protocols.vcs import WorkspaceTarget
from kora.runner.calls import MAX_DECIDED, ToolCallLedger
from kora.runner.usage import (
    MAX_CLOSED_MESSAGES,
    MAX_MODELS,
    MAX_OPEN_MESSAGES,
    UsageLedger,
)
from tests.plugins.harness.fake.fake_support import (
    AllowingPolicy,
    ScriptedSink,
    harness,
    observation,
    result,
    scripted,
    turn,
)

__all__ = ["kora_capture"]

NO_DENIALS: Final[DenialSample] = DenialSample(items=(), total=0)


def test_finalise_memory_is_bounded_open_messages() -> None:
    ledger = UsageLedger()

    for sequence in range(1, MAX_OPEN_MESSAGES + 101):
        ledger.observe(observation(f"m{sequence}", sequence))
    rows = ledger.rows()
    coverage = ledger.coverage(result_seen=True)

    assert len(ledger.open) == MAX_OPEN_MESSAGES
    assert coverage.observation_gap
    assert coverage.completeness == "partial"
    assert rows[0].input_tokens == 11 * (MAX_OPEN_MESSAGES + 1)


def test_finalise_memory_is_bounded_closed_messages() -> None:
    ledger = UsageLedger()

    for sequence in range(1, MAX_OPEN_MESSAGES + 101):
        ledger.observe(observation(f"m{sequence}", sequence))
        ledger.close(f"m{sequence}")
    ledger.observe(observation(f"m{MAX_OPEN_MESSAGES + 100}", MAX_OPEN_MESSAGES + 200))
    rows = ledger.rows()
    coverage = ledger.coverage(result_seen=True)

    assert not ledger.open
    assert len(ledger.closed_ids) == MAX_CLOSED_MESSAGES
    assert rows[0].input_tokens == 11 * (MAX_OPEN_MESSAGES + 100)
    assert coverage.observation_gap


def test_finalise_memory_is_bounded_evicted_model_keeps_its_row() -> None:
    ledger = UsageLedger()

    ledger.observe(observation("a1", 1, "model-a"))
    for sequence in range(2, MAX_OPEN_MESSAGES + 2):
        ledger.observe(observation(f"b{sequence}", sequence, "model-b"))
    rows = ledger.rows()

    assert [row.model for row in rows] == ["model-a", "model-b"]
    assert rows[0].input_tokens == 11


def test_finalise_memory_is_bounded_models() -> None:
    ledger = UsageLedger()

    for sequence in range(1, MAX_MODELS + 11):
        ledger.observe(observation(f"m{sequence}", sequence, f"model-{sequence}"))
    rows = ledger.rows()

    assert len(rows) == MAX_MODELS
    assert ledger.coverage(result_seen=True).attribution_gap


def test_finalise_memory_is_bounded_uncounted_volume() -> None:
    ledger = UsageLedger()

    for sequence in range(1, 5_001):
        ledger.observe(observation("m1", sequence))
    ledger.observe(observation("m1", 4_000))
    ledger.observe(observation("m1", 5_000))
    rows = ledger.rows()

    assert len(ledger.open) == 1
    assert len(rows) == 1
    assert rows[0].input_tokens == 11
    assert rows[0].observation_sequence == 5_000
    assert ledger.coverage(result_seen=True).completeness == "complete"


def test_finalise_memory_is_bounded_held_results() -> None:
    ledger = ToolCallLedger()

    for index in range(MAX_SAMPLE_ITEMS + 5):
        ledger.result("Bash", f"held-{index}", True)
    bypass = ledger.bypass(NO_DENIALS, result_seen=False)

    assert len(ledger.held) == MAX_SAMPLE_ITEMS
    assert ledger.held_overflow == 5
    assert bypass.confirmed
    assert len(bypass.unconfirmed.items) == MAX_SAMPLE_ITEMS


def test_finalise_memory_is_bounded_decided_calls() -> None:
    ledger = ToolCallLedger()

    for index in range(MAX_DECIDED + 5):
        ledger.decided(f"call-{index}")

    assert len(ledger.decided_ids) == MAX_DECIDED
    assert ledger.decided_overflow == 5


def denial(index: int) -> JsonObject:
    return {"kind": "harness_denial", "tool": "Bash", "tool_use_id": f"call-{index}"}


def test_finalise_memory_is_bounded_denials(kora_capture: Capture) -> None:
    documents = [turn("m1"), *(denial(index) for index in range(1_500)), result()]
    adapter = harness(scripted(documents), context_for(kora_capture))
    spec = adapter.build_session_spec(
        {}, WorkspaceTarget(root="/kora-fake-workspace", host_path=None), None
    )

    outcome = asyncio.run(
        adapter.run(spec, AllowingPolicy(), ScriptedSink(), NoLaunches())
    )

    assert len(outcome.denials.items) == MAX_SAMPLE_ITEMS
    assert outcome.denials.total == 1_500
    assert not outcome.denials_complete
