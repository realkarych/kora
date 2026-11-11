import json
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import (
    MAX_CAPTURE_BYTES,
    Capture,
    CaptureOverflow,
    captured,
)
from kora.conformance.common import CONFORMANCE_COMPONENT, INSTANCE_ID, utc_now
from kora.observability import LoggingSettings
from kora.plugins.runtime.local.runtime import Sweep, SweepOutcome, SweepRefused
from kora.protocols.errors import KoraProtocolError
from kora.runner.identity import NativeIdentityReader, ProcessScanFailure
from kora.runner.registry import Entry, Starting
from tests.measurements.test_local_runtime_cost import last_retirement_observation
from tests.support.local_runners import (
    exited_within,
    lingering_start,
    open_spawning,
    reap_runners,
    runner_pids,
    short_directory,
)
from tests.support.local_runtime import (
    SteeredIdentityReader,
    open_descriptors,
    protocol_error,
    recorded,
    runtime_context,
    state_root,
    unreadable,
)
from tests.support.retirement_evidence import (
    recovery_retirement_observation,
    retirement_failure_note,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_bytes", [MAX_CAPTURE_BYTES, 1])
@pytest.mark.parametrize("after_incomplete_listing", [False, True])
async def test_ordinary_retirement_note_preserves_refusal_and_live_carrier(
    tmp_path: Path, capture_bytes: int, after_incomplete_listing: bool
) -> None:
    descriptors = open_descriptors()
    root = state_root(tmp_path)
    original: list[KoraProtocolError] = []
    with (
        captured(
            CONFORMANCE_COMPONENT,
            LoggingSettings(),
            INSTANCE_ID,
            utc_now,
            max_bytes=capture_bytes,
        ) as capture,
        short_directory() as base,
    ):
        ctx = runtime_context(capture)
        runtime, entry, _, spawned = await lingering_start(
            root,
            base / "sockets",
            ctx,
            scanner=NativeIdentityReader(clock=ctx.clock, max_scan_entries=1),
        )
        states: list[Entry | None] = []

        async def retire_and_close() -> None:
            with retirement_failure_note(
                capture, after_incomplete_listing=after_incomplete_listing
            ):
                try:
                    await runtime.retire("attempt-1", timedelta(seconds=10), ctx)
                except KoraProtocolError as error:
                    original.append(error)
                    states.append(await recorded(runtime, "attempt-1"))
                    raise
                finally:
                    await runtime.close()

        try:
            if after_incomplete_listing:
                initial = await protocol_error(
                    runtime.retire("attempt-1", timedelta(seconds=10), ctx)
                )
                assert initial.code == "runner_stop_unconfirmed"
                await runtime.close()
                reader = SteeredIdentityReader()
                reader.steered[spawned] = unreadable
                runtime = await open_spawning(
                    root, ctx, base / "sockets", identities=reader
                )

            with pytest.raises(KoraProtocolError) as raised:
                await retire_and_close()

            assert raised.value is original[0]
            assert (raised.value.code, raised.value.details) == (
                "runner_stop_unconfirmed",
                {},
            )
            fenced = states[0]
            assert isinstance(fenced, Starting)
            assert fenced.fence is not None
            assert not exited_within(spawned, 0)
            assert runtime.executor.jobs == runtime.sweeps.jobs == set()
            assert capture.finished
            assert len(raised.value.__notes__) == 1
            note = raised.value.__notes__[0]
            assert all(
                value not in note for value in (str(root), entry.nonce, str(base))
            )
            if capture_bytes == 1:
                assert note == "last observed runtime.local.retire: null (unverified)"
                assert isinstance(raised.value.__cause__, CaptureOverflow)
            else:
                expected = last_retirement_observation(capture.finish())
                assert expected is not None
                if after_incomplete_listing:
                    assert expected["outcome"] == "match_unreadable"
                    assert expected["scan_failure"] is None
                    assert expected["matched"] == 1
                    assert expected["killed"] == 0
                    assert recovery_retirement_observation(capture.finish()) == expected
                else:
                    assert expected["outcome"] == "listing_incomplete"
                    assert expected["scan_failure"] == "pid_listing_incomplete"
                    assert expected["matched"] == expected["killed"] == 0
                assert note == json.dumps(
                    {"last_observed_runtime.local.retire": expected}, sort_keys=True
                )
        finally:
            await runtime.close()
            reap_runners(root)
    assert runner_pids(root) == ()
    assert open_descriptors() == descriptors


def recovery_events() -> list[dict[str, object]]:
    return [
        {
            "event": "runtime.local.retire",
            "outcome": "listing_incomplete",
            "scan_failure": "pid_listing_incomplete",
            "matched": 0,
            "killed": 0,
            "waited_ms": 1.0,
            "argv": "private-first-event",
        },
        {
            "event": "runtime.local.retire",
            "outcome": "match_unreadable",
            "matched": 1,
            "killed": 0,
            "waited_ms": 2.0,
            "path": "private-second-event",
        },
    ]


def test_recovery_note_selects_second_event_without_private_content() -> None:
    first, second = recovery_events()
    records: list[dict[str, object]] = [
        first,
        {"event": "unrelated"},
        second,
        {"event": "other"},
    ]

    observed = recovery_retirement_observation(records)

    assert observed == {
        "outcome": "match_unreadable",
        "matched": 1,
        "killed": 0,
        "waited_ms": 2.0,
        "scan_failure": None,
    }
    assert "private" not in json.dumps(observed)


@pytest.mark.parametrize("indices", [(), (0,), (1,), (0, 1, 1), (1, 0)])
def test_recovery_note_rejects_incomplete_or_extra_sequence(
    indices: tuple[int, ...],
) -> None:
    events = recovery_events()
    records = [events[index] for index in indices]

    observed = recovery_retirement_observation(records)

    assert observed is None


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (0, "outcome", "match_unreadable"),
        (0, "scan_failure", "scan_deadline"),
        (0, "scan_failure", None),
        (0, "matched", 1),
        (0, "killed", 1),
        *[
            (index, field, value)
            for index in (0, 1)
            for field, value in (
                ("outcome", "private-invalid"),
                ("matched", True),
                ("killed", -1),
                ("waited_ms", float("inf")),
            )
        ],
    ],
)
def test_recovery_note_rejects_invalid_sequence_without_fallback(
    index: int, field: str, value: object
) -> None:
    records = recovery_events()
    records[index][field] = value

    observed = recovery_retirement_observation(records)

    assert observed is None
