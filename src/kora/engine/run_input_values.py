import json

from kora.cel_eval.errors import raise_safe
from kora.engine.start_records import MAX_PARAMS_BYTES, MAX_PARAMS_DEPTH, bounded_object
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import (
    canonical_bytes,
    lexical_depth_valid,
    refuse_constant,
    unique_object,
)

MAX_INTEGER_TOKEN: int = 20
MAX_FLOAT_TOKEN: int = 32


class RunInputValueRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid run input representation")


def _check_limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_PARAMS_BYTES:
        raise RunInputValueRejected()


def _read_int(token: str) -> int:
    if len(token) > MAX_INTEGER_TOKEN:
        raise RunInputValueRejected()
    return int(token)


def _read_float(token: str) -> float:
    if len(token) > MAX_FLOAT_TOKEN:
        raise RunInputValueRejected()
    return float(token)


def encode_run_input_value(value: object, *, max_bytes: int) -> bytes:
    try:
        _check_limit(max_bytes)
        return canonical_bytes(bounded_object(value, max_bytes, MAX_PARAMS_DEPTH))
    except (ValueError, RecursionError):
        del value, max_bytes
    raise_safe(RunInputValueRejected())


def _decode(payload: object, max_bytes: int) -> JsonObject:
    _check_limit(max_bytes)
    if (
        type(payload) is not bytes
        or len(payload) > max_bytes
        or not lexical_depth_valid(payload, MAX_PARAMS_DEPTH + 1)
    ):
        raise RunInputValueRejected()
    value: object = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=refuse_constant,
        parse_int=_read_int,
        parse_float=_read_float,
    )
    admitted = bounded_object(value, max_bytes, MAX_PARAMS_DEPTH)
    if canonical_bytes(admitted) != payload:
        raise RunInputValueRejected()
    return admitted


def decode_run_input_value(payload: object, *, max_bytes: int) -> JsonObject:
    try:
        return _decode(payload, max_bytes)
    except (ValueError, RecursionError):
        del payload, max_bytes
    raise_safe(RunInputValueRejected())
