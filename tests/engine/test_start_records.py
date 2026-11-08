import hashlib
import json
import math
import pickle
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from traceback import format_exception, walk_tb

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from kora.engine.start_records import (
    MAX_INTEGER,
    MAX_PARAMS_BYTES,
    MIN_INTEGER,
    AcceptedStart,
    ManualStartIdentity,
    PreparedManualStartRequest,
    SkippedStart,
    StartOutcome,
    StartReceipt,
    StartRecordRejected,
    decode_start_receipt,
    prepare_manual_start,
    prepare_manual_start_request,
    start_receipt_document,
)
from kora.ir.json import JsonObject, JsonValue

NAMESPACE: str = "00000000000000000000000001"
GRAPH: str = "00000000000000000000000002"
VERSION: str = "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
RUN: str = "00000000000000000000000003"


def prepared(params: object) -> ManualStartIdentity:
    return prepare_manual_start(NAMESPACE, GRAPH, params)


def receipt(response: StartOutcome) -> StartReceipt:
    identity = prepared({})
    return StartReceipt(
        version=1,
        kind="manual",
        namespace_id=NAMESPACE,
        graph_id=GRAPH,
        request_digest=identity.request_digest,
        response=response,
    )


def accepted() -> AcceptedStart:
    return AcceptedStart(
        outcome="accepted", run_id=RUN, graph_version_id=VERSION, status="pending"
    )


def test_manual_start_identity_has_fixed_canonical_vector() -> None:
    params: JsonObject = {"z": [1, 1.0, True, -0.0], "é": "value"}
    envelope = {
        "format": "manual-start/1",
        "namespace_id": NAMESPACE,
        "graph_id": GRAPH,
        "graph_version": {"kind": "latest"},
        "params": params,
        "allow_unenforced_secrets": False,
    }
    expected = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    result = prepared(params)

    assert result.request_digest == hashlib.sha256(expected.encode()).hexdigest()
    assert (
        result.request_digest
        == "deddf370233f28b6e28092f9cb4a402b6a8be0c68eb76dccf20c01930c82dc20"
    )
    assert result.namespace_id == NAMESPACE
    assert result.graph_id == GRAPH


def test_manual_start_identity_preserves_original_selector_and_scope() -> None:
    params = {"first": 1, "second": 2}
    original = prepared(params)

    explicit_false = prepare_manual_start(
        NAMESPACE, GRAPH, {"second": 2, "first": 1}, allow_unenforced_secrets=False
    )
    changed = (
        prepare_manual_start(NAMESPACE, GRAPH, params, graph_version_id=VERSION),
        prepare_manual_start(NAMESPACE, GRAPH, params, allow_unenforced_secrets=True),
        prepare_manual_start(GRAPH, NAMESPACE, params),
        prepare_manual_start(NAMESPACE, RUN, params),
        prepared({"first": 2, "second": 1}),
    )

    assert explicit_false == original
    assert len({item.request_digest for item in (*changed, original)}) == 6


@pytest.mark.parametrize(
    ("first", "second"),
    [(1, 1.0), (1, True), (-0.0, 0.0), ([1, 2], [2, 1]), ("é", "e\u0301")],
)
def test_manual_start_identity_preserves_json_kinds(
    first: JsonValue, second: JsonValue
) -> None:
    original = prepared({"value": first})

    changed = prepared({"value": second})

    assert changed.request_digest != original.request_digest


def test_manual_start_identity_retains_no_mutable_parameters() -> None:
    child: JsonObject = {"value": [1, 2]}
    params: JsonObject = {"left": child, "right": child}
    result = prepared(params)
    original = prepared({"left": {"value": [1, 2]}, "right": {"value": [1, 2]}})

    child["value"] = "changed"
    changed = prepared(params)

    assert result == original
    assert changed.request_digest != result.request_digest
    assert set(ManualStartIdentity.model_fields) == {
        "namespace_id",
        "graph_id",
        "request_digest",
    }
    with pytest.raises(ValidationError):
        result.graph_id = RUN


@pytest.mark.parametrize("character", ["a", "é", "🙂", "\u0000", "\\", '"'])
def test_manual_start_canonical_support_bounds(character: str) -> None:
    unit = len(json.dumps(character, ensure_ascii=False).encode()) - 2
    remaining = MAX_PARAMS_BYTES - len(b'{"value":""}')
    value = character * (remaining // unit) + "a" * (remaining % unit)
    params = {"value": value}

    result = prepare_manual_start_request(
        VERSION,
        VERSION,
        params,
        graph_version_id=VERSION,
        allow_unenforced_secrets=True,
    )
    with pytest.raises(StartRecordRejected):
        prepared({"value": value + "a"})

    assert (
        len(json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode())
        == MAX_PARAMS_BYTES
    )
    assert len(result.identity.request_digest) == 64
    assert result.parameters() == params


def test_manual_start_depth_and_integer_bounds() -> None:
    nested: JsonValue = {"minimum": MIN_INTEGER, "maximum": MAX_INTEGER}
    for _ in range(32):
        nested = {"child": nested}

    result = prepare_manual_start_request(NAMESPACE, GRAPH, nested)
    with pytest.raises(StartRecordRejected):
        prepared({"child": nested})
    for outside in (MIN_INTEGER - 1, MAX_INTEGER + 1, 1 << 100_000):
        with pytest.raises(StartRecordRejected):
            prepared({"unused": outside})

    assert len(result.identity.request_digest) == 64
    assert result.parameters() == nested


@pytest.mark.parametrize(
    "params", [{"unused": "a" * 4097}, {"unused": list(range(257))}]
)
def test_manual_start_does_not_apply_projection_limits(params: JsonObject) -> None:
    empty = prepared({})

    result = prepared(params)

    assert result.request_digest != empty.request_digest


class NonNativeString(str):
    pass


class NonNativeList(list[object]):
    pass


class NonNativeDict(dict[str, object]):
    pass


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {1: "private"},
        {"private": float("nan")},
        {"private": float("inf")},
        {"private": "\ud800"},
        {"\udfff": "private"},
        {"private": Decimal("1")},
        {"private": (1, 2)},
        {"private": NonNativeString("content")},
        {"private": NonNativeList([1])},
        NonNativeDict(private="content"),
    ],
)
def test_manual_start_rejects_non_native_or_invalid_json(params: object) -> None:
    expected = "the manual start record is invalid"

    with pytest.raises(StartRecordRejected, match=f"^{expected}$") as error:
        prepared(params)

    assert error.value.__suppress_context__
    assert error.value.__cause__ is None


def test_manual_start_refuses_cycles_and_wide_containers() -> None:
    cyclic: dict[str, object] = {}
    cyclic["cycle"] = cyclic
    wide = [None] * (MAX_PARAMS_BYTES + 1)

    for params in (cyclic, {"values": wide}):
        with pytest.raises(StartRecordRejected):
            prepared(params)

    assert cyclic["cycle"] is cyclic
    assert len(wide) == MAX_PARAMS_BYTES + 1


@pytest.mark.parametrize("identifier", ["", "private", "8" + "0" * 25])
def test_manual_start_refuses_invalid_scope_or_version(identifier: str) -> None:
    params: JsonObject = {}

    with pytest.raises(StartRecordRejected):
        prepare_manual_start(identifier, GRAPH, params)
    with pytest.raises(StartRecordRejected):
        prepare_manual_start(NAMESPACE, identifier, params)
    with pytest.raises(StartRecordRejected):
        prepare_manual_start(NAMESPACE, GRAPH, params, graph_version_id=identifier)


@pytest.mark.parametrize("skipped", [False, True])
def test_start_receipt_round_trip_preserves_exact_response(skipped: bool) -> None:
    response = (
        SkippedStart(outcome="skipped", reason="overlap", run_id=None)
        if skipped
        else accepted()
    )
    original = receipt(response)

    document = start_receipt_document(original)
    decoded = decode_start_receipt(document)
    document["request_digest"] = "f" * 64

    assert decoded == original
    assert decoded.response == response
    assert set(document) == {
        "version",
        "kind",
        "namespace_id",
        "graph_id",
        "request_digest",
        "response",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", "1"),
        ("version", 0),
        ("version", 2),
        ("kind", "webhook"),
        ("namespace_id", "private"),
        ("graph_id", "private"),
        ("request_digest", "private"),
        ("unexpected", "private"),
        ("response", {"outcome": "accepted", "run_id": RUN}),
        ("response", {"outcome": "skipped", "reason": "overlap", "run_id": RUN}),
        ("response", {"outcome": "skipped", "run_id": None}),
    ],
)
def test_start_receipt_rejects_unknown_and_inconsistent_records(
    field: str, value: JsonValue
) -> None:
    document = start_receipt_document(receipt(accepted()))
    document[field] = value

    with pytest.raises(
        StartRecordRejected, match="^the manual start record is invalid$"
    ):
        decode_start_receipt(document)


@pytest.mark.parametrize("value", [None, {}, [], {"unexpected": "x" * 4096}])
def test_start_receipt_requires_a_complete_bounded_record(value: object) -> None:
    with pytest.raises(StartRecordRejected):
        decode_start_receipt(value)


@pytest.mark.parametrize("version", [None, VERSION], ids=["latest", "explicit"])
@pytest.mark.parametrize("allow", [False, True], ids=["enforced", "unenforced"])
def test_manual_start_snapshot_hashes_the_exact_existing_native_values(
    version: str | None, allow: bool
) -> None:
    params: JsonObject = {
        "nul\u0000": "nul\u0000value",
        "é": 1,
        "e\u0301": 1.0,
        "mixed": [True, -0.0, 0.0, MIN_INTEGER, MAX_INTEGER, {"z": 1, "a": 2}],
    }
    selector = (
        {"kind": "latest"} if version is None else {"kind": "version", "id": version}
    )
    envelope = {
        "format": "manual-start/1",
        "namespace_id": NAMESPACE,
        "graph_id": GRAPH,
        "graph_version": selector,
        "params": params,
        "allow_unenforced_secrets": allow,
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()

    result = prepare_manual_start_request(
        NAMESPACE,
        GRAPH,
        params,
        graph_version_id=version,
        allow_unenforced_secrets=allow,
    )
    decoded = result.parameters()

    assert result.graph_version_id == version
    assert result.allow_unenforced_secrets is allow
    assert result.identity.request_digest == expected_digest
    assert result.identity == prepare_manual_start(
        NAMESPACE,
        GRAPH,
        decoded,
        graph_version_id=version,
        allow_unenforced_secrets=allow,
    )
    assert json.dumps(
        decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) == json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert decoded["nul\u0000"] == "nul\u0000value"
    assert type(decoded["é"]) is int
    assert type(decoded["e\u0301"]) is float
    mixed = decoded["mixed"]
    assert type(mixed) is list
    assert mixed[0] is True
    negative_zero = mixed[1]
    assert type(negative_zero) is float
    assert math.copysign(1.0, negative_zero) == -1.0
    assert list(decoded) == sorted(params)


def test_manual_start_snapshot_has_no_shared_mutable_view() -> None:
    nested: JsonObject = {"children": [1, 2]}
    params: JsonObject = {"left": nested, "right": nested}
    original = {"left": {"children": [1, 2]}, "right": {"children": [1, 2]}}
    expected_identity = prepared(original)

    result = prepare_manual_start_request(NAMESPACE, GRAPH, params)
    exposed = result.parameters()
    left = exposed["left"]
    assert type(left) is dict
    left["children"] = "view-changed"
    nested["children"] = "caller-changed"
    identity = result.identity
    identity.__init__(namespace_id=NAMESPACE, graph_id=RUN, request_digest="0" * 64)
    reread = result.parameters()

    assert exposed != original
    assert params != original
    assert identity != expected_identity
    assert result.identity == expected_identity
    assert reread == original
    assert reread["left"] is not reread["right"]


@dataclass(frozen=True)
class NestedManualRequest:
    request: PreparedManualStartRequest


def test_manual_start_snapshot_refuses_incidental_projection() -> None:
    marker = "private-manual-start-serialization"
    result = prepare_manual_start_request(NAMESPACE, GRAPH, {"secret": marker})
    nested = NestedManualRequest(result)

    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(result)
    with pytest.raises(TypeError, match="requires explicit access"):
        asdict(nested)
    with pytest.raises(TypeError, match="requires explicit access"):
        jsonable_encoder(nested)
    with pytest.raises(
        ValueError, match="PreparedManualStartRequest.*not iterable"
    ) as error:
        jsonable_encoder(result)
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(result)

    assert marker not in repr(result)
    assert marker not in str(error.value)


@pytest.mark.parametrize("entry", [prepare_manual_start, prepare_manual_start_request])
def test_manual_start_refusal_drops_parameters_and_exception_chain(
    entry: Callable[
        [str, str, object], ManualStartIdentity | PreparedManualStartRequest
    ],
) -> None:
    marker = "private-manual-start-refusal"
    invalid = {marker: ("not a native list",)}

    try:
        raise ValueError(marker)
    except ValueError:
        with pytest.raises(StartRecordRejected) as error:
            entry(NAMESPACE, GRAPH, invalid)

    assert error.value.__context__ is error.value.__cause__ is None
    assert marker not in "".join(format_exception(error.value))
    for frame, _ in walk_tb(error.value.__traceback__):
        name = frame.f_code.co_name
        assert name != "_prepare_manual_start_request"
        if name in ("prepare_manual_start", "prepare_manual_start_request"):
            assert frame.f_locals == {}
