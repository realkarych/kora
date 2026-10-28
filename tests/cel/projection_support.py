from kora.cel import (
    Environment,
    JsonValue,
    ResultKind,
    SchemaTypes,
    SchemaView,
    Variable,
)
from kora.cel.types import DYN, CelType, Record, RecordField
from tests.cel.generator import SeededSource

REPORT: JsonValue = {
    "type": "object",
    "properties": {
        "verdict": {"enum": ["approved", "rejected"]},
        "cpu": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "labels": {"type": "object", "additionalProperties": {"type": "string"}},
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "score": {"type": "integer"},
                },
            },
        },
        "facts": {
            "type": "object",
            "properties": {
                "change": {"type": "string"},
                "size": {"type": "integer"},
            },
        },
        "opt": {"type": "integer"},
    },
    "required": ["verdict", "cpu", "tags", "labels", "notes", "facts"],
}


def report_type() -> CelType:
    return SchemaTypes(lambda uri: None).document(REPORT, SchemaView.STRUCTURED_OUTPUT)


def projection_environment() -> tuple[Environment, dict[str, CelType]]:
    report = report_type()
    inputs = Record(
        {"review": RecordField(report, True), "bench": RecordField(report, True)}
    )
    types: dict[str, CelType] = {"inputs": inputs, "payload": DYN, "response": report}
    environment = Environment(
        {name: Variable(cel_type) for name, cel_type in types.items()},
        ResultKind.BOOL,
        frozenset({"inputs"}),
    )
    return environment, types


WORDS: tuple[str, ...] = ("", "a", "k", "x", "long text")


def maybe(source: SeededSource) -> bool:
    return source.randrange(2) == 0


def word(source: SeededSource) -> str:
    return WORDS[source.randrange(len(WORDS))]


def report_value(source: SeededSource) -> JsonValue:
    labels = {word(source): word(source) for _ in range(source.randrange(4))}
    notes: list[JsonValue] = []
    for _ in range(source.randrange(4)):
        note: dict[str, JsonValue] = {}
        if maybe(source):
            note["text"] = word(source)
        if maybe(source):
            note["score"] = source.randrange(5)
        notes.append(note)
    facts: dict[str, JsonValue] = {}
    if maybe(source):
        facts["change"] = word(source)
    if maybe(source):
        facts["size"] = source.randrange(8)
    cpu: JsonValue = source.randrange(200) if maybe(source) else source.random() * 200
    value: dict[str, JsonValue] = {
        "verdict": "approved" if maybe(source) else "rejected",
        "cpu": cpu,
        "tags": [word(source) for _ in range(source.randrange(4))],
        "labels": dict(labels),
        "notes": notes,
        "facts": facts,
    }
    if maybe(source):
        value["opt"] = source.randrange(4)
    return value


def any_value(source: SeededSource, depth: int = 0) -> JsonValue:
    kind = source.randrange(7 if depth < 2 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return maybe(source)
    if kind == 2:
        return source.randrange(3)
    if kind == 3:
        return word(source)
    if kind == 4:
        return source.random()
    if kind == 5:
        return [any_value(source, depth + 1) for _ in range(source.randrange(3))]
    keys = ("a", "b", "m", "k")
    return {keys[source.randrange(4)]: any_value(source, depth + 1) for _ in range(3)}
