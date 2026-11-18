import json
from typing import cast

from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import (
    canonical_bytes,
    finite_float,
    refuse_constant,
    unique_object,
)

MAX_BYTES = 4_194_304
MAX_DEPTH = 32


def _integer(text: str) -> int:
    if len(text) > 20:
        raise ValueError("parameter integer exceeds its domain")
    value = int(text)
    if not -(2**63) <= value < 2**63:
        raise ValueError("parameter integer exceeds its domain")
    return value


def _depth(value: object, depth: int) -> None:
    if type(value) in (dict, list):
        if depth > MAX_DEPTH:
            raise ValueError("parameter nesting exceeds its domain")
        children = (
            cast(dict[str, object], value).values()
            if type(value) is dict
            else cast(list[object], value)
        )
        for child in children:
            _depth(child, depth + 1)


def trigger_parameters(arguments: tuple[str, ...]) -> JsonObject:
    document: dict[str, object] = {}
    total = 0
    for argument in arguments:
        total += len(argument.encode("utf-8"))
        if total > MAX_BYTES:
            raise ValueError("parameter arguments exceed their domain")
        key, separator, value = argument.partition("=")
        if not separator or not key or key in document:
            raise ValueError("parameter arguments require unique nonempty keys")
        document[key] = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_int=_integer,
            parse_float=finite_float,
            parse_constant=refuse_constant,
        )
    _depth(document, 0)
    if len(canonical_bytes(cast(JsonObject, document))) > MAX_BYTES:
        raise ValueError("parameters exceed their canonical domain")
    return cast(JsonObject, document)
