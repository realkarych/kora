import json
import math
from collections import deque

from kora.conformance.capture import (
    Capture,
    CaptureLeftover,
    CaptureOverflow,
    CaptureTimeout,
)
from kora.ir.json import JsonObject, JsonValue
from kora.plugins.runtime.local.command_stop import (
    STOP_DETAIL_FIELDS,
    STOP_STATUS_FIELDS,
)
from kora.plugins.runtime.local.workload_session import Completion

DIAGNOSTIC_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "runtime.local.workspace": (
        ("prepared", "failed", "journal_unavailable", "cancelled"),
        ("waited_ms",),
    ),
    "runtime.local.command_stop": (("confirmed", "unresolved"), ("waited_ms",)),
    "runtime.local.capture": (
        ("complete", "truncated", "write_failed", "unsealed"),
        ("retained_bytes", "observed_bytes", "waited_ms"),
    ),
    "runtime.local.command": (
        ("started", "exited", "observed", "refused", "journal_unavailable"),
        ("waited_ms",),
    ),
}


def diagnostic_number(value: object) -> int | float | str:
    if (
        (type(value) is int or type(value) is float)
        and 0 <= value <= 2**63 - 1
        and math.isfinite(value)
    ):
        return value
    return "invalid"


def event_diagnostics(capture: Capture) -> JsonObject:
    selected: deque[JsonValue] = deque(maxlen=16)
    omitted = 0
    for record in capture.finish():
        name = record.get("event")
        if type(name) is not str or name not in DIAGNOSTIC_FIELDS:
            omitted += 1
            continue
        outcomes, fields = DIAGNOSTIC_FIELDS[name]
        outcome = record.get("outcome")
        projected: JsonObject = {
            "event": name,
            "outcome": outcome
            if type(outcome) is str and outcome in outcomes
            else "invalid",
        }
        for field in fields:
            projected[field] = diagnostic_number(record.get(field))
        if name == "runtime.local.command_stop":
            for field in (*STOP_DETAIL_FIELDS, *STOP_STATUS_FIELDS):
                if field in record:
                    value = record[field]
                    projected[field] = value if type(value) is bool else "invalid"
        if len(selected) == 16:
            omitted += 1
        selected.append(projected)
    return {
        "post_teardown_events": list(selected),
        "omitted_records": omitted,
    }


def observation_diagnostics(
    capture: Capture, completion: Completion, journal_bytes: int
) -> JsonObject:
    return {
        "pre_teardown_completion": completion.value,
        "pre_teardown_journal_bytes": diagnostic_number(journal_bytes),
        **event_diagnostics(capture),
    }


def attach_event_diagnostics(error: Exception, capture: Capture) -> None:
    try:
        evidence = event_diagnostics(capture)
    except (CaptureLeftover, CaptureOverflow, CaptureTimeout) as collection_error:
        error.add_note("post_teardown_event_collection=failed")
        raise ExceptionGroup(
            "diagnostic collection failed after the original assertion",
            [error, collection_error],
        ) from None
    error.add_note(json.dumps(evidence, sort_keys=True))
