import json
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.runtime import Sweep, SweepOutcome, SweepRefused
from kora.runner.identity import ProcessScanFailure
from tests.measurements.test_local_runtime_cost import last_retirement_observation
from tests.support.local_runners import open_spawning, short_directory
from tests.support.local_runtime import open_descriptors, runtime_context, state_root

__all__ = ["kora_capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(ProcessScanFailure))
async def test_scan_failure_event_is_same_call_and_content_free(
    tmp_path: Path, kora_capture: Capture, reason: ProcessScanFailure
) -> None:
    descriptors = open_descriptors()
    ctx = runtime_context(kora_capture)
    root = state_root(tmp_path)
    private = "private-argv-987654321-errno-native-failure"
    error = SweepRefused(SweepOutcome.LISTING_INCOMPLETE, reason)
    error.add_note(private)
    error.__cause__ = OSError(13, private, str(root))
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        sweep = Sweep(ctx.clock.monotonic_ns())

        try:
            runtime.report(error.outcome, sweep, ctx, error.scan_failure)
            runtime.report(SweepOutcome.LISTING_INCOMPLETE, sweep, ctx)
            runtime.report(SweepOutcome.MATCH_UNREADABLE, sweep, ctx, reason)
            runtime.report(SweepOutcome.TOMBSTONED, sweep, ctx, reason)
        finally:
            await runtime.close()

    assert open_descriptors() == descriptors
    records = kora_capture.finish()
    swept = [r for r in records if r.get("event") == "runtime.local.retire"]
    assert [r.get("scan_failure") for r in swept] == [reason.value, None, None, None]
    assert all("scan_failure" not in record for record in swept[1:])
    assert [r["level"] for r in swept] == ["WARNING"] * 3 + ["INFO"]
    assert str(error) == "listing_incomplete"
    assert all(r["matched"] == r["killed"] == 0 for r in swept)
    assert not any(value in json.dumps(records) for value in (private, str(root)))


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        ("listing_incomplete", "reader_process_changed", "reader_process_changed"),
        ("listing_incomplete", "pid_listing_incomplete", "pid_listing_incomplete"),
        ("listing_incomplete", "scan_deadline", "scan_deadline"),
        ("listing_incomplete", "owner_or_argv_unknown", "owner_or_argv_unknown"),
        *[
            (outcome.value, "pid_listing_incomplete", None)
            for outcome in SweepOutcome
            if outcome is not SweepOutcome.LISTING_INCOMPLETE
        ],
        *[
            ("listing_incomplete", value, None)
            for value in (
                None,
                42,
                True,
                list[object](),
                dict[str, object](),
                "unknown",
                "/private/argv",
                "scan_deadline\n",
            )
        ],
    ],
)
def test_failure_note_retains_only_applicable_closed_reason(
    outcome: str, reason: object, expected: str | None
) -> None:
    record: dict[str, object] = {
        "event": "runtime.local.retire",
        "outcome": outcome,
        "matched": 2,
        "killed": 1,
        "waited_ms": 3.5,
        "exception": "private-error-text",
    }
    if reason is not None:
        record["scan_failure"] = reason

    observed = last_retirement_observation([record])

    assert observed == {
        "outcome": outcome,
        "matched": 2,
        "killed": 1,
        "waited_ms": 3.5,
        "scan_failure": expected,
    }


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("outcome", "private-error"),
        ("matched", True),
        ("matched", -1),
        ("matched", 65537),
        ("killed", True),
        ("killed", -1),
        ("killed", 2),
        ("waited_ms", 0),
        ("waited_ms", -0.1),
        ("waited_ms", float("inf")),
        ("waited_ms", float("nan")),
    ],
)
def test_failure_note_does_not_replace_invalid_last_event_with_earlier_event(
    field: str, invalid: object
) -> None:
    valid: dict[str, object] = {
        "event": "runtime.local.retire",
        "outcome": "listing_incomplete",
        "matched": 1,
        "killed": 0,
        "waited_ms": 3.5,
        "scan_failure": "scan_deadline",
    }
    last = {**valid, field: invalid}

    observed = last_retirement_observation([valid, last, {"event": "other"}])

    assert observed is None


def test_failure_note_without_retirement_is_unverified() -> None:
    observed = last_retirement_observation([{"event": "runtime.local.spawn"}])

    assert observed is None
