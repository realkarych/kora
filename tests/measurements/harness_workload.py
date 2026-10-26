import json
from collections.abc import Callable
from typing import Final

from kora.ir.cost import MAX_IDENTIFIER_LENGTH
from kora.ir.json import JsonValue
from kora.protocols.harness import (
    EVENT_ADAPTER,
    MAX_SAMPLE_ITEMS,
    MAX_USAGE_ROWS,
    SessionResult,
)
from tests.protocols.harness_documents import (
    DENIAL,
    EVENT_DATA,
    UNCONFIRMED,
    event,
    session_result,
    usage_row,
)

LARGE_EVENTS: Final[int] = 1000
SMALL_EVENTS: Final[int] = 10_000
ASCII_TEXT: Final[str] = "a" * 61_000
ESCAPED_TEXT: Final[str] = "a\n" * 20_000
SOURCE_TEXT: Final[str] = "s" * 8_180
DENSE_OUTPUT: Final[list[JsonValue]] = ["a"] * 260_000
RESULT_BATCH: Final[int] = 16


def large_events(kind: str, field: str, text: str) -> list[dict[str, object]]:
    return [
        {**event(kind, **{field: text}), "i": index} for index in range(LARGE_EVENTS)
    ]


def small_events() -> list[dict[str, object]]:
    kinds = sorted(EVENT_DATA)
    return [
        {**event(kinds[index % len(kinds)]), "i": index}
        for index in range(SMALL_EVENTS)
    ]


def bounded_identifier(index: int) -> str:
    return f"{index:04d}".ljust(MAX_IDENTIFIER_LENGTH, "x")


def maximal_result() -> dict[str, object]:
    call = {"tool": bounded_identifier(0), "tool_use_id": bounded_identifier(1)}
    return session_result(
        usage=[
            usage_row(
                model=bounded_identifier(index),
                observation_id=bounded_identifier(index),
                observation_sequence=index,
                source={"s": SOURCE_TEXT},
            )
            for index in range(MAX_USAGE_ROWS)
        ],
        denials={
            "items": [{**DENIAL, **call}] * MAX_SAMPLE_ITEMS,
            "total": MAX_SAMPLE_ITEMS,
        },
        unconfirmed_calls={
            "items": [{**UNCONFIRMED, **call}] * MAX_SAMPLE_ITEMS,
            "total": MAX_SAMPLE_ITEMS,
        },
        output={"source": "structured_output", "value": {"k": DENSE_OUTPUT}},
    )


def events_in_python(documents: list[dict[str, object]]) -> Callable[[], object]:
    return lambda: [EVENT_ADAPTER.validate_python(document) for document in documents]


def events_in_json(documents: list[dict[str, object]]) -> Callable[[], object]:
    encoded = [json.dumps(document).encode("utf-8") for document in documents]
    return lambda: [EVENT_ADAPTER.validate_json(document) for document in encoded]


def result_in_python(document: dict[str, object]) -> Callable[[], object]:
    return lambda: SessionResult.model_validate(document)


def result_in_json(document: dict[str, object]) -> Callable[[], object]:
    encoded = json.dumps(document).encode("utf-8")
    return lambda: SessionResult.model_validate_json(encoded)


def traced_batch(workload: str) -> int:
    return RESULT_BATCH if workload.startswith("session_result") else 1


def traced_cycles(workload: str) -> int:
    return 2 if workload.startswith("session_result") else 4


def encoded_bytes(document: dict[str, object]) -> int:
    return len(json.dumps(document).encode("utf-8"))


EVENT_WORKLOADS: Final[dict[str, tuple[str, str, str]]] = {
    "turn_ascii_60kib": ("turn", "text", ASCII_TEXT),
    "turn_escaped_60kib": ("turn", "text", ESCAPED_TEXT),
    "tool_result_ascii_60kib": ("tool_result", "content", ASCII_TEXT),
    "tool_result_escaped_60kib": ("tool_result", "content", ESCAPED_TEXT),
}
MODES: Final[tuple[str, ...]] = ("python", "json")
WORKLOADS: Final[tuple[str, ...]] = tuple(
    f"{prefix}_{mode}"
    for prefix in (
        *(f"events_{name}" for name in EVENT_WORKLOADS),
        "events_mixed_small",
        "session_result_maximal",
    )
    for mode in MODES
)


def event_documents(name: str) -> list[dict[str, object]]:
    if name == "mixed_small":
        return small_events()
    kind, field, text = EVENT_WORKLOADS[name]
    return large_events(kind, field, text)


def build(workload: str) -> tuple[Callable[[], object], int]:
    prefix, _, mode = workload.rpartition("_")
    if prefix == "session_result_maximal":
        result = maximal_result()
        action = result_in_json(result) if mode == "json" else result_in_python(result)
        return action, 1
    documents = event_documents(prefix.removeprefix("events_"))
    action = (
        events_in_json(documents) if mode == "json" else events_in_python(documents)
    )
    return action, len(documents)
