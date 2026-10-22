import json
from collections.abc import Callable

import pytest

from kora.ir.json import (
    MAX_CANONICAL_BYTES,
    JsonObject,
    JsonShapeError,
    parse_json_object,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.metadata import ProviderMetadataError, parse_config_schema

KEY_OVERHEAD: int = len('{"k":""}')


class ShortList(list[int]):
    def __len__(self) -> int:
        return 0


def sized(total: int) -> JsonObject:
    return {"k": "a" * (total - KEY_OVERHEAD)}


def refused_rule(action: Callable[[], object]) -> str | None:
    try:
        action()
    except (JsonShapeError, ProviderMetadataError) as error:
        shown = error.__context__ is not None and not error.__suppress_context__
        chained = error.__cause__ is not None or shown
        return "chained" if chained else error.rule
    return None


def protocol_error(details: JsonObject) -> Callable[[], object]:
    return lambda: KoraProtocolError("failed", "failed", False, details)


def config_schema(extra: dict[str, object]) -> Callable[[], object]:
    return lambda: parse_config_schema({"type": "object", **extra})


CANONICAL_FORMS: list[JsonObject] = [
    {},
    {"k": []},
    {"k": {}},
    {"a": 1, "b": [True, False, None]},
    {"f": [0.1, -2.5e-10, 1e300, 3.0]},
    {"n": [-7, 10**400]},
    {"e": 'quote " backslash \\ tab \t newline \n bell \x07 del \x7f'},
    {"u": "é ü 中文 😀 \u2028"},
    {"é": {"nested": ["x", {"y": "z"}]}},
]


def canonical_size(document: JsonObject) -> int:
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return len(encoded.encode("utf-8"))


@pytest.mark.parametrize("document", CANONICAL_FORMS)
def test_the_size_rule_counts_canonical_bytes_exactly(document: JsonObject) -> None:
    size = canonical_size(document)

    accepted = parse_json_object(document, max_bytes=size)
    refused = refused_rule(lambda: parse_json_object(document, max_bytes=size - 1))

    assert accepted == document
    assert refused == "json_size"


def test_an_object_of_exactly_the_limit_is_accepted() -> None:
    document = sized(MAX_CANONICAL_BYTES)

    parsed = parse_json_object(document)

    assert parsed == document


OVER_LIMIT: dict[str, tuple[Callable[[], object], str]] = {
    "one_byte_over": (
        lambda: parse_json_object(sized(MAX_CANONICAL_BYTES + 1)),
        "json_size",
    ),
    "escaped_string_over": (
        lambda: parse_json_object({"k": '"' * (MAX_CANONICAL_BYTES // 2)}),
        "json_size",
    ),
    "string_over_before_encoding": (
        lambda: parse_json_object({"k": "a" * MAX_CANONICAL_BYTES + "\ud800"}),
        "json_size",
    ),
    "integer_past_the_digit_limit": (
        lambda: parse_json_object({"k": 10**5000}),
        "json_size",
    ),
    "protocol_error_details": (
        protocol_error(sized(MAX_CANONICAL_BYTES + 1)),
        "json_size",
    ),
    "provider_config_schema": (
        config_schema({"x": "a" * MAX_CANONICAL_BYTES}),
        "config_schema.json_size",
    ),
    "many_small_items": (lambda: parse_json_object({"k": [0] * 600_000}), "json_size"),
    "many_keys": (
        lambda: parse_json_object({str(index): 0 for index in range(200_000)}),
        "json_size",
    ),
    "list_subclass_length": (
        lambda: parse_json_object({"k": ShortList([1, 2, 3])}, max_bytes=11),
        "json_size",
    ),
    "string_not_text": (lambda: parse_json_object({"k": "\ud800"}), "json_scalar"),
    "key_not_text": (lambda: parse_json_object({"\ud800": 1}), "json_key"),
}


@pytest.mark.parametrize("case", sorted(OVER_LIMIT))
def test_json_size_rule(case: str) -> None:
    action, expected = OVER_LIMIT[case]

    rule = refused_rule(action)

    assert rule == expected
