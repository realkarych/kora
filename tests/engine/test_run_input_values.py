import math
from traceback import format_exception, walk_tb

import pytest

from kora.engine.run_input_values import (
    RunInputValueRejected,
    decode_run_input_value,
    encode_run_input_value,
)
from kora.engine.start_records import (
    MAX_INTEGER,
    MAX_PARAMS_BYTES,
    MAX_PARAMS_DEPTH,
    MIN_INTEGER,
    prepare_manual_start,
    prepare_manual_start_request,
)
from kora.ir.json import JsonObject, JsonValue
from tests.engine.test_start_records import GRAPH, NAMESPACE

CANONICAL = (
    '{"\\u0000key":"\\u0000value","numbers":'
    "[-9223372036854775808,9223372036854775807,1,1.0,true,-0.0,"
    '5e-324,-1.7976931348623157e+308],"unicode":["é","é","😀","{[\\"]}"]}'
).encode()


def test_run_input_codec_matches_manual_request_bytes() -> None:
    native: JsonObject = {
        "unicode": ["é", "e\u0301", "😀", '{["]}'],
        "numbers": [
            MIN_INTEGER,
            MAX_INTEGER,
            1,
            1.0,
            True,
            -0.0,
            5e-324,
            -1.7976931348623157e308,
        ],
        "\0key": "\0value",
    }
    max_bytes = len(CANONICAL)

    prepared = prepare_manual_start_request(NAMESPACE, GRAPH, native)
    encoded = encode_run_input_value(native, max_bytes=max_bytes)
    decoded = decode_run_input_value(CANONICAL, max_bytes=max_bytes)
    reread = decode_run_input_value(CANONICAL, max_bytes=max_bytes)
    numbers = decoded["numbers"]

    assert encoded == prepared.parameter_bytes() == CANONICAL
    assert decoded == native
    assert list(decoded) == ["\0key", "numbers", "unicode"]
    assert decoded["numbers"] is not reread["numbers"]
    assert decoded["unicode"] is not reread["unicode"]
    assert type(numbers) is list
    assert tuple(map(type, numbers)) == (
        int,
        int,
        int,
        float,
        bool,
        float,
        float,
        float,
    )
    signed_zero = numbers[5]
    assert type(signed_zero) is float
    assert math.copysign(1.0, signed_zero) == -1.0
    assert decoded["\0key"] == "\0value"


class ByteSubclass(bytes):
    pass


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"null",
        b"[]",
        b'{"a":1,"a":2}',
        b'{"o":[{"a":1,"\\u0061":2}]}',
        '{"é":0,"\\u00e9":1}'.encode(),
        b'{"a":1 }',
        b'{"z":0,"a":1}',
        b'{"\\u0061":0}',
        b'{"a":-0}',
        b'{"a":1.00}',
        b'{"a":1E+20}',
        b'{"a":1e309}',
        b'{"a":1e-999}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":9223372036854775808}',
        b'{"a":-9223372036854775809}',
        b'{"a":' + b"9" * 256 + b"}",
        b'{"a":0.' + b"1" * 256 + b"}",
        b'{"a":"\\ud800"}',
        b'{"a":"\xed\xa0\x80"}',
        b'{"a":"\xff"}',
        b"\xef\xbb\xbf{}",
        b'{"a":"\x00"}',
        b'{"a":}',
        b"{}{}",
        bytearray(b"{}"),
        memoryview(b"{}"),
        ByteSubclass(b"{}"),
    ],
)
def test_run_input_codec_rejects_noncanonical_or_invalid_bytes(payload: object) -> None:
    with pytest.raises(
        RunInputValueRejected, match="^invalid run input representation$"
    ):
        decode_run_input_value(payload, max_bytes=1024)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {1: True},
        {"a": ()},
        {"a": b"bytes"},
        {"a": "\ud800"},
        {"a": float("nan")},
        {"a": float("inf")},
        {"a": MAX_INTEGER + 1},
        {"a": MIN_INTEGER - 1},
    ],
)
def test_run_input_codec_rejects_non_native_or_out_of_domain_values(
    value: object,
) -> None:
    with pytest.raises(
        RunInputValueRejected, match="^invalid run input representation$"
    ):
        encode_run_input_value(value, max_bytes=1024)


class LimitSubclass(int):
    pass


@pytest.mark.parametrize(
    "max_bytes", [0, -1, True, MAX_PARAMS_BYTES + 1, LimitSubclass(8)]
)
def test_run_input_codec_refuses_invalid_allowances(max_bytes: int) -> None:
    payload = b"{}"
    value: JsonObject = {}

    with pytest.raises(RunInputValueRejected):
        decode_run_input_value(payload, max_bytes=max_bytes)
    with pytest.raises(RunInputValueRejected):
        encode_run_input_value(value, max_bytes=max_bytes)


def test_run_input_codec_enforces_exact_limits() -> None:
    level: JsonValue = 0
    for _ in range(MAX_PARAMS_DEPTH):
        level = [level]
    value: JsonObject = {"x": level}
    too_deep: JsonObject = {"x": [level]}
    opening = b"[" * MAX_PARAMS_DEPTH
    closing = b"]" * MAX_PARAMS_DEPTH
    payload = b'{"x":' + opening + b"0" + closing + b"}"
    over_depth = b'{"x":[' + opening + b"0" + closing + b"]}"
    exact = len(payload)

    decoded = decode_run_input_value(payload, max_bytes=exact)
    encoded = encode_run_input_value(value, max_bytes=exact)
    at_ceiling = decode_run_input_value(payload, max_bytes=MAX_PARAMS_BYTES)
    encoded_at_ceiling = encode_run_input_value(value, max_bytes=MAX_PARAMS_BYTES)
    with pytest.raises(RunInputValueRejected):
        decode_run_input_value(payload, max_bytes=exact - 1)
    with pytest.raises(RunInputValueRejected):
        encode_run_input_value(value, max_bytes=exact - 1)
    with pytest.raises(RunInputValueRejected):
        decode_run_input_value(over_depth, max_bytes=len(over_depth))
    with pytest.raises(RunInputValueRejected):
        encode_run_input_value(too_deep, max_bytes=len(over_depth))

    assert decoded == at_ceiling == value
    assert encoded == encoded_at_ceiling == payload


def test_run_input_codec_preserves_the_prepared_snapshot() -> None:
    nested: JsonObject = {"list": [1, 2]}
    request = prepare_manual_start_request(NAMESPACE, GRAPH, {"shared": nested})
    expected_identity = prepare_manual_start(
        NAMESPACE, GRAPH, {"shared": {"list": [1, 2]}}
    )
    expected_bytes = b'{"shared":{"list":[1,2]}}'

    raw = request.parameter_bytes()
    direct = request.parameters()
    direct["shared"] = "changed"
    nested["list"] = "changed"
    decoded = decode_run_input_value(raw, max_bytes=len(raw))
    decoded["shared"] = "changed"
    retained = request.parameter_bytes()
    recovered = decode_run_input_value(retained, max_bytes=len(retained))

    assert raw is retained
    assert retained == expected_bytes
    assert request.identity == expected_identity
    assert recovered == {"shared": {"list": [1, 2]}}
    assert request.parameters() == recovered


def test_run_input_codec_refusal_drops_unsafe_arguments_and_parser_frames() -> None:
    marker = "private-run-input-canary"
    malformed = f'{{"{marker}":}}'.encode()
    invalid = {marker: ("native representation is required",)}
    wrapper_codes = {decode_run_input_value.__code__, encode_run_input_value.__code__}

    try:
        raise ValueError(marker)
    except ValueError:
        with pytest.raises(RunInputValueRejected) as bad_bytes:
            decode_run_input_value(malformed, max_bytes=1024)
        with pytest.raises(RunInputValueRejected) as bad_value:
            encode_run_input_value(invalid, max_bytes=1024)

    for error in (bad_bytes.value, bad_value.value):
        assert error.__cause__ is error.__context__ is None
        assert error.args == ("invalid run input representation",)
        assert marker not in "".join(format_exception(error))
        frames = [frame for frame, _ in walk_tb(error.__traceback__)]
        assert "_decode" not in {frame.f_code.co_name for frame in frames}
        assert [
            frame.f_locals for frame in frames if frame.f_code in wrapper_codes
        ] == [{}]
