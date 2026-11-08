import gc
import pickle
from traceback import format_exception, walk_tb

import pytest

from kora.engine.start_parameter_tokens import (
    ManualParameterTokenCursor,
    ParameterToken,
    ParameterTokenFailure,
    ParameterTokenKind,
    ParameterTokenRejected,
)
from kora.engine.start_records import (
    MAX_INTEGER,
    MIN_INTEGER,
    PreparedManualStartRequest,
    prepare_manual_start_request,
)
from kora.ir.json import JsonObject
from tests.engine.test_start_records import GRAPH, NAMESPACE

PRIVATE = "private-manual-token-marker"


def prepared(value: JsonObject) -> PreparedManualStartRequest:
    return prepare_manual_start_request(NAMESPACE, GRAPH, value)


def borrows(cursor: ManualParameterTokenCursor, source: bytes) -> bool:
    return any(value is source for value in gc.get_referents(cursor))


def collect_tokens(cursor: ManualParameterTokenCursor) -> list[ParameterToken]:
    tokens: list[ParameterToken] = []
    while (token := cursor.next_token()) is not None:
        tokens.append(token)
    return tokens


def test_parameter_tokens_preserve_actual_canonical_kinds_and_byte_offsets() -> None:
    ascii_value = "x" * 4096
    request = prepared(
        {
            "text": 'a"b\\c\0🦦',
            "plain": ascii_value,
            "nested": {
                "n": [MIN_INTEGER, MAX_INTEGER, 0, -0.0, 1e-7, 1e20, True, False, None]
            },
        }
    )
    source = request.parameter_bytes()
    cursor = ManualParameterTokenCursor(request, len(source))
    kind = ParameterTokenKind
    expected = [
        (kind.OBJECT_START, b"{"),
        (kind.STRING, b'"nested"'),
        (kind.COLON, b":"),
        (kind.OBJECT_START, b"{"),
        (kind.STRING, b'"n"'),
        (kind.COLON, b":"),
        (kind.ARRAY_START, b"["),
        (kind.INTEGER, b"-9223372036854775808"),
        (kind.COMMA, b","),
        (kind.INTEGER, b"9223372036854775807"),
        (kind.COMMA, b","),
        (kind.INTEGER, b"0"),
        (kind.COMMA, b","),
        (kind.FLOAT, b"-0.0"),
        (kind.COMMA, b","),
        (kind.FLOAT, b"1e-07"),
        (kind.COMMA, b","),
        (kind.FLOAT, b"1e+20"),
        (kind.COMMA, b","),
        (kind.TRUE, b"true"),
        (kind.COMMA, b","),
        (kind.FALSE, b"false"),
        (kind.COMMA, b","),
        (kind.NULL, b"null"),
        (kind.ARRAY_END, b"]"),
        (kind.OBJECT_END, b"}"),
        (kind.COMMA, b","),
        (kind.STRING, b'"plain"'),
        (kind.COLON, b":"),
        (kind.STRING, b'"' + ascii_value.encode() + b'"'),
        (kind.COMMA, b","),
        (kind.STRING, b'"text"'),
        (kind.COLON, b":"),
        (kind.STRING, b'"a\\"b\\\\c\\u0000' + "🦦".encode() + b'"'),
        (kind.OBJECT_END, b"}"),
    ]
    borrowed = borrows(cursor, source)

    tokens = collect_tokens(cursor)
    again = cursor.next_token()
    cursor.close()
    after_close = cursor.next_token()

    assert borrowed
    assert [
        (token.kind, source[token.start : token.end]) for token in tokens
    ] == expected
    assert [token.start for token in tokens] == [
        0,
        *(token.end for token in tokens[:-1]),
    ]
    assert tokens[-1].end == len(source)
    assert again is after_close is None
    assert not borrows(cursor, source)


@pytest.mark.parametrize("extra", [0, 1 << 8192], ids=["exact", "oversized"])
def test_parameter_tokens_reuse_numeric_delimiter_lookahead(extra: int) -> None:
    request = prepared({"n": 1})
    source = request.parameter_bytes()
    cursor = ManualParameterTokenCursor(request, len(source) + extra)
    kind = ParameterTokenKind

    tokens = collect_tokens(cursor)

    assert source == b'{"n":1}'
    assert tokens == [
        ParameterToken(kind.OBJECT_START, 0, 1),
        ParameterToken(kind.STRING, 1, 4),
        ParameterToken(kind.COLON, 4, 5),
        ParameterToken(kind.INTEGER, 5, 6),
        ParameterToken(kind.OBJECT_END, 6, 7),
    ]
    assert not borrows(cursor, source)


def test_parameter_tokens_refuse_before_incomplete_numeric_lookahead() -> None:
    request = prepared({"n": 1})
    source = request.parameter_bytes()
    cursor = ManualParameterTokenCursor(request, len(source) - 1)

    first = [cursor.next_token() for _ in range(3)]
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterTokenRejected) as refused:
            cursor.next_token()
    with pytest.raises(ParameterTokenRejected) as closed:
        cursor.next_token()
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }

    assert first == [
        ParameterToken(ParameterTokenKind.OBJECT_START, 0, 1),
        ParameterToken(ParameterTokenKind.STRING, 1, 4),
        ParameterToken(ParameterTokenKind.COLON, 4, 5),
    ]
    assert refused.value.reason is ParameterTokenFailure.BUDGET
    assert closed.value.reason is ParameterTokenFailure.CLOSED
    assert refused.value.__context__ is refused.value.__cause__ is None
    assert frames["next_token"] == {"reason": ParameterTokenFailure.BUDGET}
    assert "_peek" not in frames
    assert "_scan_number" not in frames
    assert PRIVATE not in "".join(format_exception(refused.value))
    assert not borrows(cursor, source)


def test_parameter_tokens_premature_close_releases_source_and_stays_closed() -> None:
    request = prepared({"n": 1})
    source = request.parameter_bytes()
    cursor = ManualParameterTokenCursor(request, len(source))

    first = [cursor.next_token() for _ in range(4)]
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(cursor)
    still_borrowed = borrows(cursor, source)
    cursor.close()
    with pytest.raises(ParameterTokenRejected) as closed:
        cursor.next_token()

    assert first[-1] == ParameterToken(ParameterTokenKind.INTEGER, 5, 6)
    assert still_borrowed
    assert closed.value.reason is ParameterTokenFailure.CLOSED
    assert not borrows(cursor, source)


class UnissuedRequest(PreparedManualStartRequest):
    pass


@pytest.mark.parametrize("budget", [0, -1, True], ids=["zero", "negative", "boolean"])
def test_parameter_tokens_refuse_invalid_admission_without_source_frames(
    budget: int,
) -> None:
    request = prepared({"private": PRIVATE})
    unissued = object.__new__(UnissuedRequest)

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterTokenRejected) as invalid_budget:
            ManualParameterTokenCursor(request, budget)
        with pytest.raises(ParameterTokenRejected) as invalid_input:
            ManualParameterTokenCursor(unissued, 1)
    failures = [invalid_budget.value, invalid_input.value]

    assert [error.reason for error in failures] == [
        ParameterTokenFailure.BUDGET,
        ParameterTokenFailure.INPUT,
    ]
    for error in failures:
        assert error.__context__ is error.__cause__ is None
        [constructor] = [
            frame
            for frame, _ in walk_tb(error.__traceback__)
            if frame.f_code.co_name == "__new__"
        ]
        assert {"request", "source", "max_inspections", "cursor"}.isdisjoint(
            constructor.f_locals
        )
        assert PRIVATE not in "".join(format_exception(error))
