import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from kora.ir.errors import PLACEHOLDER_LOCATION, SafeFieldError, safe_field_errors
from kora.ir.json import DEFAULT_MAX_DEPTH, MAX_CANONICAL_BYTES, JsonObjectField
from tests.ir.support import nested

MARKER: str = "json-field-content-marker"
SAFE_MESSAGE: str = "the request is invalid"


class ClosedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class Document(ClosedModel):
    body: JsonObjectField


class Batch(ClosedModel):
    documents: list[Document]


REFUSED: tuple[object, ...] = (
    [MARKER],
    {1: MARKER},
    {"value": float("nan")},
    {"value": {MARKER}},
    nested(DEFAULT_MAX_DEPTH + 1, MARKER),
    {"value": MARKER * (MAX_CANONICAL_BYTES // len(MARKER) + 1)},
    {"value": MARKER + "\ud800"},
    {"value": 10**5000},
)


@pytest.mark.parametrize("body", REFUSED)
def test_json_object_field_refuses_without_content(body: object) -> None:
    candidate = {"body": body}

    with pytest.raises(ValidationError) as raised:
        Document.model_validate(candidate)

    errors = raised.value.errors(include_input=False, include_context=False)
    assert [(item["type"], item["loc"]) for item in errors] == [
        ("json_object_invalid", ("body",))
    ]
    assert MARKER not in str(raised.value)


def test_json_object_field_accepts_the_deepest_object_and_declares_its_schema() -> None:
    body = nested(DEFAULT_MAX_DEPTH, MARKER)

    document = Document.model_validate({"body": body})

    assert document.body == body
    assert Document.model_json_schema()["properties"]["body"] == {
        "title": "Body",
        "type": "object",
    }


def test_safe_field_errors_hide_unknown_keys_and_input() -> None:
    candidate: dict[str, object] = {
        "documents": [{"body": {}}, {"body": [MARKER], MARKER: 1}]
    }
    with pytest.raises(ValidationError) as raised:
        Batch.model_validate(candidate)

    safe = safe_field_errors(
        raised.value, frozenset({"documents", "body"}), SAFE_MESSAGE
    )

    assert sorted(safe, key=lambda item: item.type) == [
        SafeFieldError(
            ("documents", 1, PLACEHOLDER_LOCATION), "extra_forbidden", SAFE_MESSAGE
        ),
        SafeFieldError(("documents", 1, "body"), "json_object_invalid", SAFE_MESSAGE),
    ]
    assert MARKER not in json.dumps([item.loc for item in safe])
